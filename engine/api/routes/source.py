"""AGPL section 13 compliance: the source offer.

The AGPL's distinguishing obligation is not the copyleft, it is section 13 —
a modified version offered to users over a network must give those users an
opportunity to receive the Corresponding Source. A hosted service that skips
this is in breach even though it never distributes a binary, and it is the
requirement most often missed because nothing in normal testing surfaces it.

So the offer is served by the software itself, from configuration, rather than
living in a README that a fork would not update.

The proprietary modules are NOT part of the Corresponding Source: they are
separately licensed, not derived from the AGPL core, and excluded from the
distribution. `/v1/source` says so plainly rather than leaving it ambiguous.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from engine import __version__
from engine.licensing import offer_paths
from engine.settings import settings

router = APIRouter(tags=["source"])

# NOT a copy of the list. This is the offer users are served under AGPL
# section 13, and a hand-kept copy of it named four of the eleven withheld
# paths while the CI gate and LICENSE-PROPRIETARY stayed in sync with each
# other — so the offer promised the source of the stealth tiers, Places, the
# platform shortcuts and the lead pipeline (6 Sep 2026).
PROPRIETARY_MODULES = offer_paths()


@router.get("/source")
async def source_offer() -> dict[str, Any]:
    """Where to get the Corresponding Source for this running instance.

    Deliberately unauthenticated: section 13 grants the right to "all users
    interacting with it remotely", so putting the offer behind an API key
    would defeat it.
    """
    return {
        "success": True,
        "data": {
            "license": "AGPL-3.0-only",
            "version": __version__,
            "source": settings.source_url,
            "revision": settings.source_revision or "unspecified",
            "notice": (
                "This service runs software licensed under the GNU Affero General "
                "Public License v3.0. Under section 13 you are entitled to the "
                "Corresponding Source for the version running here, available at "
                f"{settings.source_url}."
            ),
            "proprietary_components": {
                "modules": list(PROPRIETARY_MODULES),
                "notice": (
                    "These modules are separately licensed, are not derived from "
                    "the AGPL core, and are not part of the Corresponding Source. "
                    "The core builds and runs without them."
                ),
            },
            "client_sdk_license": "MIT",
        },
    }
