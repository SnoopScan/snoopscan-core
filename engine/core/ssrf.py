"""SSRF guard — 11-compliance.md section 5.

/v1/scrape fetches a caller-supplied URL, which without controls is a
request-forgery primitive against internal infrastructure.

The important part is that we resolve DNS ourselves, validate every resolved
address, and hand the caller the pinned IP to connect to. Validating the
hostname alone is defeated by DNS rebinding: a name that resolves to a public
address when checked and an internal one when fetched.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from engine.core.errors import FetchFailed, InvalidRequest
from engine.settings import settings

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that never leave the box, whatever DNS says.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)

# Suffixes for names that only exist inside a network.
BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localdomain", ".home.arpa", ".onion")

# Cloud metadata endpoints. 169.254.169.254 is covered by the link-local range
# below, but naming them keeps the failure message specific.
METADATA_IPS = frozenset({"169.254.169.254", "100.100.100.200", "fd00:ec2::254"})


@dataclass(frozen=True)
class ResolvedTarget:
    """A URL cleared for fetching, with its addresses pinned."""

    url: str
    host: str
    port: int
    scheme: str
    addresses: tuple[str, ...] = field(default_factory=tuple)

    @property
    def pinned_ip(self) -> str:
        return self.addresses[0]


# Ranges the stdlib's flags do NOT catch, named rather than guessed at.
# Measured on Python 3.12.14: `100.64.0.1` reports is_private False,
# is_reserved False and is_global False — it matches none of the tests below,
# so carrier-grade NAT, Tailscale and a good many container fabrics were
# reachable. Alibaba's metadata address 100.100.100.200 lives inside it and
# was only ever caught by being listed in METADATA_IPS by hand.
#
# `192.88.99.0/24` is worse: the 6to4 relay anycast range reports
# is_global=True, so any check shaped as "allow if global" admits it.
_EXTRA_FORBIDDEN: tuple[tuple[str, str], ...] = (
    ("100.64.0.0/10", "carrier-grade NAT range"),
    ("192.88.99.0/24", "6to4 relay anycast"),
    ("192.0.0.0/24", "IETF protocol assignments"),
    ("2002::/16", "6to4"),
    ("2001::/32", "Teredo"),
    ("64:ff9b::/96", "NAT64 well-known prefix"),
    ("64:ff9b:1::/48", "NAT64 local-use prefix"),
)
_EXTRA_NETWORKS = tuple((ipaddress.ip_network(cidr), reason) for cidr, reason in _EXTRA_FORBIDDEN)


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a reason string when the address must not be fetched."""
    if str(ip) in METADATA_IPS:
        return "cloud metadata endpoint"
    for network, reason in _EXTRA_NETWORKS:
        if ip.version == network.version and ip in network:
            return reason
    if ip.is_private:
        return "private address range"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_reserved:
        return "reserved address range"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4-mapped/compatible v6 addresses smuggle a v4 target through a v6
        # literal — unwrap and re-check rather than trusting the v6 flags.
        mapped = ip.ipv4_mapped or (
            ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
            if int(ip) >> 32 == 0 and int(ip) != 0
            else None
        )
        if mapped is not None:
            return _is_forbidden_ip(mapped)
        if ip.is_site_local:
            return "site-local address"
        # Unique local addresses (fc00::/7).
        if ip in ipaddress.ip_network("fc00::/7"):
            return "unique local address"
    return None


def _check_hostname(host: str) -> None:
    lowered = host.lower().rstrip(".")
    if not lowered:
        raise InvalidRequest("URL has no host")
    if lowered in BLOCKED_HOSTNAMES:
        raise InvalidRequest(
            "Refusing to fetch an internal hostname",
            {"host": host, "reason": "blocked hostname"},
        )
    if lowered.endswith(BLOCKED_HOST_SUFFIXES):
        raise InvalidRequest(
            "Refusing to fetch an internal hostname",
            {"host": host, "reason": "internal domain suffix"},
        )


async def resolve_and_validate(url: str) -> ResolvedTarget:
    """Validate a caller-supplied URL and pin its resolved addresses.

    Raises InvalidRequest for anything that resolves to infrastructure we must
    not reach. The returned addresses are what the fetcher must connect to —
    re-resolving at connect time reopens the rebinding hole.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()

    if scheme not in ALLOWED_SCHEMES:
        raise InvalidRequest(
            "Only http and https URLs may be fetched",
            {"scheme": scheme or "(none)"},
        )

    host = parts.hostname or ""
    _check_hostname(host)
    port = parts.port or (443 if scheme == "https" else 80)

    if not settings.ssrf_guard_enabled:
        return ResolvedTarget(url=url, host=host, port=port, scheme=scheme)

    # A literal IP in the URL skips DNS but still gets checked.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    # A host made only of hex/digits/dots/colons is MEANT as an address. If it
    # is not a canonical one, refuse it here rather than handing it to the
    # resolver: `0177.0.0.1` and `2130706433` are both 127.0.0.1 to a lenient
    # getaddrinfo, which makes the C library's parsing rules our security
    # policy. It is also a denial-of-service — those forms stall the resolver
    # for seconds on some platforms.
    if literal is None and host and all(c in "0123456789abcdefABCDEFxX.:" for c in host):
        raise InvalidRequest(
            "Refusing a host that looks like an address but is not a valid one",
            {"host": host},
        )

    if literal is not None:
        reason = _is_forbidden_ip(literal)
        if reason:
            raise InvalidRequest(
                "Refusing to fetch an internal address",
                {"host": host, "reason": reason},
            )
        return ResolvedTarget(
            url=url, host=host, port=port, scheme=scheme, addresses=(str(literal),)
        )

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        # A name that does not resolve is the TARGET failing, not the caller
        # mis-typing. `INVALID_REQUEST` tells an SDK user "fix your input, do
        # not retry" and makes a crawl tally a dead domain as a 4xx-style
        # client error (measured, Sep 2026). Every other raise in this
        # function stays INVALID_REQUEST: those are refusals, and a refusal IS
        # about the request.
        raise FetchFailed(
            "Could not resolve host", {"host": host, "reason": str(exc), "stage": "dns"}
        ) from exc

    addresses: list[str] = []
    for info in infos:
        # sockaddr is (host, port) for IPv4 and a 4-tuple for IPv6; the first
        # element is the address in both, but it is typed as str | int.
        sockaddr_host = info[4][0]
        if not isinstance(sockaddr_host, str):
            continue
        # `fe80::1%eth0` — the zone id is not part of the address and makes
        # it unparseable. This used to `continue`, which SKIPPED the answer
        # rather than judging it, so a link-local with a scope fell out of the
        # check entirely instead of being refused.
        addr = sockaddr_host.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise InvalidRequest(
                "Host resolved to an address we could not parse",
                {"host": host, "address": sockaddr_host},
            ) from None
        reason = _is_forbidden_ip(ip)
        if reason:
            # Any forbidden answer poisons the whole name: a rebinding attack
            # returns one public and one internal address. Reject outright.
            raise InvalidRequest(
                "Host resolves to an internal address",
                {"host": host, "address": addr, "reason": reason},
            )
        addresses.append(addr)

    if not addresses:
        raise InvalidRequest("Host resolved to no usable addresses", {"host": host})

    return ResolvedTarget(url=url, host=host, port=port, scheme=scheme, addresses=tuple(addresses))
