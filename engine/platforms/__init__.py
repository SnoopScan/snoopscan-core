"""Platform shortcuts — where a site hands its data over as JSON or a feed.

PROPRIETARY. Knowing that a Shopify store publishes its whole catalogue at
/products.json, that WooCommerce's Store API answers without a key, that a
Squarespace page returns JSON with ?format=json, is the difference between one
request and a crawl. Measured live, 6 Sep 2026, against a dozen platforms; the
truth table is in docs/internal/field-reports.md and the fixtures under
engine/tests/fixtures/platforms are the real responses.
"""

from engine.platforms.detect import Platform, detect_platform
from engine.platforms.models import Post, Product

__all__ = ["Platform", "Post", "Product", "detect_platform"]
