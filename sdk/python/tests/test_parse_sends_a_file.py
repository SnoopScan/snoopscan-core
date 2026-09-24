"""`parse` sends what `/v1/parse` accepts: a multipart file.

It used to post JSON (`{"url": ...}` or `{"content": ...}`) to an endpoint
that takes one uploaded file, so every call failed, and the CLI read a local
PDF as UTF-8 text before sending it (found 23 Sep 2026). A URL now goes
through scrape, which fetches with every tier and reads PDFs.
"""

from __future__ import annotations

import json

import httpx
import pytest
from snoopscan.cli import build_parser, cmd_parse
from snoopscan.client import SnoopScan

PDF = b"%PDF-1.4\n\xe2\xe3\xcf\xd3 binary bytes that are not UTF-8 \xff\xfe\n%%EOF"


def _client(seen: list[httpx.Request], answer: dict) -> SnoopScan:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "data": answer})

    client = SnoopScan(api_key="sk_test", base_url="https://api.example.test")
    # Keep the real client's headers: a client-wide JSON Content-Type is
    # exactly what broke uploads, and a bare test client would hide it.
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), headers=client._client.headers
    )
    return client


def test_a_local_pdf_is_uploaded_byte_for_byte(tmp_path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(PDF)
    seen: list[httpx.Request] = []
    out = _client(seen, {"markdown": "# Report", "pages": 1, "kind": "pdf"}).parse(path=doc)

    assert out["markdown"] == "# Report"
    request = seen[0]
    assert request.url.path == "/v1/parse"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = request.read()
    assert b'filename="report.pdf"' in body
    assert PDF in body, "the PDF must arrive exactly as it is on disk"


def test_text_in_memory_is_uploaded_as_a_named_file() -> None:
    seen: list[httpx.Request] = []
    _client(seen, {"markdown": "hi"}).parse(content="hello", filename="note.md")
    body = seen[0].read()
    assert b'filename="note.md"' in body and b"hello" in body


def test_a_url_is_fetched_by_scrape_and_answered_in_parse_shape() -> None:
    seen: list[httpx.Request] = []
    answer = {"markdown": "# Annual report", "metadata": {"sourceURL": "https://ex.test/r.pdf"}}
    out = _client(seen, answer).parse(url="https://ex.test/r.pdf")

    assert seen[0].url.path == "/v1/scrape"
    sent = json.loads(seen[0].read())
    assert sent["url"] == "https://ex.test/r.pdf" and "pdf" in sent["parsers"]
    assert out["markdown"] == "# Annual report" and out["url"] == "https://ex.test/r.pdf"


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"url": "https://ex.test/a.pdf", "content": "x"}, {"url": "https://ex.test/memo.docx"}],
)
def test_ambiguous_or_unreadable_input_is_refused_before_sending(kwargs) -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(ValueError):
        _client(seen, {}).parse(**kwargs)
    assert seen == []


def test_the_cli_uploads_a_local_file(tmp_path, capsys) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(PDF)
    seen: list[httpx.Request] = []
    client = _client(seen, {"markdown": "# Report", "pages": 1, "kind": "pdf"})
    args = build_parser().parse_args(["parse", str(doc), "-q"])

    assert cmd_parse(client, args) == 0
    assert PDF in seen[0].read()
    assert "# Report" in capsys.readouterr().out
