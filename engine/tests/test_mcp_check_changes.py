"""checkChanges must be able to say "new" and "unchanged".

Found by `mypy --strict`, not by a person: `record_version` returns a
(status, previous_time) TUPLE and the tool assigned the whole thing to
`status`, so `status == "new"` and `status == "same"` compared a tuple to a
string. Both are always false, so every call fell through to the "changed"
branch — a page seen for the first time was reported as changed, and so was a
page that had not moved.

That is the worst shape of bug for a watching tool: confidently wrong, on every
single call, with a plausible-looking answer. An agent polling a URL through
this would have acted on every check.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


class _Outcome:
    class data:  # noqa: N801 - mirrors the real outcome shape
        markdown = "the page text"

        class metadata:  # noqa: N801
            wordCount = 3  # noqa: N815


async def _run(monkeypatch: pytest.MonkeyPatch, status: str, previous_at: Any) -> str:
    from engine.mcp import server
    from engine.storage import db
    from engine.storage import repositories as repo

    async def fake_fetchrow(*args: object, **kwargs: object) -> Any:
        return None

    async def fake_record_version(*args: object, **kwargs: object) -> tuple[str, Any]:
        return status, previous_at

    class _Service:
        async def scrape(self, *args: object, **kwargs: object) -> Any:
            return _Outcome()

    monkeypatch.setattr(db, "fetchrow", fake_fetchrow)
    monkeypatch.setattr(repo, "record_version", fake_record_version)
    monkeypatch.setattr(server, "get_service", lambda: _Service())

    class _Budget:
        def record_pages(self) -> None:
            return None

    monkeypatch.setattr(server, "budget", lambda: _Budget())

    fn = getattr(server.checkChanges, "fn", server.checkChanges)
    return await fn("https://example.com/pricing")


async def test_a_page_never_seen_before_is_new(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "new" in await _run(monkeypatch, "new", None)


async def test_a_page_that_has_not_moved_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _run(monkeypatch, "same", "2026-09-01T10:00:00Z")

    assert "unchanged" in out
    assert "changed since" not in out.replace("unchanged since", "")


async def test_a_page_that_moved_is_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _run(monkeypatch, "changed", "2026-09-01T10:00:00Z")

    assert "changed since" in out
    assert "2026-09-01" in out, "the reader is told when it last differed"
