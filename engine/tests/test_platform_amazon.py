"""Amazon product pages, from the two real pages captured at tier 0 (6 Sep 2026):
one out of stock (no price block at all), one in stock but served without a
price to the honest client. Every missing field is None, never invented."""

from __future__ import annotations

from pathlib import Path

from engine.core.extract.classify import PageType, classify
from engine.platforms.amazon import amazon_product, asin_of, is_product_url
from engine.tests.fixtures.platforms import load

FIX = Path(__file__).parent / "fixtures" / "platforms"


def test_asin_is_read_from_every_url_shape() -> None:
    assert asin_of("https://www.amazon.com/dp/B0CX23V2ZK") == "B0CX23V2ZK"
    assert (
        asin_of("https://www.amazon.co.uk/Apple-MacBook/dp/B0CX23V2ZK/ref=sr_1_1") == "B0CX23V2ZK"
    )
    assert asin_of("https://www.amazon.de/gp/product/B0CX23V2ZK") == "B0CX23V2ZK"
    assert asin_of("https://www.amazon.com/s?k=laptop") is None
    assert is_product_url("https://www.amazon.com/dp/B0CX23V2ZK")


def test_the_real_out_of_stock_page_yields_what_it_carries_and_nothing_more() -> None:
    html = load("amazon_product_page.html")
    p = amazon_product(html, "https://www.amazon.com/dp/B0CX23V2ZK")
    assert p is not None and p.platform == "amazon" and p.id == "B0CX23V2ZK"
    assert "MacBook Air" in p.title
    assert p.vendor == "Apple"
    assert p.description_html and "<li>" in p.description_html, "the feature bullets"
    assert p.images and all(i.startswith("http") for i in p.images)
    assert p.price is None and p.currency is None, "no price block: none is reported"
    assert p.available is False, "#outOfStock is on the page"
    assert p.rating is None and p.review_count is None


def test_a_page_with_a_price_block_reports_the_price() -> None:
    html = (
        '<html><body><span id="productTitle"> Widget </span>'
        '<div id="corePriceDisplay_desktop_feature_div"><span class="priceToPay">'
        '<span class="a-offscreen">$1,299.00</span></span>'
        '<span class="basisPrice"><span class="a-offscreen">$1,499.00</span></span></div>'
        '<div id="availability"><span>In Stock</span></div>'
        '<span id="acrPopover"><span class="a-icon-alt">4.7 out of 5 stars</span></span>'
        '<span id="acrCustomerReviewText">12,345 ratings</span>'
        "</body></html>"
    )
    p = amazon_product(html, "https://www.amazon.com/dp/B000000001")
    assert p is not None
    assert p.price == "1299.00" and p.compare_at_price == "1499.00" and p.currency == "USD"
    assert p.available is True and p.rating == 4.7 and p.review_count == 12345


def test_european_prices_keep_their_decimal() -> None:
    html = (
        '<span id="productTitle">Ding</span>'
        '<span class="a-price"><span class="a-offscreen">1.299,00 €</span></span>'
    )
    p = amazon_product(html, "https://www.amazon.de/dp/B000000002")
    assert p is not None and p.price == "1299.00" and p.currency == "EUR"


def test_not_a_product_page_is_none() -> None:
    assert (
        amazon_product("<html><body><p>Robot check</p></body></html>", "https://www.amazon.com/")
        is None
    )


def test_the_real_amazon_page_classifies_as_a_product_not_a_forum() -> None:
    """The earlier smoke printed '## Post 1' for an Amazon listing."""
    html = load("amazon_product_page.html")
    cls = classify(html)
    assert cls.page_type == PageType.PRODUCT, cls.scores


def test_an_unavailable_item_with_an_alternative_offers_cart_button_is_not_available() -> None:
    html = (
        '<span id="productTitle">Gone</span>'
        '<script>{"currentlyUnavailableMessage":"Currently unavailable."}</script>'
        '<div class="_export-alternative-card">'
        '<span id="add-to-cart-button">Add to Cart</span></div>'
    )
    p = amazon_product(html, "https://www.amazon.com/dp/B000000003")
    assert p is not None and p.available is False


def test_the_browser_tier_page_carries_the_full_buybox() -> None:
    """Captured 6 Sep 2026: the same ASIN that shows no price to the honest
    tier-0 client shows everything to the browser — geo-priced in GBP for this
    IP, which the extractor must take from the symbol, not the .com host."""
    html = load("amazon_product_instock_browser.html")
    p = amazon_product(html, "https://www.amazon.com/dp/B00FLYWNYQ")
    assert p is not None and "Instant Pot" in p.title
    assert p.price == "66.59" and p.currency == "GBP"
    assert p.available is True
    assert p.rating == 4.7 and p.review_count == 180256
    assert p.images


def test_the_honest_tier_page_for_the_same_kind_of_item_has_no_price() -> None:
    """Not a bug in the extractor: Amazon withholds the buybox from that client."""
    html = load("amazon_product_page.html")
    p = amazon_product(html, "https://www.amazon.com/dp/B0CX23V2ZK")
    assert p is not None and p.price is None and p.rating is None
