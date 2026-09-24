"""Amazon product pages, read from the DOM we were served.

There is no JSON endpoint. What the page contains depends on the client: the
honest tier-0 client is given title, bullets, brand, images and ASIN, and
sometimes a price; the impersonation tier is shown a captcha the honest one is
not (6 Sep 2026). So this extracts from whatever HTML the ladder fetched, and
every field is None when the page did not carry it — a missing price means
"Amazon did not show it to this client", never a guess.
"""

from __future__ import annotations

import contextlib
import json
import re
from urllib.parse import urlsplit

from selectolax.parser import HTMLParser

from engine.platforms.models import Product

_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", re.I)
_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePrice_feature_div .a-offscreen",
    "#apex_desktop .a-offscreen",
    ".priceToPay .a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "#priceblock_saleprice",
    "#price_inside_buybox",
    "#tp_price_block_total_price_ww .a-offscreen",
    ".a-price .a-offscreen",
)
_LIST_PRICE_SELECTORS = (
    "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen",
    ".basisPrice .a-offscreen",
    "#listPrice",
    "#priceblock_listprice",
)
# "$1,299.00", "US$ 12", "1.299,00 €", "12,99€": symbol before or after.
_MONEY_RE = re.compile(
    r"(?:(?P<pre>[£$€¥₹]|[A-Z]{3}\s?\$?)\s?(?P<amt>[\d.,]+)"
    r"|(?P<amt2>[\d.,]+)\s?(?P<post>[£$€¥₹]|[A-Z]{3}))"
)
_CURRENCY_BY_SYMBOL = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "₹": "INR"}
_CURRENCY_BY_HOST = {
    "co.uk": "GBP",
    "de": "EUR",
    "fr": "EUR",
    "it": "EUR",
    "es": "EUR",
    "nl": "EUR",
    "ca": "CAD",
    "com.au": "AUD",
    "co.jp": "JPY",
    "in": "INR",
    "com": "USD",
    "com.mx": "MXN",
    "com.br": "BRL",
    "se": "SEK",
    "pl": "PLN",
    "ae": "AED",
    "sg": "SGD",
}


def asin_of(url: str) -> str | None:
    m = _ASIN_RE.search(url)
    return m.group(1).upper() if m else None


def is_product_url(url: str) -> bool:
    return asin_of(url) is not None


def _text(tree: HTMLParser, *selectors: str) -> str | None:
    for sel in selectors:
        node = tree.css_first(sel)
        if node is not None:
            txt = " ".join((node.text() or "").split())
            if txt:
                return txt
    return None


def _money(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    m = _MONEY_RE.search(text)
    if not m:
        return None, None
    sym = (m.group("pre") or m.group("post") or "").strip()
    raw = m.group("amt") or m.group("amt2") or ""
    # "1.234,56" (EU) vs "1,234.56": whichever separator is LAST is the decimal;
    # a lone comma with two digits after it ("12,99") is a decimal too.
    if "," in raw and "." in raw:
        amount = (
            raw.replace(".", "").replace(",", ".")
            if raw.rfind(",") > raw.rfind(".")
            else raw.replace(",", "")
        )
    elif "," in raw and re.fullmatch(r"\d+,\d{2}", raw):
        amount = raw.replace(",", ".")
    else:
        amount = raw.replace(",", "")
    try:
        float(amount)
    except ValueError:
        return None, None
    return amount, _CURRENCY_BY_SYMBOL.get(sym, sym if len(sym) == 3 else None)


def amazon_product(html: str, url: str) -> Product | None:
    tree = HTMLParser(html)
    title = _text(tree, "#productTitle", "#title span", "h1#title")
    asin = asin_of(url) or (
        (tree.css_first("input#ASIN") or tree.css_first("[data-asin]"))
        and (
            (tree.css_first("input#ASIN") or tree.css_first("[data-asin]")).attributes.get("value")
            or (tree.css_first("[data-asin]") or tree.css_first("input#ASIN")).attributes.get(
                "data-asin"
            )
        )
    )
    if not title and not asin:
        return None

    host = (urlsplit(url).hostname or "").lower()
    tld = host.split("amazon.", 1)[-1] if "amazon." in host else ""
    price, currency = _money(_text(tree, *_PRICE_SELECTORS))
    list_price, _ = _money(_text(tree, *_LIST_PRICE_SELECTORS))
    currency = currency or (_CURRENCY_BY_HOST.get(tld) if price else None)

    bullets = [
        " ".join((li.text() or "").split())
        for li in tree.css("#feature-bullets li span.a-list-item, #feature-bullets li")
    ]
    bullets = [b for b in dict.fromkeys(bullets) if b and "see more" not in b.lower()]

    images: list[str] = []
    img = tree.css_first("#landingImage, #imgTagWrapperId img, #imgBlkFront")
    if img is not None:
        dyn = img.attributes.get("data-a-dynamic-image")
        if dyn:
            with contextlib.suppress(ValueError):
                images.extend(json.loads(dyn).keys())
        for attr in ("data-old-hires", "src"):
            v = img.attributes.get(attr)
            if v and v.startswith("http") and v not in images:
                images.append(v)

    availability = _text(tree, "#availability span", "#availability", "#outOfStock")
    available: bool | None = None
    if availability:
        low = availability.lower()
        available = not any(k in low for k in ("unavailable", "out of stock", "not available"))
    elif "currentlyUnavailableMessage" in html or "No featured offers" in html:
        # The buybox data says there is nothing to buy. Checked BEFORE looking
        # for a buy button, because the "Add to Cart" on an unavailable page
        # belongs to an alternative-offer card for a different item.
        available = False
    elif tree.css_first("#add-to-cart-button, #buy-now-button") is not None:
        available = True

    rating_txt = _text(
        tree,
        "#acrPopover .a-icon-alt",
        "span[data-hook='rating-out-of-text']",
        "#averageCustomerReviews .a-icon-alt",
    )
    rating = None
    if rating_txt:
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*out of", rating_txt)
        rating = float(m.group(1).replace(",", ".")) if m else None
    reviews_txt = _text(tree, "#acrCustomerReviewText", "[data-hook='total-review-count']")
    review_count = None
    if reviews_txt:
        m = re.search(r"([\d,.]+)", reviews_txt)
        if m:
            try:
                review_count = int(m.group(1).replace(",", "").replace(".", ""))
            except ValueError:
                review_count = None

    brand = _text(tree, "#bylineInfo", "a#brand", "#brand")
    if brand:
        brand = re.sub(r"^(Visit the |Brand: )", "", brand).replace(" Store", "").strip() or None

    root = f"{urlsplit(url).scheme}://{host}"
    return Product(
        platform="amazon",
        id=asin or (title or "")[:40],
        title=title or asin or "",
        url=f"{root}/dp/{asin}" if asin else url,
        handle=asin,
        description_html=("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
        if bullets
        else None,
        vendor=brand,
        product_type=_text(tree, "#wayfinding-breadcrumbs_feature_div li:last-child a"),
        price=price,
        compare_at_price=list_price if list_price and list_price != price else None,
        currency=currency,
        available=available,
        sku=asin,
        images=images,
        rating=rating,
        review_count=review_count,
    )


__all__ = ["amazon_product", "asin_of", "is_product_url"]
