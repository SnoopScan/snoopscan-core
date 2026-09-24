"""A raw-socket byte counter for httpx, surviving a failed request.

httpx exposes no counter of its own (`tier0_http.py` measures a SUCCESSFUL
response from its headers and body, which is exact but tells you nothing
about a request that never got that far). httpcore's own network layer does
see every byte, success or failure, so this wraps it: `CountingTransport`
builds httpcore's connection pool itself — the one piece of the stack that
takes a `network_backend` — with a backend that tallies bytes on the way
past, even through a TLS handshake that never completes and a proxy CONNECT
that gets torn down mid-tunnel. The tally is read from `.counter` after the
request, exception or not, exactly like tier2/tier3's browser byte meter.
"""

from __future__ import annotations

import ssl
import typing
from dataclasses import dataclass

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend


@dataclass
class ByteCounter:
    down: int = 0
    up: int = 0

    @property
    def total(self) -> int:
        return self.down + self.up


class _CountingStream(httpcore.AsyncNetworkStream):
    def __init__(self, stream: httpcore.AsyncNetworkStream, counter: ByteCounter) -> None:
        self._stream = stream
        self._counter = counter

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        data = await self._stream.read(max_bytes, timeout=timeout)
        self._counter.down += len(data)
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        await self._stream.write(buffer, timeout=timeout)
        self._counter.up += len(buffer)

    async def aclose(self) -> None:
        await self._stream.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        # The handshake itself runs over THIS stream before start_tls returns
        # a new one for the encrypted traffic that follows — both legs count,
        # which is exactly the part a failed CONNECT never got past.
        tls_stream = await self._stream.start_tls(ssl_context, server_hostname, timeout=timeout)
        return _CountingStream(tls_stream, self._counter)

    def get_extra_info(self, info: str) -> typing.Any:
        return self._stream.get_extra_info(info)


class _CountingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, counter: ByteCounter) -> None:
        # AutoBackend, not a hardcoded one: it lazily picks trio vs asyncio
        # itself, exactly what httpx's own transport would have used had we
        # not needed to intercept it here.
        self._backend = AutoBackend()
        self._counter = counter

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._backend.connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )
        return _CountingStream(stream, self._counter)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:  # pragma: no cover - proxied fetches never use a UDS
        stream = await self._backend.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )
        return _CountingStream(stream, self._counter)

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class CountingTransport(httpx.AsyncHTTPTransport):
    """Drop-in for `httpx.AsyncHTTPTransport`, same constructor, plus `.counter`.

    httpx's own transport never forwards `network_backend` to the httpcore
    pool it builds — this rebuilds that one call with it added, everything
    else identical to `httpx._transports.default.AsyncHTTPTransport.__init__`.
    """

    counter: ByteCounter

    def __init__(self, *, proxy: httpx.Proxy | str | None = None, **kwargs: typing.Any) -> None:
        self.counter = ByteCounter()
        proxy_obj = httpx.Proxy(url=proxy) if isinstance(proxy, str) else proxy
        ssl_context = httpx._config.create_ssl_context(
            verify=kwargs.pop("verify", True),
            cert=kwargs.pop("cert", None),
            trust_env=kwargs.pop("trust_env", True),
        )
        limits = kwargs.pop("limits", httpx.Limits())
        backend = _CountingBackend(self.counter)
        if proxy_obj is None:
            self._pool = httpcore.AsyncConnectionPool(
                ssl_context=ssl_context,
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=kwargs.pop("http1", True),
                http2=kwargs.pop("http2", False),
                retries=kwargs.pop("retries", 0),
                network_backend=backend,
            )
        elif proxy_obj.url.scheme in ("http", "https"):
            self._pool = httpcore.AsyncHTTPProxy(
                proxy_url=httpcore.URL(
                    scheme=proxy_obj.url.raw_scheme,
                    host=proxy_obj.url.raw_host,
                    port=proxy_obj.url.port,
                    target=proxy_obj.url.raw_path,
                ),
                proxy_auth=proxy_obj.raw_auth,
                proxy_headers=proxy_obj.headers.raw,
                proxy_ssl_context=proxy_obj.ssl_context,
                ssl_context=ssl_context,
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=kwargs.pop("http1", True),
                http2=kwargs.pop("http2", False),
                network_backend=backend,
            )
        else:  # pragma: no cover - this engine's proxy config is always http/https
            raise ValueError(
                f"Unsupported proxy scheme for CountingTransport: {proxy_obj.url.scheme}"
            )
