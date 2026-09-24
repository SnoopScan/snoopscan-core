"""Normalised shapes. One product, one post, whatever platform produced it."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Variant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    title: str | None = None
    sku: str | None = None
    price: str | None = None
    compare_at_price: str | None = None
    available: bool | None = None


class Product(BaseModel):
    """A product as its own store publishes it — price as the store states it,
    as a string, in the store's currency. No conversion, no guessing."""

    model_config = ConfigDict(extra="forbid")

    platform: str
    id: str
    title: str
    url: str | None = None
    handle: str | None = None
    description_html: str | None = None
    vendor: str | None = None
    product_type: str | None = None
    price: str | None = None
    compare_at_price: str | None = None
    currency: str | None = None
    available: bool | None = None
    sku: str | None = None
    images: list[str] = Field(default_factory=list)
    variants: list[Variant] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    rating: float | None = None
    review_count: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class Post(BaseModel):
    """An article, post or topic. `content_html` only when the API gives it."""

    model_config = ConfigDict(extra="forbid")

    platform: str
    id: str
    title: str
    url: str
    published_at: str | None = None
    updated_at: str | None = None
    author: str | None = None
    excerpt: str | None = None
    content_html: str | None = None
    tags: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class Listing(BaseModel):
    """What a listing call produced, and what it cost to produce."""

    model_config = ConfigDict(extra="forbid")

    platform: str | None
    source: str  # api | feed | none
    pages_fetched: int = 0
    total: int | None = None
    products: list[Product] = Field(default_factory=list)
    posts: list[Post] = Field(default_factory=list)
    note: str | None = None
