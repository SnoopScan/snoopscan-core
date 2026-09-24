"""Each platform's JSON into one Product or Post — against the real responses
captured in fixtures/platforms (6 Sep 2026). If a platform changes its shape,
these are the tests that say so, with the old response beside them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine.core.fetch.base import FetchResult
from engine.platforms import catalog, posts
from engine.tests.fixtures.platforms import load

FIX = Path(__file__).parent / "fixtures" / "platforms"


def _j(name: str) -> Any:
    return json.loads((FIX / name).read_text())


# --------------------------------------------------------------------------
# Shopify
# --------------------------------------------------------------------------


def test_shopify_listing_item_normalises() -> None:
    raw = _j("shopify_products.json")["products"][0]
    p = catalog.shopify_product(raw, "https://www.allbirds.com")
    assert p.platform == "shopify" and p.id == str(raw["id"])
    assert p.url == f"https://www.allbirds.com/products/{raw['handle']}"
    assert p.price == raw["variants"][0]["price"], "price is the store's string, untouched"
    assert len(p.variants) == len(raw["variants"])
    assert p.available in (True, False)
    assert all(i.startswith("http") for i in p.images)


def test_shopify_single_product_normalises_the_same_way() -> None:
    raw = _j("shopify_product.json")["product"]
    p = catalog.shopify_product(raw, "https://www.allbirds.com")
    assert p.title == raw["title"] and p.handle == raw["handle"]


def test_shopify_tags_accept_both_list_and_comma_string() -> None:
    base = {"id": 1, "title": "T", "handle": "t", "variants": [], "images": []}
    assert catalog.shopify_product({**base, "tags": ["a", "b"]}, "https://s").tags == ["a", "b"]
    assert catalog.shopify_product({**base, "tags": "a, b"}, "https://s").tags == ["a", "b"]


# --------------------------------------------------------------------------
# WooCommerce
# --------------------------------------------------------------------------


def test_woo_store_api_item_normalises_with_minor_unit_prices() -> None:
    raw = _j("woocommerce_store_products.json")[0]
    p = catalog.woo_product(raw)
    assert p.platform == "woocommerce"
    assert p.currency == raw["prices"]["currency_code"]
    minor = raw["prices"]["currency_minor_unit"]
    assert p.price == f"{int(raw['prices']['price']) / 10**minor:.{minor}f}", (
        "Store API prices are integers in minor units and must be scaled"
    )
    assert p.url == raw["permalink"]
    assert p.available == raw["is_in_stock"]


def test_woo_compare_at_only_when_actually_on_sale() -> None:
    raw = {**_j("woocommerce_store_products.json")[0]}
    raw["on_sale"] = True
    raw["prices"] = {
        **raw["prices"],
        "regular_price": "20000",
        "sale_price": "15000",
        "price": "15000",
    }
    p = catalog.woo_product(raw)
    assert p.price == "150.00" and p.compare_at_price == "200.00"
    raw["on_sale"] = False
    assert catalog.woo_product(raw).compare_at_price is None


# --------------------------------------------------------------------------
# posts
# --------------------------------------------------------------------------


def test_wordpress_post_strips_rendered_html_from_title_and_excerpt() -> None:
    raw = _j("wordpress_posts.json")[0]
    p = posts.wp_post(raw)
    assert "<" not in p.title and p.url == raw["link"]
    assert p.content_html and p.published_at == raw["date"]


def test_substack_post_keeps_the_paywall_flag() -> None:
    raw = _j("substack_archive.json")[0]
    p = posts.substack_post(raw)
    assert p.url == raw["canonical_url"] and "audience" in p.extra


def test_squarespace_item_gets_an_absolute_url() -> None:
    raw = _j("squarespace_blog.json")["items"][0]
    p = posts.squarespace_item(raw, "https://www.squarespace.com")
    assert p.url.startswith("https://www.squarespace.com/") and p.title == raw["title"]


def test_discourse_topic_builds_its_canonical_url() -> None:
    raw = _j("discourse_latest.json")["topic_list"]["topics"][0]
    p = posts.discourse_topic(raw, "https://discuss.python.org")
    assert p.url == f"https://discuss.python.org/t/{raw['slug']}/{raw['id']}"
    assert "posts_count" in p.extra


# --------------------------------------------------------------------------
# pagination, with a fake fetcher — the logic the fixtures cannot exercise
# --------------------------------------------------------------------------


class _Fetcher:
    def __init__(self, pages: dict[str, tuple[int, str, dict[str, str]]]) -> None:
        self.pages, self.calls = pages, []

    async def fetch(self, req: Any) -> FetchResult:
        self.calls.append(req.url)
        status, body, headers = self.pages.get(req.url, (404, "{}", {}))
        return FetchResult(
            url=req.url,
            status_code=status,
            headers=headers,
            body=body.encode(),
            content_type="application/json",
            tier="http",
            latency_ms=1,
            bytes_transferred=len(body),
        )


def _shopify_page(n: int, start: int) -> str:
    return json.dumps(
        {
            "products": [
                {
                    "id": start + i,
                    "title": f"P{start + i}",
                    "handle": f"p-{start + i}",
                    "variants": [],
                    "images": [],
                }
                for i in range(n)
            ]
        }
    )


async def test_shopify_pages_until_a_short_page() -> None:
    root = "https://shop.test"
    f = _Fetcher(
        {
            f"{root}/products.json?limit=250&page=1": (200, _shopify_page(250, 0), {}),
            f"{root}/products.json?limit=250&page=2": (200, _shopify_page(7, 250), {}),
        }
    )
    out = await catalog.shopify_catalog(root + "/", f, limit=5000)
    assert out.total == 257 and out.pages_fetched == 2
    assert not any("page=3" in u for u in f.calls), "a short page ends the walk"


async def test_shopify_stops_at_the_callers_limit() -> None:
    root = "https://shop.test"
    f = _Fetcher({f"{root}/products.json?limit=250&page=1": (200, _shopify_page(250, 0), {})})
    out = await catalog.shopify_catalog(root + "/", f, limit=10)
    assert out.total == 10 and out.pages_fetched == 1


async def test_woo_pages_by_the_total_pages_header() -> None:
    root = "https://woo.test"
    item = json.dumps(
        [
            {
                "id": 1,
                "name": "A",
                "slug": "a",
                "permalink": "https://woo.test/product/a/",
                "prices": {"price": "100", "currency_code": "GBP", "currency_minor_unit": 2},
            }
        ]
    )
    hdr = {"x-wp-totalpages": "2", "x-wp-total": "2"}
    f = _Fetcher(
        {
            f"{root}/wp-json/wc/store/v1/products?per_page=100&page=1": (200, item, hdr),
            f"{root}/wp-json/wc/store/v1/products?per_page=100&page=2": (200, item, hdr),
        }
    )
    out = await catalog.woo_catalog(root + "/", f, limit=100)
    assert out.pages_fetched == 2 and len(out.products) == 2 and out.total == 2
    assert out.products[0].price == "1.00" and out.products[0].currency == "GBP"


async def test_a_403_on_products_json_is_reported_not_raised() -> None:
    root = "https://locked.test"
    f = _Fetcher({f"{root}/products.json?limit=250&page=1": (403, "", {})})
    out = await catalog.shopify_catalog(root + "/", f)
    assert out.products == [] and out.note and "403" in out.note


# --------------------------------------------------------------------------
# Squarespace commerce
# --------------------------------------------------------------------------


def test_squarespace_store_item_normalises_with_variant_prices() -> None:
    fx = _j("squarespace_shop.json")
    raw = fx["items"][0]
    p = catalog.squarespace_product(
        raw, "https://www.flourandbranch.com", fx["website"].get("currency")
    )
    assert p.platform == "squarespace" and p.title == raw["title"]
    assert p.url.startswith("https://www.flourandbranch.com/")
    v0 = raw["structuredContent"]["variants"][0]
    assert p.variants[0].price == v0["priceMoney"]["value"]
    assert p.variants[0].sku == v0["sku"]
    assert p.currency in ("USD", fx["website"].get("currency"))
    assert p.images and p.images[0] == raw["assetUrl"]


async def test_squarespace_catalog_finds_the_shop_and_pages_by_offset() -> None:
    root = "https://sq.test"
    item = json.dumps(
        {
            "website": {"currency": "GBP"},
            "items": [
                {
                    "id": "1",
                    "title": "Mug",
                    "fullUrl": "/shop/mug",
                    "assetUrl": "https://c/x.jpg",
                    "structuredContent": {
                        "_type": "StoreItem",
                        "variants": [
                            {
                                "sku": "M1",
                                "priceMoney": {"currency": "GBP", "value": "12.00"},
                                "unlimited": True,
                            }
                        ],
                    },
                }
            ],
            "pagination": {"nextPage": True, "nextPageOffset": 999},
        }
    )
    last = json.dumps(
        {
            "website": {"currency": "GBP"},
            "items": [
                {
                    "id": "2",
                    "title": "Bowl",
                    "fullUrl": "/shop/bowl",
                    "structuredContent": {
                        "_type": "StoreItem",
                        "variants": [
                            {
                                "sku": "B1",
                                "priceMoney": {"currency": "GBP", "value": "20.00"},
                                "qtyInStock": 3,
                            }
                        ],
                    },
                }
            ],
            "pagination": {"nextPage": False},
        }
    )
    f = _Fetcher(
        {
            f"{root}/shop?format=json": (200, item, {}),
            f"{root}/shop?format=json&offset=999": (200, last, {}),
        }
    )
    out = await catalog.squarespace_catalog(root + "/", f, limit=50)
    assert [p.title for p in out.products] == ["Mug", "Bowl"]
    assert out.products[0].currency == "GBP" and out.products[1].available is True
    assert out.pages_fetched == 2


# --------------------------------------------------------------------------
# Magento 2 storefront GraphQL
# --------------------------------------------------------------------------


def test_magento_graphql_item_normalises() -> None:
    raw = _j("magento_products.json")["data"]["products"]["items"][0]
    p = catalog.magento_product(raw, "https://www.bulk.com")
    assert p.platform == "magento" and p.title == raw["name"] and p.sku == raw["sku"]
    assert p.url == f"https://www.bulk.com/{raw['url_key']}.html"
    assert p.available is (raw["stock_status"] == "IN_STOCK")
    fp = raw["price_range"]["minimum_price"]["final_price"]
    assert p.price == f"{fp['value']:.2f}" and p.currency == fp["currency"]
    assert p.images and p.images[0] == raw["small_image"]["url"]


def test_the_magento_homepage_has_no_passive_marker_and_that_is_ok() -> None:
    """bulk.com carries none of Magento's real markers — the old detection only
    "worked" because `mage/` matched `image/`. Passive detection now correctly
    returns None, and /v1/products finds it by probing the GraphQL endpoint."""
    import json as _json

    from engine.platforms.detect import Platform, detect_platform

    h = _json.loads(load("homepages.json"))["magento"]
    assert detect_platform(h["head"], h["headers"]) is None

    # A store that DOES expose a real marker still detects passively.
    assert detect_platform('<script>require(["data-mage-init"])</script>', {}) == Platform.MAGENTO


# --------------------------------------------------------------------------
# Mastodon and Bluesky — the fediverse, public and unauthenticated
# --------------------------------------------------------------------------


def test_mastodon_status_normalises_and_strips_html() -> None:
    from engine.platforms import posts

    raw = _j("mastodon_statuses.json")[0]
    p = posts.mastodon_status(raw)
    assert p.platform == "mastodon" and p.url.startswith("http")
    assert "<" not in p.title
    assert "favourites_count" in p.extra


def test_a_mastodon_boost_takes_the_boosted_posts_content() -> None:
    from engine.platforms import posts

    boost = {
        "id": "9",
        "created_at": "2026-09-01T00:00:00Z",
        "content": "",
        "reblog": {
            "content": "<p>The original toot</p>",
            "url": "https://m.test/@a/1",
            "account": {"acct": "a@m.test"},
            "favourites_count": 12,
        },
    }
    p = posts.mastodon_status(boost)
    assert p.title == "The original toot" and p.author == "a@m.test"
    assert p.extra["boost"] is True and p.extra["favourites_count"] == 12


def test_bluesky_post_builds_the_web_url_from_the_at_uri() -> None:
    from engine.platforms import posts

    item = _j("bluesky_feed.json")["feed"][0]
    p = posts.bluesky_post(item)
    assert p is not None and p.platform == "bluesky"
    assert p.url.startswith("https://bsky.app/profile/")
    assert "/post/" in p.url
    assert "likeCount" in p.extra


def test_a_bluesky_item_without_a_handle_or_rkey_is_skipped() -> None:
    from engine.platforms import posts

    assert posts.bluesky_post({"post": {"uri": "", "author": {}, "record": {}}}) is None
