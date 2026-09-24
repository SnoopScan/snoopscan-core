"""We must connect to the address we validated, not one resolved again after.

`ssrf.py` judged every address a name returned, and then the client resolved
the name a SECOND time and dialled whatever came back. A name that answers
with a public address once and `127.0.0.1` the next time walks straight
through that gap, and no amount of validating harder closes it.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from engine.core.fetch.pinning import (
    PinnedBackend,
    UnpinnedAuthority,
    curl_resolve_entries,
)
from engine.core.ssrf import ResolvedTarget


def target(host: str, addresses: tuple[str, ...], port: int = 443) -> ResolvedTarget:
    return ResolvedTarget(
        url=f"https://{host}/", host=host, port=port, scheme="https", addresses=addresses
    )


class Recorder(PinnedBackend):
    """Records what would actually have been dialled."""

    def __init__(self) -> None:
        super().__init__()
        self.dialled: list[tuple[str, int]] = []

    async def _super_connect(self, host: str, port: int, **kw: Any) -> str:
        self.dialled.append((host, port))
        return "socket"


async def connect(backend: Recorder, host: str, port: int = 443) -> Any:
    """Call connect_tcp with the real dial stubbed out."""
    import httpcore

    original = httpcore.AnyIOBackend.connect_tcp

    async def fake(self: Any, h: str, p: int, **kw: Any) -> str:
        backend.dialled.append((h, p))
        return "socket"

    httpcore.AnyIOBackend.connect_tcp = fake  # type: ignore[method-assign]
    try:
        return await backend.connect_tcp(host, port)
    finally:
        httpcore.AnyIOBackend.connect_tcp = original  # type: ignore[method-assign]


async def test_the_validated_address_is_what_gets_dialled() -> None:
    backend = Recorder()
    backend.pin_target(target("example.com", ("93.184.216.34",)))

    await connect(backend, "example.com")

    assert backend.dialled == [("93.184.216.34", 443)], (
        "httpcore was handed the NAME and would have resolved it again"
    )


async def test_a_rebind_between_validation_and_connect_cannot_land() -> None:
    """The attack itself. The name was validated as public; by connect time
    its DNS answers 127.0.0.1. Because the socket goes to the address that was
    judged, the second answer is never asked for."""
    backend = Recorder()
    backend.pin_target(target("rebind.test", ("93.184.216.34",)))

    await connect(backend, "rebind.test")

    assert backend.dialled == [("93.184.216.34", 443)]
    assert ("127.0.0.1", 443) not in backend.dialled


async def test_an_authority_nobody_pinned_is_refused_not_resolved() -> None:
    """Default-deny. A code path that forgets to validate must fail closed,
    not quietly reopen the hole it was built to close."""
    backend = Recorder()
    with pytest.raises(UnpinnedAuthority):
        await connect(backend, "never-validated.test")
    assert backend.dialled == []


async def test_a_pin_onto_a_forbidden_address_is_still_refused() -> None:
    """Judged once more at the moment of dialling, so a stale or mistaken pin
    cannot be used either."""
    backend = Recorder()
    backend.pin("sneaky.test", 443, "169.254.169.254")
    with pytest.raises(UnpinnedAuthority):
        await connect(backend, "sneaky.test")
    assert backend.dialled == []


def test_the_host_is_matched_however_it_is_written() -> None:
    backend = PinnedBackend()
    backend.pin_target(target("Example.COM.", ("93.184.216.34",)))
    assert backend._pins.get(("example.com", 443)) == "93.184.216.34"


async def test_a_port_is_part_of_the_pin() -> None:
    """443 and 8443 on one host are different authorities."""
    backend = Recorder()
    backend.pin_target(target("example.com", ("93.184.216.34",), port=443))
    with pytest.raises(UnpinnedAuthority):
        await connect(backend, "example.com", 8443)


def test_curl_gets_the_same_addresses_in_its_own_format() -> None:
    """libcurl's CURLOPT_RESOLVE is `host:port:address`. Verified honoured on
    this build: pinned at an unroutable address the request times out rather
    than succeeding."""
    entries = curl_resolve_entries(target("example.com", ("93.184.216.34", "1.2.3.4")))
    assert entries == ["example.com:443:93.184.216.34", "example.com:443:1.2.3.4"]


def test_nothing_validated_means_nothing_to_pin() -> None:
    assert curl_resolve_entries(target("example.com", ())) == []
    backend = PinnedBackend()
    backend.pin_target(target("example.com", ()))
    assert backend._pins == {}, "an empty validation must not pin anything"


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "100.64.0.1"])
def test_the_dial_time_check_uses_the_same_rules_as_the_guard(address: str) -> None:
    from engine.core.ssrf import _is_forbidden_ip

    assert _is_forbidden_ip(ipaddress.ip_address(address)) is not None
