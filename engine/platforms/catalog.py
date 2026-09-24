"""Product catalogues, from the endpoint each store platform publishes itself.

Shopify: /products.json?limit=250&page=N until a page comes back short, and
/products/<handle>.json for one. WooCommerce: the Store API,
/wp-json/wc/store/v1/products?per_page=100&page=N, paged by X-WP-TotalPages.
Both unauthenticated, both tier 0, both measured on live stores (fixtures/).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from engine.platforms.models import Listing, Product, Variant

SHOPIFY_PAGE = 250
WOO_PAGE = 100
MAX_PAGES = 40  # 10,000 Shopify products or 4,000 Woo; a cap, not a target


def _root(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _strip_html(html: str | None) -> str | None:
    if not html:
        return None
    return re.sub(r"<[^>]+>", " ", html).strip() or None


# --------------------------------------------------------------------------
# Shopify
# --------------------------------------------------------------------------


def shopify_product(raw: dict[str, Any], root: str) -> Product:
    variants = [
        Variant(
            id=str(v.get("id")) if v.get("id") is not None else None,
            title=v.get("title"),
            sku=v.get("sku") or None,
            price=v.get("price"),
            compare_at_price=v.get("compare_at_price"),
            available=v.get("available"),
        )
        for v in raw.get("variants") or []
    ]
    first = variants[0] if variants else None
    handle = raw.get("handle")
    return Product(
        platform="shopify",
        id=str(raw.get("id")),
        title=raw.get("title") or handle or "",
        url=f"{root}/products/{handle}" if handle else None,
        handle=handle,
        description_html=raw.get("body_html") or None,
        vendor=raw.get("vendor") or None,
        product_type=raw.get("product_type") or None,
        price=first.price if first else None,
        compare_at_price=first.compare_at_price if first else None,
        currency=None,  # Shopify's public JSON omits it; the store's locale decides
        available=any(v.available for v in variants) if variants else None,
        sku=first.sku if first else None,
        images=[i.get("src") for i in raw.get("images") or [] if i.get("src")],
        variants=variants,
        tags=list(raw.get("tags") or [])
        if isinstance(raw.get("tags"), list)
        else [t.strip() for t in str(raw.get("tags") or "").split(",") if t.strip()],
        created_at=raw.get("created_at"),
        updated_at=raw.get("updated_at"),
    )


async def shopify_catalog(url: str, fetcher: Any, limit: int = 1000) -> Listing:
    from engine.core.fetch.base import FetchRequest

    root = _root(url)
    out = Listing(platform="shopify", source="api")
    for page in range(1, MAX_PAGES + 1):
        listing_url = f"{root}/products.json?limit={SHOPIFY_PAGE}&page={page}"
        res = await fetcher.fetch(FetchRequest(url=listing_url, timeout_ms=20_000))
        out.pages_fetched += 1
        if res.status_code != 200:
            out.note = f"products.json answered {res.status_code}"
            break
        try:
            items = json.loads(res.text(limit=20_000_000)).get("products") or []
        except (ValueError, AttributeError):
            out.note = "products.json was not JSON"
            break
        out.products.extend(shopify_product(p, root) for p in items)
        if len(items) < SHOPIFY_PAGE or len(out.products) >= limit:
            break
    out.products = out.products[:limit]
    out.total = len(out.products)
    return out


async def shopify_single(url: str, fetcher: Any) -> Product | None:
    """/products/<handle> → /products/<handle>.json."""
    from engine.core.fetch.base import FetchRequest

    m = re.search(r"/products/([a-z0-9\-_%.]+)", urlsplit(url).path, re.I)
    if not m:
        return None
    root = _root(url)
    res = await fetcher.fetch(
        FetchRequest(url=f"{root}/products/{m.group(1)}.json", timeout_ms=15_000)
    )
    if res.status_code != 200:
        return None
    try:
        raw = json.loads(res.text(limit=5_000_000)).get("product")
    except (ValueError, AttributeError):
        return None
    return shopify_product(raw, root) if raw else None


# --------------------------------------------------------------------------
# WooCommerce (Store API)
# --------------------------------------------------------------------------


def woo_product(raw: dict[str, Any]) -> Product:
    prices = raw.get("prices") or {}
    minor = int(prices.get("currency_minor_unit") or 2)

    def money(v: Any) -> str | None:
        # Store API prices are integer strings in minor units: "19500" → "195.00".
        if v in (None, ""):
            return None
        try:
            return f"{int(v) / (10**minor):.{minor}f}"
        except (TypeError, ValueError):
            return str(v)

    sale = money(prices.get("sale_price"))
    regular = money(prices.get("regular_price"))
    return Product(
        platform="woocommerce",
        id=str(raw.get("id")),
        title=raw.get("name") or "",
        url=raw.get("permalink"),
        handle=raw.get("slug"),
        description_html=raw.get("description") or raw.get("short_description") or None,
        vendor=(raw.get("brands") or [{}])[0].get("name") if raw.get("brands") else None,
        product_type=(
            (raw.get("categories") or [{}])[0].get("name") if raw.get("categories") else None
        ),
        price=money(prices.get("price")),
        compare_at_price=regular if raw.get("on_sale") and sale != regular else None,
        currency=prices.get("currency_code"),
        available=raw.get("is_in_stock"),
        sku=raw.get("sku") or None,
        images=[i.get("src") for i in raw.get("images") or [] if i.get("src")],
        variants=[],
        tags=[t.get("name") for t in raw.get("tags") or [] if t.get("name")],
        rating=float(raw["average_rating"])
        if raw.get("average_rating") not in (None, "", "0")
        else None,
        review_count=int(raw["review_count"])
        if raw.get("review_count") not in (None, "")
        else None,
    )


async def woo_catalog(url: str, fetcher: Any, limit: int = 1000) -> Listing:
    from engine.core.fetch.base import FetchRequest

    root = _root(url)
    out = Listing(platform="woocommerce", source="api")
    total_pages = 1
    page = 1
    while page <= min(total_pages, MAX_PAGES):
        res = await fetcher.fetch(
            FetchRequest(
                url=f"{root}/wp-json/wc/store/v1/products?per_page={WOO_PAGE}&page={page}",
                timeout_ms=20_000,
            )
        )
        out.pages_fetched += 1
        if res.status_code != 200:
            out.note = f"store api answered {res.status_code}"
            break
        try:
            items = json.loads(res.text(limit=20_000_000))
        except ValueError:
            out.note = "store api was not JSON"
            break
        if not isinstance(items, list):
            break
        out.products.extend(woo_product(p) for p in items)
        hdr = {k.lower(): v for k, v in res.headers.items()}
        try:
            total_pages = int(hdr.get("x-wp-totalpages") or total_pages)
            out.total = int(hdr.get("x-wp-total") or 0) or None
        except ValueError:
            pass
        if len(out.products) >= limit or not items:
            break
        page += 1
    out.products = out.products[:limit]
    out.total = out.total or len(out.products)
    return out


async def woo_single(url: str, fetcher: Any) -> Product | None:
    """/product/<slug>/ → Store API ?slug=<slug>."""
    from engine.core.fetch.base import FetchRequest

    m = re.search(r"/product/([a-z0-9\-_%.]+)", urlsplit(url).path, re.I)
    if not m:
        return None
    slug_url = f"{_root(url)}/wp-json/wc/store/v1/products?slug={m.group(1)}"
    res = await fetcher.fetch(FetchRequest(url=slug_url, timeout_ms=15_000))
    if res.status_code != 200:
        return None
    try:
        items = json.loads(res.text(limit=5_000_000))
    except ValueError:
        return None
    return woo_product(items[0]) if isinstance(items, list) and items else None


# --------------------------------------------------------------------------
# Squarespace commerce — a shop collection is ?format=json like any other
# --------------------------------------------------------------------------

SQUARESPACE_SHOP_PATHS = ("/shop", "/store", "/products", "/collections/all", "/shop-all")


def squarespace_product(
    raw: dict[str, Any], root: str, currency_hint: str | None = None
) -> Product:
    sc = raw.get("structuredContent") or {}
    variants: list[Variant] = []
    for v in sc.get("variants") or []:
        money = v.get("priceMoney") or {}
        sale = (v.get("salePriceMoney") or {}) if v.get("onSale") else {}
        variants.append(
            Variant(
                id=str(v.get("id")) if v.get("id") is not None else None,
                title=", ".join(f"{k}: {val}" for k, val in (v.get("attributes") or {}).items())
                or None,
                sku=v.get("sku") or None,
                price=(sale.get("value") if sale else None) or money.get("value"),
                compare_at_price=money.get("value") if sale else None,
                available=bool(v.get("unlimited")) or (v.get("qtyInStock") or 0) > 0,
            )
        )
    first = variants[0] if variants else None
    money = sc.get("priceMoney") or {}
    images = [raw["assetUrl"]] if raw.get("assetUrl") else []
    images += [i.get("assetUrl") for i in raw.get("items") or [] if i.get("assetUrl")]
    return Product(
        platform="squarespace",
        id=str(raw.get("id")),
        title=raw.get("title") or "",
        url=urljoin(root, raw.get("fullUrl") or ""),
        handle=raw.get("urlId"),
        description_html=raw.get("body") or raw.get("excerpt") or None,
        price=(first.price if first else None) or money.get("value"),
        compare_at_price=first.compare_at_price if first else None,
        currency=money.get("currency") or currency_hint,
        available=any(v.available for v in variants) if variants else None,
        sku=first.sku if first else None,
        images=list(dict.fromkeys(images)),
        variants=variants,
        tags=list(raw.get("tags") or []) + list(raw.get("categories") or []),
        created_at=str(raw.get("addedOn")) if raw.get("addedOn") else None,
        updated_at=str(raw.get("updatedOn")) if raw.get("updatedOn") else None,
    )


async def squarespace_catalog(url: str, fetcher: Any, limit: int = 1000) -> Listing:
    """The shop collection at the requested path, else the usual shop paths.
    Pages by `pagination.nextPageOffset`, exactly as the blog does."""
    from engine.core.fetch.base import FetchRequest

    root = _root(url)
    out = Listing(platform="squarespace", source="api")
    path = (urlsplit(url).path or "/").rstrip("/")
    candidates = ([path] if path else []) + [p for p in SQUARESPACE_SHOP_PATHS if p != path]
    for coll in candidates:
        offset: int | None = None
        found_here = 0
        while len(out.products) < limit and out.pages_fetched < MAX_PAGES:
            q = "?format=json" + (f"&offset={offset}" if offset else "")
            res = await fetcher.fetch(FetchRequest(url=f"{root}{coll}{q}", timeout_ms=20_000))
            out.pages_fetched += 1
            if res.status_code != 200:
                break
            try:
                data = json.loads(res.text(limit=20_000_000))
            except ValueError:
                break
            if not isinstance(data, dict):
                break
            currency = ((data.get("website") or {}).get("currency")) or None
            items = [
                i
                for i in data.get("items") or []
                if (i.get("structuredContent") or {}).get("_type") == "StoreItem"
                or i.get("recordTypeLabel") == "store-item"
            ]
            out.products.extend(squarespace_product(i, root, currency) for i in items)
            found_here += len(items)
            pag = data.get("pagination") or {}
            if not pag.get("nextPage") or not pag.get("nextPageOffset"):
                break
            offset = pag["nextPageOffset"]
        if found_here:
            break
    out.products = out.products[:limit]
    out.total = len(out.products)
    if not out.products:
        out.source = "none"
        out.note = "no store collection answered ?format=json with StoreItems"
    return out


# --------------------------------------------------------------------------
# Magento 2 — the storefront GraphQL endpoint answers GET, unauthenticated
# --------------------------------------------------------------------------

MAGENTO_PAGE = 100
_MAGENTO_QUERY = (
    '{ products(search: "%s", pageSize: %d, currentPage: %d) { total_count '
    "page_info { total_pages current_page } items { name sku url_key stock_status "
    "small_image { url } price_range { minimum_price { final_price { value currency } "
    "regular_price { value currency } } } } } }"
)


def magento_product(raw: dict[str, Any], root: str) -> Product:
    pr = (raw.get("price_range") or {}).get("minimum_price") or {}
    final = pr.get("final_price") or {}
    regular = pr.get("regular_price") or {}
    fv, rv = final.get("value"), regular.get("value")
    return Product(
        platform="magento",
        id=raw.get("sku") or raw.get("url_key") or "",
        title=raw.get("name") or "",
        url=f"{root}/{raw['url_key']}.html" if raw.get("url_key") else None,
        handle=raw.get("url_key"),
        price=f"{fv:.2f}"
        if isinstance(fv, (int, float))
        else (str(fv) if fv is not None else None),
        compare_at_price=(
            f"{rv:.2f}"
            if isinstance(rv, (int, float)) and rv and fv is not None and rv > fv
            else None
        ),
        currency=final.get("currency") or regular.get("currency"),
        available=(raw.get("stock_status") == "IN_STOCK") if raw.get("stock_status") else None,
        sku=raw.get("sku") or None,
        images=[(raw.get("small_image") or {}).get("url")]
        if (raw.get("small_image") or {}).get("url")
        else [],
    )


async def magento_catalog(url: str, fetcher: Any, limit: int = 1000, search: str = "") -> Listing:
    from urllib.parse import quote

    from engine.core.fetch.base import FetchRequest

    root = _root(url)
    out = Listing(platform="magento", source="api")
    page, total_pages = 1, 1
    while page <= min(total_pages, MAX_PAGES) and len(out.products) < limit:
        q = quote(_MAGENTO_QUERY % (search.replace('"', ""), MAGENTO_PAGE, page))
        res = await fetcher.fetch(FetchRequest(url=f"{root}/graphql?query={q}", timeout_ms=25_000))
        out.pages_fetched += 1
        if res.status_code != 200:
            out.note = f"graphql answered {res.status_code}"
            break
        try:
            data = json.loads(res.text(limit=20_000_000))
        except ValueError:
            out.note = "graphql was not JSON"
            break
        products = (data.get("data") or {}).get("products") or {}
        items = products.get("items") or []
        out.products.extend(magento_product(p, root) for p in items)
        out.total = products.get("total_count") or out.total
        total_pages = int((products.get("page_info") or {}).get("total_pages") or total_pages)
        if not items:
            break
        page += 1
    out.products = out.products[:limit]
    out.total = out.total or len(out.products)
    return out
