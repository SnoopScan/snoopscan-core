"""A redirect must not carry us somewhere the first URL was refused for.

`follow_redirects=True` validated the submitted URL and then followed whatever
the server said. A public host answering
`302 Location: http://169.254.169.254/latest/meta-data/` was followed straight
into the cloud metadata endpoint, and the SSRF guard never saw the request
that mattered. These run a real redirecting server on loopback.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from engine.core.errors import InvalidRequest
from engine.core.fetch.redirects import TooManyRedirects
from engine.core.fetch.redirects import follow as follow_redirects


class Redirector:
    """A server that answers with whatever Location the test asks for."""

    def __init__(self, location: str | None, status: int = 302) -> None:
        self.location = location
        self.status = status
        self.hops = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        self.hops += 1
        if self.location:
            body = b"redirecting"
            head = (
                f"HTTP/1.1 {self.status} Found\r\n"
                f"Location: {self.location}\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
        else:
            body = b"<html><body>the real page</body></html>"
            head = (
                "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode()
        writer.write(head + body)
        await writer.drain()
        writer.close()


async def serve(server: Redirector) -> AsyncIterator[int]:
    running = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    port = running.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        running.close()
        await running.wait_closed()


@pytest.fixture
async def metadata_redirect() -> AsyncIterator[tuple[Redirector, int]]:
    server = Redirector("http://169.254.169.254/latest/meta-data/")
    async for port in serve(server):
        yield server, port


@pytest.fixture
async def plain_redirect() -> AsyncIterator[tuple[Redirector, int]]:
    server = Redirector(None)
    async for port in serve(server):
        yield server, port


async def test_every_hop_is_validated_not_just_the_first(
    metadata_redirect: tuple[Redirector, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole fault, pinned. The test server has to sit on loopback, which
    the real guard refuses at hop 0, so the guard is stood in for by a spy
    that records what it was ASKED about and refuses the metadata address.
    What is being proved is that the second URL reaches the guard at all —
    before this, it never did."""
    from engine.core.fetch import redirects as mod

    asked: list[str] = []

    async def spy(url: str) -> None:
        asked.append(url)
        if "169.254.169.254" in url:
            raise InvalidRequest("link-local address")

    monkeypatch.setattr(mod, "resolve_and_validate", spy)

    server, port = metadata_redirect
    async with httpx.AsyncClient(follow_redirects=False) as client:
        request = client.build_request("GET", f"http://127.0.0.1:{port}/start")
        with pytest.raises(InvalidRequest):
            await follow_redirects(client, request, max_redirects=5)

    assert len(asked) == 2, f"the guard saw {len(asked)} of 2 hops"
    assert "169.254.169.254" in asked[1]
    assert server.hops == 1, "the dangerous hop never left the machine"


async def test_an_ordinary_response_is_returned_unchanged(
    plain_redirect: tuple[Redirector, int],
) -> None:
    """The guard must not break the common case."""
    async with httpx.AsyncClient(follow_redirects=False) as client:
        request = client.build_request("GET", f"http://127.0.0.1:{port_of(plain_redirect)}/x")
        response = await follow_redirects(client, request, max_redirects=5, validate=False)
    assert response.status_code == 200
    assert b"the real page" in response.content


def port_of(fixture: tuple[Redirector, int]) -> int:
    return fixture[1]


async def test_a_chain_that_returns_to_a_url_it_already_used_stops() -> None:
    """A chain of slow 302s is a resource-exhaustion vector, not just a bug.
    A repeat of an address already in the chain ends it — the caller gets the
    redirect itself rather than an error, because nothing went wrong."""
    server = Redirector("")
    async for port in serve(server):
        server.location = f"http://127.0.0.1:{port}/next"
        async with httpx.AsyncClient(follow_redirects=False) as client:
            request = client.build_request("GET", f"http://127.0.0.1:{port}/next")
            response = await follow_redirects(client, request, max_redirects=20, validate=False)
        assert response.status_code == 302
        assert server.hops <= 3, f"it kept going: {server.hops} hops"


async def test_a_chain_of_new_urls_is_cut_off_at_the_limit() -> None:
    """Loop detection only catches a REPEAT. A chain that keeps inventing new
    paths is bounded by the hop count instead."""

    class Endless(Redirector):
        async def handle(self, reader, writer) -> None:  # type: ignore[no-untyped-def]
            self.location = f"http://127.0.0.1:{self.port}/hop{self.hops}"
            await super().handle(reader, writer)

    server = Endless("")
    async for port in serve(server):
        server.port = port  # type: ignore[attr-defined]
        async with httpx.AsyncClient(follow_redirects=False) as client:
            request = client.build_request("GET", f"http://127.0.0.1:{port}/start")
            with pytest.raises(TooManyRedirects):
                await follow_redirects(client, request, max_redirects=3, validate=False)
        assert server.hops == 4, f"expected 4 attempts (3 redirects + 1), got {server.hops}"


async def test_a_file_scheme_in_a_location_is_refused() -> None:
    """A Location is JOINED, not parsed fresh, and joining can replace the
    scheme outright. Checking only the caller's URL is a real bypass."""
    server = Redirector("file:///etc/passwd")
    async for port in serve(server):
        async with httpx.AsyncClient(follow_redirects=False) as client:
            request = client.build_request("GET", f"http://127.0.0.1:{port}/start")
            with pytest.raises((InvalidRequest, httpx.HTTPError, httpx.UnsupportedProtocol)):
                await follow_redirects(client, request, max_redirects=5)


# --------------------------------------------------------------------------
# The curl-backed tier, which has no next_request of its own
# --------------------------------------------------------------------------


def test_a_relative_location_is_resolved_against_the_current_url() -> None:
    from engine.core.fetch.redirects import next_hop

    assert next_hop("https://a.test/x/y/z", "/latest/meta-data/", 302, "GET") == (
        "https://a.test/latest/meta-data/",
        "GET",
    )


def test_a_scheme_relative_location_can_change_the_host() -> None:
    """`//169.254.169.254/` inherits the scheme and replaces the host — which
    is why the guard has to run on the RESULT of the join, not the Location."""
    from engine.core.fetch.redirects import next_hop

    hop = next_hop("https://a.test/x", "//169.254.169.254/", 302, "GET")
    assert hop is not None
    assert hop[0] == "https://169.254.169.254/"


def test_a_303_becomes_a_get() -> None:
    from engine.core.fetch.redirects import next_hop

    assert next_hop("https://a.test/x", "/done", 303, "POST") == ("https://a.test/done", "GET")


def test_a_non_redirect_is_not_a_hop() -> None:
    from engine.core.fetch.redirects import next_hop

    assert next_hop("https://a.test/x", "", 200, "GET") is None
    assert next_hop("https://a.test/x", "/elsewhere", 200, "GET") is None


def test_a_credential_does_not_follow_a_redirect_to_another_host() -> None:
    """Replaying a caller's bearer token at whatever host a Location names is
    how a redirect chain becomes a credential leak."""
    from engine.core.fetch.redirects import strip_credentials_across_origin

    kept = strip_credentials_across_origin(
        {"Authorization": "Bearer x", "Accept": "*/*"},
        "https://a.test/one",
        "https://a.test/two",
    )
    assert "Authorization" in kept, "same origin keeps it"

    dropped = strip_credentials_across_origin(
        {"Authorization": "Bearer x", "Accept": "*/*"},
        "https://a.test/one",
        "https://evil.test/two",
    )
    assert "Authorization" not in dropped
    assert dropped["Accept"] == "*/*", "and nothing else is disturbed"


def test_an_http_to_https_upgrade_of_the_same_host_keeps_the_credential() -> None:
    from engine.core.fetch.redirects import strip_credentials_across_origin

    kept = strip_credentials_across_origin(
        {"Authorization": "Bearer x"}, "http://a.test/one", "https://a.test/one"
    )
    assert "Authorization" in kept
