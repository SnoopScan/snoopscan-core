"""POST /v1/serp — a Google results page: rankings and the AI Overview.

Bought from a results provider, because Google's own page cannot be fetched
reliably (see engine/core/serp.py). With `domain`, the answer also says where
that site ranks and whether the AI Overview cites it — the question a rank or
AI-visibility monitor asks on every check.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from engine.api import billing
from engine.api.deps import ApiKeyDep
from engine.core import serp
from engine.core.models import Cost

router = APIRouter(tags=["serp"])


@router.post("/serp")
async def serp_search(req: serp.SerpRequest, key: ApiKeyDep) -> dict[str, Any]:
    billing.assert_credits(key)
    data = await serp.fetch(req)

    extras = {"serp": 1}
    if req.aiOverview:
        extras["serp_ai_overview"] = 1
    cost = Cost(extras=extras)
    await billing.charge(key, endpoint="serp", url=f"serp:{req.country}:{req.keyword}", cost=cost)
    data["cost"] = cost.model_dump(exclude_none=True)
    return {"success": True, "data": data}
