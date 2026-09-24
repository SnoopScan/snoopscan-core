"""Follow redirects ourselves, validating every hop.

`follow_redirects=True` validates the URL the caller submitted and then
follows whatever the server says, unchecked. A public host answering
`302 Location: http://169.254.169.254/latest/meta-data/` is followed straight
into the cloud metadata endpoint, and the SSRF guard never sees the second
request. Only hop zero was ever checked.

So redirects are turned off at the client and walked here, re-running the full
guard — scheme, host, port, and every address the host resolves to — before
each hop leaves the machine.

WHY httpx BUILDS THE NEXT REQUEST AND NOT US. `Response.next_request` already
does the RFC-correct things that are easy to get wrong by hand: it strips
`Authorization` when the origin changes but keeps it on an http->https upgrade
of the same host, downgrades POST to GET on a 303 and drops the body headers
with it, re-derives cookies from the jar rather than replaying them, and
resolves a relative `Location` against the current URL. Hand-rolling that with
urljoin is how a redirect chain leaks a bearer token to another origin.

THE SCHEME TRAP. A `Location` is JOINED, not parsed fresh, and joining can
change the scheme: `//169.254.169.254/` inherits it, and `file:///etc/passwd`
replaces it outright. So the scheme is re-checked on every hop, not only on
the URL the caller gave us.
"""

from __future__ import annotations

# `timeout` here mirrors httpx's own signature; this wraps an API rather than
# designing one.
# ruff: noqa: ASYNC109
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import structlog

from engine.core.ssrf import resolve_and_validate

logger = structlog.get_logger(__name__)

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class TooManyRedirects(httpx.HTTPError):
    """The chain did not end inside the allowed number of hops."""


async def follow(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    max_redirects: int,
    timeout: httpx.Timeout | None = None,
    extensions: dict[str, object] | None = None,
    validate: bool = True,
    pin: Any = None,
) -> httpx.Response:
    """Send `request`, walking redirects with the SSRF guard on every hop.

    `validate` is off for fetches that do not carry a caller's URL — our own
    search backend on loopback, for one — where the guard would refuse a host
    we put there on purpose.
    """
    seen: list[str] = []
    for _ in range(max_redirects + 1):
        if validate:
            # Raises InvalidRequest, which the API turns into a 400 naming the
            # host. Re-run per hop: the target changes, so the verdict does.
            target = await resolve_and_validate(str(request.url))
            # ...and pin what it validated, so the hop is dialled at the
            # address just judged rather than one resolved again after.
            if pin is not None:
                pin(target)

        # httpx carries both on the REQUEST, not as send() arguments, and a
        # redirect's next_request inherits neither — so they are reapplied on
        # every hop or the second hop silently loses the caller's timeout.
        merged: dict[str, object] = {**request.extensions, **(extensions or {})}
        if timeout is not None:
            merged["timeout"] = timeout.as_dict()
        request.extensions = merged

        response = await client.send(request, follow_redirects=False)
        if response.status_code not in REDIRECT_STATUSES:
            return response

        # The body has to be drained before the connection can carry the next
        # request; without this the pooled socket is left mid-response.
        await response.aread()
        nxt = response.next_request
        if nxt is None:
            return response

        seen.append(str(request.url))
        if str(nxt.url) in seen:
            logger.info("redirect_loop", url=str(nxt.url), hops=len(seen))
            return response
        request = nxt

    raise TooManyRedirects(f"more than {max_redirects} redirects")


def next_hop(current: str, location: str, status: int, method: str) -> tuple[str, str] | None:
    """The (url, method) a redirect points at, or None if it does not redirect.

    For the curl-backed tier, which has no `next_request` of its own. Mirrors
    the parts of the RFC that matter here: a relative Location is resolved
    against the current URL, and a 303 (or a 301/302 on a POST, which is what
    every browser does in practice) becomes a GET.
    """
    if status not in REDIRECT_STATUSES or not location:
        return None
    target = urljoin(current, location.strip())
    if status == 303 or (status in (301, 302) and method.upper() == "POST"):
        return target, "GET"
    return target, method


def strip_credentials_across_origin(
    headers: dict[str, str], current: str, target: str
) -> dict[str, str]:
    """Drop Authorization when the origin changes.

    Kept on an http->https upgrade of the SAME host, which is the one case a
    redirect chain is allowed to carry a credential through. Replaying a
    caller's bearer token to whatever host a Location names is how a redirect
    becomes a credential leak.
    """
    a, b = urlsplit(current), urlsplit(target)
    same_host = a.hostname == b.hostname
    upgrade = same_host and a.scheme == "http" and b.scheme == "https"
    if (a.scheme, a.hostname, a.port) == (b.scheme, b.hostname, b.port) or upgrade:
        return headers
    return {k: v for k, v in headers.items() if k.lower() != "authorization"}
