"""Connect to the address we validated, not one resolved again afterwards.

`ssrf.py` resolves a name and judges every address it gets back. Then the
fetcher hands the NAME to the HTTP client, which resolves it a second time —
and a name that answered with a public address the first time may answer with
`127.0.0.1` the second. That gap is DNS rebinding, and validating harder does
not close it: only connecting to the address that was judged does.

Three clients, three mechanisms, because none of them share one:

  httpx        a network backend that dials the pinned address instead of
               calling getaddrinfo, and REFUSES an authority nobody pinned.
  curl_cffi    CURLOPT_RESOLVE, which is libcurl's own pre-seeded answer for
               one host and port. Verified honoured: pinned at an unroutable
               address, the request times out instead of succeeding.
  chromium     NOT pinned — see the note at the foot of this module. The
               egress filter covers it instead, and better.

WHAT PINNING CANNOT DO. Through a proxy the client dials the PROXY; the target
name travels inside the CONNECT and the vendor resolves it. There is nothing
to pin and no way to pin it, so this applies to direct fetches only. That is
also the path that needs it: the proxy has no route to our private network,
while our own host does.

The backend below is DEFAULT-DENY. An authority that was never pinned is
refused rather than resolved, so a code path that forgets to validate fails
closed instead of quietly reopening the hole.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING, Any

import httpcore
import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from engine.core.ssrf import ResolvedTarget

logger = structlog.get_logger(__name__)


class UnpinnedAuthority(RuntimeError):
    """A connection was attempted to somewhere nobody validated."""


def _key(host: str, port: int) -> tuple[str, int]:
    return (host.strip("[]").lower().rstrip("."), port)


class PinnedBackend(httpcore.AnyIOBackend):
    """httpcore backend that dials validated addresses and nothing else.

    httpcore calls `connect_tcp` with the ORIGIN host from the URL, which is
    the moment the second resolution would happen. It never gets that far.

    TLS is untouched: httpcore calls `start_tls(server_hostname=...)` from the
    URL AFTER this returns, so the certificate is still checked against the
    real hostname. Pinning the socket does not weaken verification.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pins: dict[tuple[str, int], str] = {}

    def pin(self, host: str, port: int, address: str) -> None:
        self._pins[_key(host, port)] = address

    def pin_target(self, target: ResolvedTarget) -> None:
        for address in target.addresses or ():
            # The first validated answer. They were all judged; any of them is
            # safe, and one is enough to connect with.
            self.pin(target.host, target.port, address)
            return

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's own signature
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        pinned = self._pins.get(_key(host, port))
        if pinned is None:
            raise UnpinnedAuthority(
                f"refusing to connect to {host}:{port}: no validated address for it"
            )
        # Judged once more at the moment of dialling, so a pin that was set
        # from a stale or mistaken validation still cannot be used.
        from engine.core.ssrf import _is_forbidden_ip

        reason = _is_forbidden_ip(ipaddress.ip_address(pinned))
        if reason:
            raise UnpinnedAuthority(f"refusing to connect to {host}:{port}: {reason}")
        return await super().connect_tcp(
            pinned,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def curl_resolve_entries(target: ResolvedTarget) -> list[str]:
    """CURLOPT_RESOLVE entries: `host:port:address`, libcurl's own format."""
    return [f"{target.host}:{target.port}:{a}" for a in (target.addresses or ())]


# THE BROWSER RUNGS ARE NOT PINNED, and cannot be as the pool stands.
# `--host-resolver-rules` is a LAUNCH argument, so it belongs to the browser
# and not to a page; browsers are pooled and serve many targets, so a
# per-request rule would mean launching one browser per request and throwing
# the pool away. That is a worse trade than the gap it closes.
#
# What covers the browser instead, in order of how much it is worth:
#   the egress filter   kernel-level, and the browsers now run as their own
#                       user with no loopback exception at all, so a rebind
#                       onto a private address is refused at the socket
#   the request guard   aborts a subresource aimed at infrastructure, though
#                       it races Chromium's own resolution and is policy
#                       rather than a boundary
#   pre-flight          the document URL is still resolved and validated
#
# The first of those is the real answer, and it is the one that does not care
# what the page persuaded Chromium to resolve.
