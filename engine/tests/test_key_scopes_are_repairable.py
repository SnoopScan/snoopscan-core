"""A key minted with the wrong scopes must be correctable in place.

The repository has always allowed writing the `scopes` column (its allowlist
in update_api_key names it), but the UpdateKey model left the field out, so
PATCH /internal/keys/{id} silently had no way to express it. The only remedy
was to revoke the key and issue a new one — which changes the secret a
customer has already pasted into their code.

Measured live on 15 Sep 2026: four Playground keys, every one of them stuck
on scrape/crawl/map, because a key created without scopes takes the engine's
own narrow default. Ten of the thirteen endpoints answered 403 FORBIDDEN_SCOPE
for every member, and nothing could repair the keys without reissuing them.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from engine.api.app import app
from engine.api.scopes import ALL_SCOPES
from engine.storage import repositories as repo

TOKEN = "test-internal-token"
H = {"X-Internal-Token": TOKEN}


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine import settings as settings_mod

    monkeypatch.setattr(settings_mod.get_settings(), "internal_token", TOKEN)


@pytest.fixture
def client() -> Any:
    with TestClient(app) as c:
        yield c


def test_patch_can_set_a_keys_scopes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_update(key_id: str, **fields: Any) -> bool:
        seen.update(fields, key_id=key_id)
        return True

    monkeypatch.setattr(repo, "update_api_key", fake_update)

    wanted = list(ALL_SCOPES)
    r = client.patch("/internal/keys/key_123", headers=H, json={"scopes": wanted})

    assert r.status_code == 200, r.text
    assert seen["key_id"] == "key_123"
    assert seen["scopes"] == wanted, "the scopes must reach the repository unchanged"


def test_patching_scopes_leaves_the_other_fields_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only what was sent is written: the handler drops None fields, so a
    scopes-only repair must not blank a key's label or rate limit."""
    seen: dict[str, Any] = {}

    async def fake_update(key_id: str, **fields: Any) -> bool:
        seen.update(fields)
        return True

    monkeypatch.setattr(repo, "update_api_key", fake_update)

    r = client.patch("/internal/keys/key_123", headers=H, json={"scopes": ["scrape"]})

    assert r.status_code == 200, r.text
    assert set(seen) == {"scopes"}, f"only scopes should be written, got {sorted(seen)}"


def test_patch_still_rejects_unknown_fields(client: TestClient) -> None:
    # extra="forbid" on the model, mapped to 400 INVALID_REQUEST rather than 422.
    r = client.patch("/internal/keys/key_123", headers=H, json={"nonsense": True})
    assert r.status_code == 400


def test_patch_still_needs_the_internal_token(client: TestClient) -> None:
    assert client.patch("/internal/keys/key_123", json={"scopes": ["scrape"]}).status_code == 401
