"""Places request and response shapes (01-api-surface.md conventions)."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field


class PlaceContacts(BaseModel):
    """What leadgen found on the place's own website. Present only with `enrich`."""

    model_config = ConfigDict(extra="forbid")

    emails: list[str] = Field(default_factory=list)
    contact_page_url: str | None = None
    contact_form_url: str | None = None
    social_links: dict[str, str] = Field(default_factory=dict)
    status: str | None = None


class Place(BaseModel):
    """One business as the Maps results page presents it, plus the detail
    panel's website and phone when details were requested."""

    model_config = ConfigDict(extra="forbid")

    feature_id: str
    name: str
    place_url: str
    latitude: float | None = None
    longitude: float | None = None
    rating: float | None = None
    review_count: int | None = None
    category: str | None = None
    address: str | None = None
    tagline: str | None = None
    open_status: str | None = None
    website: str | None = None
    phone: str | None = None
    contacts: PlaceContacts | None = None


class PlacesSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, Field(min_length=2, max_length=200)]
    location: Annotated[str | None, Field(max_length=120)] = None
    limit: Annotated[int, Field(ge=1, le=60)] = 20
    includeDetails: bool = False
    enrich: bool = False
    timeout: Annotated[int, Field(ge=10_000, le=300_000)] = 120_000


class PlacesSearchData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    location: str | None
    places: list[Place]
    # Counts the route bills on. Details and enrichment are the expensive part;
    # a caller sees what they paid for.
    search_pages: int
    detail_fetches: int
    enriched: int
    cost: dict[str, Any] | None = None
