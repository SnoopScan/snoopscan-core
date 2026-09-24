"""Posts, topics and articles from the API each platform publishes.

WordPress: /wp-json/wp/v2/posts (X-WP-TotalPages). Substack: /api/v1/archive
with offset. Squarespace: <collection>?format=json with pagination.nextPageOffset.
Discourse: /latest.json with more_topics_url. Everything else — Ghost, Drupal,
Webflow, plain sites — falls back to the RSS/Atom feed.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

import structlog

from engine.platforms.feeds import WELL_KNOWN, declared_feeds, looks_like_feed, parse_feed
from engine.platforms.models import Listing, Post

logger = structlog.get_logger(__name__)

MAX_PAGES = 20


def _root(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _html_text(s: str | None) -> str | None:
    return re.sub(r"<[^>]+>", " ", s).strip() if s else None


async def _json(fetcher: Any, url: str) -> tuple[Any, dict[str, str], int]:
    from engine.core.fetch.base import FetchRequest

    res = await fetcher.fetch(FetchRequest(url=url, timeout_ms=20_000))
    hdr = {k.lower(): v for k, v in res.headers.items()}
    if res.status_code != 200:
        return None, hdr, res.status_code
    try:
        return json.loads(res.text(limit=20_000_000)), hdr, 200
    except ValueError:
        return None, hdr, 200


# --------------------------------------------------------------------------
# WordPress
# --------------------------------------------------------------------------

WP_FIELDS = "id,date,modified,link,title,excerpt,content,author,tags"


def wp_post(raw: dict[str, Any]) -> Post:
    return Post(
        platform="wordpress",
        id=str(raw.get("id")),
        title=_html_text((raw.get("title") or {}).get("rendered")) or "",
        url=raw.get("link") or "",
        published_at=raw.get("date"),
        updated_at=raw.get("modified"),
        author=str(raw["author"]) if raw.get("author") is not None else None,
        excerpt=_html_text((raw.get("excerpt") or {}).get("rendered")),
        content_html=(raw.get("content") or {}).get("rendered") or None,
    )


async def wordpress_posts(url: str, fetcher: Any, limit: int = 200) -> Listing:
    root = _root(url)
    out = Listing(platform="wordpress", source="api")
    total_pages, page = 1, 1
    while page <= min(total_pages, MAX_PAGES) and len(out.posts) < limit:
        data, hdr, status = await _json(
            fetcher, f"{root}/wp-json/wp/v2/posts?per_page=100&page={page}&_fields={WP_FIELDS}"
        )
        out.pages_fetched += 1
        if status != 200 or not isinstance(data, list):
            out.note = f"wp-json answered {status}"
            break
        out.posts.extend(wp_post(p) for p in data if p.get("link"))
        try:
            total_pages = int(hdr.get("x-wp-totalpages") or total_pages)
            out.total = int(hdr.get("x-wp-total") or 0) or None
        except ValueError:
            pass
        if not data:
            break
        page += 1
    out.posts = out.posts[:limit]
    return out


# --------------------------------------------------------------------------
# Substack
# --------------------------------------------------------------------------


def substack_post(raw: dict[str, Any]) -> Post:
    return Post(
        platform="substack",
        id=str(raw.get("id")),
        title=raw.get("title") or "",
        url=raw.get("canonical_url") or "",
        published_at=raw.get("post_date"),
        excerpt=raw.get("subtitle") or raw.get("description"),
        extra={k: raw.get(k) for k in ("audience", "wordcount", "type") if raw.get(k) is not None},
    )


async def substack_posts(url: str, fetcher: Any, limit: int = 200) -> Listing:
    root = _root(url)
    out = Listing(platform="substack", source="api")
    offset = 0
    while len(out.posts) < limit and out.pages_fetched < MAX_PAGES:
        data, _, status = await _json(
            fetcher, f"{root}/api/v1/archive?sort=new&limit=50&offset={offset}"
        )
        out.pages_fetched += 1
        if status != 200 or not isinstance(data, list) or not data:
            break
        out.posts.extend(substack_post(p) for p in data if p.get("canonical_url"))
        offset += len(data)
        if len(data) < 50:
            break
    out.posts = out.posts[:limit]
    out.total = len(out.posts)
    return out


# --------------------------------------------------------------------------
# Squarespace
# --------------------------------------------------------------------------


def squarespace_item(raw: dict[str, Any], root: str) -> Post:
    return Post(
        platform="squarespace",
        id=str(raw.get("id")),
        title=raw.get("title") or "",
        url=urljoin(root, raw.get("fullUrl") or ""),
        published_at=str(raw.get("publishOn")) if raw.get("publishOn") else None,
        updated_at=str(raw.get("updatedOn")) if raw.get("updatedOn") else None,
        author=((raw.get("author") or {}).get("displayName")),
        excerpt=_html_text(raw.get("excerpt")),
        content_html=raw.get("body") or None,
        tags=list(raw.get("tags") or []) + list(raw.get("categories") or []),
    )


async def squarespace_posts(url: str, fetcher: Any, limit: int = 200) -> Listing:
    """`url` should be the collection (e.g. /blog); the homepage is tried too."""
    root = _root(url)
    out = Listing(platform="squarespace", source="api")
    path = urlsplit(url).path or "/"
    candidates = [path] if path not in ("", "/") else ["/blog", "/news", "/journal", "/"]
    for coll in candidates:
        offset: int | None = None
        while len(out.posts) < limit and out.pages_fetched < MAX_PAGES:
            q = "?format=json" + (f"&offset={offset}" if offset else "")
            data, _, status = await _json(fetcher, f"{root}{coll}{q}")
            out.pages_fetched += 1
            if status != 200 or not isinstance(data, dict):
                break
            items = data.get("items") or []
            out.posts.extend(squarespace_item(i, root) for i in items if i.get("fullUrl"))
            pag = data.get("pagination") or {}
            if not pag.get("nextPage") or not pag.get("nextPageOffset"):
                break
            offset = pag["nextPageOffset"]
        if out.posts:
            break
    out.posts = out.posts[:limit]
    out.total = len(out.posts)
    return out


# --------------------------------------------------------------------------
# Discourse
# --------------------------------------------------------------------------


def discourse_topic(raw: dict[str, Any], root: str) -> Post:
    return Post(
        platform="discourse",
        id=str(raw.get("id")),
        title=raw.get("title") or "",
        url=f"{root}/t/{raw.get('slug')}/{raw.get('id')}",
        published_at=raw.get("created_at"),
        updated_at=raw.get("last_posted_at") or raw.get("bumped_at"),
        excerpt=raw.get("excerpt"),
        # discuss.python.org sends tag OBJECTS ({id, name, slug}); others send names.
        tags=[t.get("name") if isinstance(t, dict) else str(t) for t in raw.get("tags") or []],
        extra={
            k: raw.get(k)
            for k in ("posts_count", "reply_count", "views", "like_count", "category_id")
            if raw.get(k) is not None
        },
    )


async def discourse_topics(url: str, fetcher: Any, limit: int = 200) -> Listing:
    root = _root(url)
    out = Listing(platform="discourse", source="api")
    next_url: str | None = f"{root}/latest.json"
    while next_url and len(out.posts) < limit and out.pages_fetched < MAX_PAGES:
        data, _, status = await _json(fetcher, next_url)
        out.pages_fetched += 1
        if status != 200 or not isinstance(data, dict):
            break
        tl = data.get("topic_list") or {}
        out.posts.extend(discourse_topic(t, root) for t in tl.get("topics") or [])
        more = tl.get("more_topics_url")
        if more and ".json" not in more:
            more = more.replace("?", ".json?", 1)
        next_url = urljoin(root, more) if more else None
    out.posts = out.posts[:limit]
    out.total = len(out.posts)
    return out


# --------------------------------------------------------------------------
# Feeds — the universal fallback
# --------------------------------------------------------------------------


async def feed_posts(
    url: str, fetcher: Any, homepage_html: str = "", limit: int = 200, platform: str | None = None
) -> Listing:
    from engine.core.fetch.base import FetchRequest

    root = _root(url)
    out = Listing(platform=platform, source="feed")
    tried: list[str] = []
    for feed_url in declared_feeds(homepage_html, root + "/") + [root + p for p in WELL_KNOWN]:
        if feed_url in tried:
            continue
        tried.append(feed_url)
        try:
            res = await fetcher.fetch(FetchRequest(url=feed_url, timeout_ms=15_000))
        except Exception as exc:  # noqa: BLE001 - a missing feed is normal
            logger.debug("feed_unavailable", url=feed_url, error=str(exc))
            continue
        out.pages_fetched += 1
        if res.status_code != 200 or not looks_like_feed(res.body):
            continue
        posts = parse_feed(res.text(limit=10_000_000), platform=platform or "feed")
        if posts:
            out.posts = posts[:limit]
            out.total = len(out.posts)
            out.note = feed_url
            return out
        if out.pages_fetched >= 6:
            break
    out.source = "none"
    return out


# --------------------------------------------------------------------------
# Sitemap — the last listing every site has
# --------------------------------------------------------------------------


async def sitemap_posts(
    url: str, fetcher: Any, limit: int = 200, platform: str | None = None
) -> Listing:
    """URLs under the requested path (or the whole site for a root URL), from
    the sitemap, with a title made from the slug. `source: "sitemap"` says a
    caller is getting URLs to read, not posts already read."""
    from engine.core.fetch.base import FetchRequest
    from engine.core.frontier.discovery import default_sitemap_urls, parse_sitemap

    root = _root(url)
    section = (urlsplit(url).path or "/").rstrip("/")
    out = Listing(platform=platform, source="sitemap")
    queue = default_sitemap_urls(root)
    seen: set[str] = set()
    # An index can list a dozen children on another host (Webflow's six live on
    # CloudFront, the blog in the sixth). Children whose name mentions the
    # section, or the usual words for editorial content, are read first.
    hint = section.strip("/").split("/")[-1] if section.strip("/") else ""

    def _rank(sm_url: str) -> int:
        name = sm_url.rsplit("/", 1)[-1].lower()
        if hint and hint in name:
            return 0
        if any(w in name for w in ("post", "blog", "article", "news", "manual", "page")):
            return 1
        return 2

    while queue and out.pages_fetched < 12 and len(out.posts) < limit:
        queue.sort(key=_rank)
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            res = await fetcher.fetch(FetchRequest(url=sm, timeout_ms=15_000))
        except Exception as exc:  # noqa: BLE001
            logger.debug("sitemap_unavailable", url=sm, error=str(exc))
            continue
        out.pages_fetched += 1
        if res.status_code != 200 or not res.body:
            continue
        pages, nested = parse_sitemap(res.text(limit=5_000_000))
        queue.extend(n for n in nested if n not in seen)
        for page in pages:
            path = urlsplit(page).path.rstrip("/")
            if section and not path.startswith(section + "/"):
                continue
            if not path or path == section:
                continue
            slug = path.rsplit("/", 1)[-1]
            title = slug.replace("-", " ").replace("_", " ").strip().capitalize()
            out.posts.append(Post(platform=platform or "sitemap", id=page, url=page, title=title))
            if len(out.posts) >= limit:
                break
    if not out.posts:
        out.source = "none"
    out.total = len(out.posts) or None
    return out


# --------------------------------------------------------------------------
# Mastodon — the fediverse's public account API (no auth)
# --------------------------------------------------------------------------

_MASTODON_ACCT_RE = re.compile(r"/@([A-Za-z0-9_]+)")


def mastodon_status(raw: dict[str, Any]) -> Post:
    # A boost (reblog) carries no content of its own; the post is the reblogged
    # one underneath. Follow it so a timeline of boosts is not a wall of blanks.
    boosted = raw.get("reblog")
    source = boosted if isinstance(boosted, dict) else raw
    acct = source.get("account") or {}
    text = _html_text(source.get("content"))
    return Post(
        platform="mastodon",
        id=str(raw.get("id")),
        title=text or "(media post)",
        url=source.get("url") or source.get("uri") or "",
        published_at=raw.get("created_at"),
        author=acct.get("acct") or acct.get("username"),
        excerpt=text,
        content_html=source.get("content") or None,
        tags=[t.get("name") for t in source.get("tags") or [] if t.get("name")],
        extra={
            k: source.get(k)
            for k in ("favourites_count", "reblogs_count", "replies_count", "visibility")
            if source.get(k) is not None
        }
        | ({"boost": True} if isinstance(boosted, dict) else {}),
    )


async def mastodon_posts(url: str, fetcher: Any, limit: int = 200) -> Listing:
    """`https://<instance>/@user` → lookup the account, then its public statuses."""
    root = _root(url)
    out = Listing(platform="mastodon", source="api")
    m = _MASTODON_ACCT_RE.search(urlsplit(url).path or "")
    if not m:
        out.source = "none"
        out.note = "no @account in the URL"
        return out
    acct, _, status = await _json(fetcher, f"{root}/api/v1/accounts/lookup?acct={m.group(1)}")
    out.pages_fetched += 1
    if status != 200 or not isinstance(acct, dict) or not acct.get("id"):
        out.source = "none"
        out.note = f"account lookup answered {status}"
        return out
    max_id: str | None = None
    while len(out.posts) < limit and out.pages_fetched < MAX_PAGES:
        page_size = min(40, limit - len(out.posts))
        q = f"?limit={page_size}&exclude_replies=true" + (f"&max_id={max_id}" if max_id else "")
        data, _, status = await _json(fetcher, f"{root}/api/v1/accounts/{acct['id']}/statuses{q}")
        out.pages_fetched += 1
        if status != 200 or not isinstance(data, list) or not data:
            break
        out.posts.extend(mastodon_status(x) for x in data if x.get("url") or x.get("uri"))
        max_id = str(data[-1].get("id"))
    out.posts = out.posts[:limit]
    out.total = len(out.posts)
    return out


# --------------------------------------------------------------------------
# Bluesky — the public AppView, no auth
# --------------------------------------------------------------------------

_BLUESKY_API = "https://public.api.bsky.app"
_BSKY_PROFILE_RE = re.compile(r"/profile/([^/?#]+)")


def bluesky_post(item: dict[str, Any]) -> Post | None:
    post = item.get("post") or item
    rec = post.get("record") or {}
    uri = post.get("uri") or ""
    author = post.get("author") or {}
    handle = author.get("handle") or ""
    rkey = uri.rsplit("/", 1)[-1] if uri else ""
    text = rec.get("text") or ""
    if not (handle and rkey):
        return None
    return Post(
        platform="bluesky",
        id=uri,
        title=text[:120] or "(no text)",
        url=f"https://bsky.app/profile/{handle}/post/{rkey}",
        published_at=rec.get("createdAt"),
        author=handle,
        excerpt=text or None,
        tags=[
            f["features"][0].get("tag")
            for f in rec.get("facets") or []
            if f.get("features") and f["features"][0].get("$type", "").endswith("tag")
        ],
        extra={
            k: post.get(k)
            for k in ("likeCount", "repostCount", "replyCount", "quoteCount")
            if post.get(k) is not None
        },
    )


async def bluesky_posts(url: str, fetcher: Any, limit: int = 200) -> Listing:
    """`https://bsky.app/profile/<handle>` → the account's public feed."""
    out = Listing(platform="bluesky", source="api")
    m = _BSKY_PROFILE_RE.search(urlsplit(url).path or "")
    if not m:
        out.source = "none"
        out.note = "no /profile/<handle> in the URL"
        return out
    actor = m.group(1)
    cursor: str | None = None
    while len(out.posts) < limit and out.pages_fetched < MAX_PAGES:
        page_size = min(100, limit - len(out.posts))
        q = f"?actor={actor}&limit={page_size}" + (f"&cursor={cursor}" if cursor else "")
        data, _, status = await _json(
            fetcher, f"{_BLUESKY_API}/xrpc/app.bsky.feed.getAuthorFeed{q}"
        )
        out.pages_fetched += 1
        if status != 200 or not isinstance(data, dict):
            break
        feed = data.get("feed") or []
        out.posts.extend(p for p in (bluesky_post(x) for x in feed) if p is not None)
        cursor = data.get("cursor")
        if not cursor or not feed:
            break
    out.posts = out.posts[:limit]
    out.total = len(out.posts)
    return out
