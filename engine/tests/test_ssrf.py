"""SSRF guard, including the DNS-rebinding case.

11-compliance.md section 5. These are security tests: a regression here turns
/v1/scrape into a request-forgery primitive against internal infrastructure.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from engine.core.errors import InvalidRequest
from engine.core.ssrf import resolve_and_validate


async def test_rejects_non_http_scheme() -> None:
    for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com"):
        with pytest.raises(InvalidRequest) as exc:
            await resolve_and_validate(url)
        assert "http" in exc.value.message.lower()


async def test_rejects_localhost_by_name() -> None:
    for url in ("http://localhost/admin", "http://LOCALHOST:8080/", "http://foo.local/"):
        with pytest.raises(InvalidRequest):
            await resolve_and_validate(url)


async def test_rejects_internal_suffixes() -> None:
    for url in ("http://db.internal/", "http://svc.localdomain/", "http://x.home.arpa/"):
        with pytest.raises(InvalidRequest):
            await resolve_and_validate(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://172.16.4.4/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fc00::1]/",
    ],
)
async def test_rejects_private_literals(url: str) -> None:
    with pytest.raises(InvalidRequest):
        await resolve_and_validate(url)


async def test_rejects_ipv4_mapped_ipv6_smuggling() -> None:
    """A v6 literal wrapping a private v4 address must not slip through."""
    with pytest.raises(InvalidRequest):
        await resolve_and_validate("http://[::ffff:127.0.0.1]/")


async def test_allows_public_literal() -> None:
    target = await resolve_and_validate("https://93.184.216.34/")
    assert target.addresses == ("93.184.216.34",)
    assert target.port == 443


async def test_dns_rebinding_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname resolving to BOTH a public and an internal address is
    rejected outright.

    This is the rebinding attack: the check sees the public answer, the fetch
    would connect to the internal one. Any forbidden answer poisons the name.
    """

    async def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    import asyncio

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(InvalidRequest) as exc:
        await resolve_and_validate("https://rebind.example.com/")
    assert "internal" in exc.value.message.lower()


async def test_pins_resolved_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolved addresses are returned so the fetcher connects to what was
    checked, rather than re-resolving."""

    async def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    import asyncio

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    target = await resolve_and_validate("https://example.com/page")
    assert target.pinned_ip == "93.184.216.34"
    assert target.host == "example.com"


# --------------------------------------------------------------------------
# Ranges the stdlib's flags do not catch
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "why"),
    [
        ("100.64.0.1", "carrier-grade NAT: is_private, is_reserved and is_global are ALL False"),
        ("100.127.255.1", "the far end of the same range"),
        ("100.100.100.200", "Alibaba metadata, which lives inside CGNAT"),
        ("192.88.99.1", "6to4 relay anycast reports is_global=True"),
        ("64:ff9b::7f00:1", "NAT64-wrapped 127.0.0.1, which also reports is_global=True"),
        ("2002:7f00:1::", "6to4-wrapped 127.0.0.1"),
    ],
)
def test_the_ranges_the_flags_miss(address: str, why: str) -> None:
    import ipaddress

    from engine.core.ssrf import _is_forbidden_ip

    assert _is_forbidden_ip(ipaddress.ip_address(address)) is not None, why


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1111"])
def test_ordinary_public_addresses_are_still_allowed(address: str) -> None:
    import ipaddress

    from engine.core.ssrf import _is_forbidden_ip

    assert _is_forbidden_ip(ipaddress.ip_address(address)) is None


@pytest.mark.parametrize("host", ["0177.0.0.1", "2130706433", "0x7f000001", "127.1"])
async def test_a_host_that_looks_like_an_address_must_be_a_valid_one(host: str) -> None:
    """Otherwise the C library's parsing rules become our security policy —
    every one of these is 127.0.0.1 to a lenient getaddrinfo. It is a
    denial-of-service too: some of them stall the resolver for seconds."""
    from engine.core.errors import InvalidRequest
    from engine.core.ssrf import resolve_and_validate

    with pytest.raises(InvalidRequest):
        await resolve_and_validate(f"http://{host}/")


async def test_an_ordinary_hostname_is_not_mistaken_for_an_address() -> None:
    """The guard keys on the character set, so a name made of hex letters —
    `face.example.com` — must not be caught by it."""
    from engine.core.ssrf import resolve_and_validate

    target = await resolve_and_validate("https://example.com/")
    assert target.host == "example.com"
