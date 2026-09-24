"""GET /v1/templates — the named schemas a caller can ask for by name.

Discoverable rather than documented-only: an agent choosing a format should be
able to read the list at runtime, and a customer should not have to find the
docs to learn that `product` exists.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from engine.api.deps import ApiKeyDep
from engine.core.extract import templates

router = APIRouter(tags=["templates"])


@router.get("/templates")
async def list_templates(_: ApiKeyDep) -> dict[str, Any]:
    """Free: a list of names and fields, no fetch and no charge."""
    return {"success": True, "data": {"templates": templates.catalogue()}}
