"""A file is refused before the proxy carries it, and the refusal ends the ladder.

25 Sep 2026: a scrape of an Ubuntu mirror's `.orig.tar.gz` — served as
`text/html` with `Content-Encoding: x-gzip` — climbed http, browser, browser,
stealth, and the stealth rung pulled it through a residential exit. The TODO
entry of 17 Sep says why a size cap alone would have made it worse: a rung
that refuses for size is a failed rung, and the ladder climbs on a failed
rung. So the refusal is TERMINAL first, then the caps.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from engine.core.detect.validator import Reason, validate
from engine.core.fetch import binary
from engine.core.fetch.base import FetchRequest, FetchResult
from engine.core.fetch.escalation import DomainProfile, EscalationController
from engine.core.fetch.tier0_http import HttpFetcher, _read_capped
from engine.core.models import Tier
from engine.settings import settings

TAR = b"zip4j-2.11.5/" + b"\x00" * 244 + b"ustar\x0000" + b"\x00" * 300
GZIP = b"\x1f\x8b\x08\x00" + b"\x00" * 600
HTML = b"<!doctype html><html><head><title>t</title></head><body>" + b"<p>word </p>" * 80


# -- what is a file ------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (GZIP, "gzip"),
        (TAR, "tar"),
        (b"PK\x03\x04" + b"\x00" * 40, "zip"),
        (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40, "video"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "image"),
        (b"BZh91AY&SY" + b"\x00" * 40, "bzip2"),
    ],
)
def test_a_file_is_known_by_its_bytes(body: bytes, kind: str) -> None:
    assert binary.sniff(body) == kind


def test_pages_and_data_are_not_files() -> None:
    assert binary.sniff(HTML) is None
    assert binary.sniff(b'{"a": 1}') is None
    assert binary.sniff(b"BZh is how a sentence about bzip2 might start") is None
    assert binary.sniff(b"") is None


def test_a_document_we_parse_is_never_refused_for_being_a_zip() -> None:
    docx = b"PK\x03\x04" + b"\x00" * 40
    assert binary.refused_kind("https://x.example/report.docx", None, docx) is None
    assert (
        binary.refused_kind(
            "https://x.example/r",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            docx,
        )
        is None
    )
    assert binary.refused_kind("https://x.example/archive", "text/html", docx) == "zip"


def test_a_url_that_names_a_download() -> None:
    assert binary.names_a_download("http://mirror.example/pool/z/zip4j_2.11.5.orig.tar.gz")
    assert binary.names_a_download("https://cdn.example/v.MP4?sig=1")
    assert not binary.names_a_download("https://example.com/report.pdf")
    assert not binary.names_a_download("https://example.com/blog/v2.0-released")
    assert not binary.names_a_download("https://example.com/")


# -- the validator reads a refusal as the end ----------------------------------


def _result(**kw: Any) -> FetchResult:
    base: dict[str, Any] = {
        "url": "http://mirror.example/a.tar.gz",
        "status_code": 200,
        "headers": {},
        "body": b"",
        "content_type": "text/html; charset=UTF-8",
        "tier": "http",
        "latency_ms": 10,
        "bytes_transferred": 100,
    }
    base.update(kw)
    return FetchResult(**base)


def test_a_mislabelled_archive_is_a_target_error_not_a_thin_page() -> None:
    verdict = validate(_result(body=GZIP))
    assert verdict.reason == Reason.TARGET_ERROR
    assert verdict.signal == "binary_content"
    assert "gzip" in verdict.details["message"]


def test_a_fetcher_refusal_is_a_target_error_even_with_no_status() -> None:
    verdict = validate(
        _result(status_code=None, refused="response_too_large", refused_detail="over 25 MB")
    )
    assert verdict.reason == Reason.TARGET_ERROR
    assert verdict.signal == "response_too_large"


class _Rung:
    def __init__(self, tier: Tier, result: FetchResult) -> None:
        self.name = str(tier)
        self.calls = 0
        self._result = result

    async def fetch(self, req: FetchRequest) -> FetchResult:
        self.calls += 1
        return self._result

    async def healthcheck(self) -> bool:
        return True


async def test_a_refused_download_does_not_climb_to_a_browser() -> None:
    http = _Rung(Tier.HTTP, _result(refused="binary_content", refused_detail="a gzip file"))
    browser = _Rung(Tier.BROWSER, _result(body=HTML, tier="browser"))
    outcome = await EscalationController({Tier.HTTP: http, Tier.BROWSER: browser}).fetch(
        FetchRequest(url="http://mirror.example/a.tar.gz"), DomainProfile("mirror.example")
    )
    assert not outcome.succeeded
    assert outcome.verdict.signal == "binary_content"
    assert browser.calls == 0
    assert outcome.tiers_attempted == ["http"]


# -- tier 0 reads only as much as it must ----------------------------------------


def _streamed(body: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    class _Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            for i in range(0, len(body), 1024):
                yield body[i : i + 1024]

    return httpx.Response(
        200,
        headers=headers or {"content-type": "text/html"},
        stream=_Chunks(),
        request=httpx.Request("GET", "https://example.com/file"),
    )


def _req(proxied: bool) -> FetchRequest:
    return FetchRequest(
        url="https://example.com/file",
        proxy_url="http://u:p@gate.example:1" if proxied else None,
    )


async def test_a_proxied_response_declaring_more_than_the_cap_is_not_read(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(settings, "proxy_max_response_mb", 1)
    response = _streamed(HTML, {"content-type": "text/html", "content-length": str(5 * 2**20)})
    body, refused, detail = await _read_capped(response, _req(proxied=True))
    assert (body, refused) == (b"", "response_too_large")
    assert detail and "5.0 MB" in detail


async def test_a_proxied_response_is_cut_off_the_moment_it_passes_the_cap(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(settings, "proxy_max_response_mb", 1)
    body, refused, _ = await _read_capped(_streamed(HTML * 2000), _req(proxied=True))
    assert refused == "response_too_large" and body == b""


async def test_a_direct_response_is_not_capped(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "proxy_max_response_mb", 1)
    big = HTML * 2000
    body, refused, _ = await _read_capped(_streamed(big), _req(proxied=False))
    assert refused is None and body == big


async def test_the_cap_can_be_switched_off(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "proxy_max_response_mb", 0)
    big = HTML * 2000
    body, refused, _ = await _read_capped(_streamed(big), _req(proxied=True))
    assert refused is None and body == big


async def test_a_file_is_refused_on_its_first_chunk() -> None:
    body, refused, detail = await _read_capped(_streamed(GZIP * 2000), _req(proxied=False))
    assert (body, refused) == (b"", "binary_content")
    assert detail and "gzip" in detail


async def test_tier0_end_to_end_refuses_a_mislabelled_archive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=TAR * 50)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await HttpFetcher(client=client).fetch(FetchRequest(url="http://127.0.0.1/a.tar"))
    assert result.refused == "binary_content"
    assert result.body == b""
    assert validate(result).reason == Reason.TARGET_ERROR


async def test_tier0_still_returns_an_ordinary_page_whole() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=HTML)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await HttpFetcher(client=client).fetch(FetchRequest(url="http://127.0.0.1/"))
    assert result.refused is None
    assert result.body == HTML
    assert result.bytes_transferred > len(HTML)


# -- tier 1: libcurl enforces the cap itself --------------------------------------


async def test_tier1_maps_libcurls_size_refusal_to_a_terminal_one(monkeypatch: Any) -> None:
    """curl error 63 ("Maximum file size exceeded"), from MAXFILESIZE_LARGE."""
    from curl_cffi.requests import errors as curl_errors

    from engine.core.fetch import tier1_impersonate

    monkeypatch.setattr(settings, "proxy_max_response_mb", 1)
    seen_options: dict[Any, Any] = {}

    class _Session:
        def __init__(self) -> None:
            self.curl_options: dict[Any, Any] = {}

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *a: object) -> None:
            seen_options.update(self.curl_options)

        async def request(self, *a: object, **k: object) -> object:
            err = curl_errors.RequestsError(
                "Failed to perform, curl: (63) Maximum file size exceeded.", code=63
            )
            raise err

    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _Session)
    result = await tier1_impersonate.ImpersonateFetcher().fetch(
        FetchRequest(url="http://127.0.0.1/big.bin", proxy_url="http://u:p@127.0.0.1:9")
    )
    from curl_cffi import CurlOpt

    assert seen_options.get(CurlOpt.MAXFILESIZE_LARGE) == 2**20
    assert result.refused == "response_too_large"
    assert result.error is None
    assert validate(result).reason == Reason.TARGET_ERROR
