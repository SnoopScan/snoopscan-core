"""The published API description names the product and where it answers.

It said "Scraping Engine" with no server address, so APIs.guru and any client
generator pointed at /openapi.json would list the repository's name and could
not tell where to send a request (found preparing directory listings, Sep 2026).
"""

from __future__ import annotations

from engine.api.app import _public_openapi, app
from engine.settings import settings


def test_the_schema_names_snoopscan_and_its_server() -> None:
    app.openapi_schema = None
    schema = _public_openapi()
    assert schema["info"]["title"] == "SnoopScan API"
    assert schema["info"].get("description")
    assert schema["servers"] == [{"url": settings.public_api_url.rstrip("/")}]
