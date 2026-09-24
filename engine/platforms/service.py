"""One door: detect the platform from the homepage, then ask it the right way."""

from __future__ import annotations

from typing import Any

import structlog

from engine.platforms import catalog, posts
from engine.platforms.detect import Platform, detect_platform_for_url
from engine.platforms.models import Listing, Product

logger = structlog.get_logger(__name__)


class PlatformService:
    def __init__(self, scrape_service: Any) -> None:
        from engine.core.models import Tier

        self._service = scrape_service
        self._fetcher = scrape_service._fetchers.get(Tier.HTTP)

    async def homepage(self, url: str) -> tuple[str, dict[str, str], Platform | None]:
        """Homepage bytes and headers, tier 0 then tier 1, and the platform.

        Raw fetch, NOT service.scrape(): detection needs the bytes, not a
        verdict on them, and the content validator rightly calls a homepage an
        index page — TechCrunch's and a Substack's were both rejected as
        non-content, which took /v1/posts down with them (6 Sep 2026).
        """
        from engine.core.fetch.base import FetchRequest
        from engine.core.models import Tier

        html, headers = "", {}
        for tier in (Tier.HTTP, Tier.IMPERSONATE):
            fetcher = self._service._fetchers.get(tier)
            if fetcher is None:
                continue
            try:
                res = await fetcher.fetch(FetchRequest(url=url, timeout_ms=20_000))
            except Exception as exc:  # noqa: BLE001 - try the next rung
                logger.debug("platform_homepage_failed", url=url, tier=str(tier), error=str(exc))
                continue
            headers = dict(res.headers)
            html = res.text(limit=400_000)
            if res.status_code == 200 and html:
                break
        return html, headers, detect_platform_for_url(url, html, headers)

    async def products(self, url: str, limit: int = 1000) -> Listing:
        html, headers, platform = await self.homepage(url)
        if self._fetcher is None:
            return Listing(platform=platform, source="none", note="no tier-0 fetcher")
        if platform == Platform.SHOPIFY:
            return await catalog.shopify_catalog(url, self._fetcher, limit)
        if platform == Platform.WOOCOMMERCE:
            return await catalog.woo_catalog(url, self._fetcher, limit)
        if platform == Platform.SQUARESPACE:
            return await catalog.squarespace_catalog(url, self._fetcher, limit)
        if platform == Platform.MAGENTO:
            return await catalog.magento_catalog(url, self._fetcher, limit)
        # Magento has no reliable passive marker (the old `mage/` needle was a
        # false positive on `image/`). When nothing was detected, ask the
        # storefront GraphQL endpoint that DEFINES a Magento store — one probe,
        # only on an explicit /v1/products call, and only when detection came up
        # empty so a known platform is never second-guessed.
        if platform is None:
            probe = await catalog.magento_catalog(url, self._fetcher, limit)
            if probe.products:
                return probe
        return Listing(
            platform=platform,
            source="none",
            note=f"no public catalogue endpoint known for {platform or 'this site'}",
        )

    async def product_for_page(
        self, url: str, platform: Platform | None, html: str | None = None
    ) -> Product | None:
        """The structured product behind a product PAGE: from the store's JSON
        where it publishes one, from the page itself where it does not (Amazon)."""
        if platform == Platform.AMAZON:
            from engine.platforms.amazon import amazon_product, is_product_url

            if not html or not is_product_url(url):
                return None
            return amazon_product(html, url)
        if self._fetcher is None:
            return None
        if platform == Platform.SHOPIFY:
            return await catalog.shopify_single(url, self._fetcher)
        if platform == Platform.WOOCOMMERCE:
            return await catalog.woo_single(url, self._fetcher)
        return None

    @staticmethod
    def wants_page_html(url: str) -> bool:
        """Does attaching a product to this URL need the fetched page? Amazon has
        no JSON twin, so the route must ask for rawHtml before scraping."""
        from engine.platforms.amazon import is_product_url
        from engine.platforms.detect import detect_platform_for_url

        return detect_platform_for_url(url, "", {}) == Platform.AMAZON and is_product_url(url)

    async def posts(self, url: str, limit: int = 200) -> Listing:
        html, headers, platform = await self.homepage(url)
        if self._fetcher is None:
            return Listing(platform=platform, source="none", note="no tier-0 fetcher")
        f = self._fetcher
        if platform in (Platform.WORDPRESS, Platform.WOOCOMMERCE):
            got = await posts.wordpress_posts(url, f, limit)
            if got.posts:
                return got
        elif platform == Platform.SUBSTACK:
            got = await posts.substack_posts(url, f, limit)
            if got.posts:
                return got
        elif platform == Platform.SQUARESPACE:
            got = await posts.squarespace_posts(url, f, limit)
            if got.posts:
                return got
        elif platform == Platform.DISCOURSE:
            got = await posts.discourse_topics(url, f, limit)
            if got.posts:
                return got
        elif platform == Platform.MASTODON:
            return await posts.mastodon_posts(url, f, limit)
        elif platform == Platform.BLUESKY:
            return await posts.bluesky_posts(url, f, limit)
        # Ghost, Drupal, Webflow, Framer, plain sites — and any API that answered
        # with nothing: the feed is what every platform still speaks…
        got = await posts.feed_posts(
            url, f, homepage_html=html, limit=limit, platform=str(platform) if platform else None
        )
        if got.posts:
            return got
        # …except Webflow, which has none. The sitemap is the last honest
        # listing: URLs under the requested section, titles from the slugs.
        return await posts.sitemap_posts(
            url, f, limit=limit, platform=str(platform) if platform else None
        )
