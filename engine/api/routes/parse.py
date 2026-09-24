"""POST /v1/parse — a document the customer already has, as markdown.

Multipart, one file, bounded in size and pages. Metered per page through the
same pdf_page price a scraped PDF pays; a Word document or HTML file counts
one page per 3,000 characters.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, File, UploadFile

from engine.api import billing
from engine.api.deps import ApiKeyDep
from engine.core import credits as rules
from engine.core.errors import EngineError, ErrorCode
from engine.core.models import Cost
from engine.core.parse import UnsupportedDocument, parse_bytes
from engine.settings import settings

router = APIRouter(tags=["parse"])


@router.post("/parse")
async def parse_document(key: ApiKeyDep, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    billing.assert_credits(key)
    limit = settings.parse_max_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise EngineError(
            ErrorCode.INVALID_REQUEST, f"The file is over {settings.parse_max_mb} MB."
        )
    if not data:
        raise EngineError(ErrorCode.INVALID_REQUEST, "The file is empty.")

    try:
        parsed = await asyncio.to_thread(
            parse_bytes,
            file.filename or "",
            file.content_type or "",
            data,
            max_pages=settings.parse_max_pages,
        )
    except UnsupportedDocument as exc:
        raise EngineError(ErrorCode.INVALID_REQUEST, str(exc)) from exc

    cost = Cost(pdf_pages=parsed.pages)
    await billing.charge(key, endpoint="parse", url=None, cost=cost)
    return {
        "success": True,
        "data": {
            "markdown": parsed.markdown,
            "pages": parsed.pages,
            "kind": parsed.kind,
            "filename": file.filename,
            "cost": {
                "pdf_pages": parsed.pages,
                "credits": rules.credits_for(cost, await billing.cost_table()),
            },
        },
    }
