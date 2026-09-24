"""URL normalisation, hashing and registrable-domain extraction.

Normalisation order is defined in 07-orchestration.md section 3 and must not be
reordered — the resulting hash is a database dedup key.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tldextract
import yaml

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Bundled suffix list, so there is no network call at import time.
# Private suffixes are included deliberately: on a hosting suffix like
# github.io or vercel.app, `project.github.io` is a distinct entity and
# collapsing every such site to `github.io` would merge unrelated companies
# into one row during lead-gen dedup.
_extract = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)


def _load_tracking_params() -> tuple[frozenset[str], tuple[str, ...]]:
    """Tracking params live in a data file — the list grows without a deploy."""
    path = _DATA_DIR / "tracking_params.yaml"
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except OSError:
        return frozenset(), ()
    exact = frozenset(str(p).lower() for p in raw.get("exact", []))
    prefixes = tuple(str(p).lower() for p in raw.get("prefixes", []))
    return exact, prefixes


TRACKING_EXACT, TRACKING_PREFIXES = _load_tracking_params()

DEFAULT_PORTS = {"http": "80", "https": "443"}

# Extensions never worth fetching in a crawl unless explicitly requested.
SKIP_EXTENSIONS = frozenset(
    {
        ".zip",
        ".gz",
        ".tar",
        ".bz2",
        ".7z",
        ".rar",
        ".exe",
        ".dmg",
        ".pkg",
        ".msi",
        ".deb",
        ".rpm",
        ".apk",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".mkv",
        ".wav",
        ".flac",
        ".webm",
        ".iso",
        ".bin",
        ".dll",
        ".so",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".css",
        ".js",
        ".map",
        # Images. WordPress sitemaps list every uploaded image alongside the
        # posts, so without these a crawl spends its whole budget downloading
        # megabytes of JPEGs and extracting nothing from them.
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".svg",
        ".webp",
        ".bmp",
        ".ico",
        ".tif",
        ".tiff",
        ".avif",
        ".heic",
        ".heif",
        # .pdf is deliberately NOT skipped — it is a supported parser target
        # (`parsers: ["pdf"]` in the scrape contract).
    }
)


def is_tracking_param(name: str) -> bool:
    lower = name.lower()
    return lower in TRACKING_EXACT or any(lower.startswith(p) for p in TRACKING_PREFIXES)


def normalize_url(url: str, *, drop_query: bool = False) -> str:
    """Canonical form used for deduplication.

    Steps, in the order mandated by the spec:
      1. lowercase scheme and host
      2. strip default port
      3. remove fragment
      4. sort query params alphabetically
      5. drop tracking params
      6. strip trailing slash unless path is root
      7. decode unnecessary percent-encoding (handled by urlsplit/urlencode)
      8. drop the query entirely when `drop_query`
    """
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        return url.strip()

    netloc = host
    port = parts.port
    if port is not None and str(port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    if drop_query:
        query = ""
    else:
        pairs = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not is_tracking_param(k)
        ]
        pairs.sort(key=lambda kv: (kv[0], kv[1]))
        query = urlencode(pairs)

    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> bytes:
    """SHA-256 of the exact URL. Identity, not dedup."""
    return hashlib.sha256(url.encode("utf-8")).digest()


def normalized_hash(url: str, *, drop_query: bool = False) -> bytes:
    """SHA-256 of the normalised URL. The dedup key."""
    return hashlib.sha256(normalize_url(url, drop_query=drop_query).encode("utf-8")).digest()


def variant_hash(
    url: str,
    *,
    country: str | None = None,
    mobile: bool = False,
    drop_query: bool = False,
    extraction: str = "",
    actions: str = "",
) -> bytes:
    """The cache key: the URL PLUS whatever changes what the document is.

    `normalized_hash` is the URL alone, which is right for crawl dedup — two
    routes to the same page are the same page. It is wrong for a cache that is
    shared between customers, because the same URL fetched from a different
    country or rendered for a phone is a DIFFERENT document.

    Measured: two scrapes of ipinfo.io, the first through a US exit and the
    second explicitly asking for GB. The GB caller was handed the US page. Same
    failure as a search parameter nobody honoured — you ask for one thing and
    are silently given another, and the response looks perfect.

    `extraction` is the same argument one level down. A `pages` row stores ONE
    extraction, so the options that DECIDE that extraction belong in the key
    too. They were missing: measured 6 Sep 2026, a request with
    `includeTags: ["h1"]` stored a 40-character row, and the very next caller
    asking for the whole page was handed those 40 characters with
    `cached: true`. Fetch-shaping and extraction-shaping options fail the same
    way; only one of them was in the key.

    `actions` is the third. A page reached by clicking is not the page at the
    URL: clicking `#a` and clicking `#b` produced the same key, and a request
    carrying steps would accept any browser-rendered page as its cached
    answer — including one where no step had ever run.
    """
    parts = [
        normalize_url(url, drop_query=drop_query),
        f"country={(country or '').lower()}",
        f"mobile={int(bool(mobile))}",
        f"extraction={extraction}",
        f"actions={actions}",
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).digest()


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def registrable_domain(url_or_host: str) -> str:
    """eTLD+1 via the public suffix list.

    `example.co.uk` must yield `example.co.uk`, never `co.uk` — naive string
    splitting on dots gets this wrong and silently corrupts company dedup.
    """
    candidate = url_or_host
    if "://" in candidate:
        candidate = host_of(candidate)
    result = _extract(candidate)
    if result.domain and result.suffix:
        return f"{result.domain}.{result.suffix}".lower()

    # Unknown suffix: a TLD newer than the bundled public-suffix snapshot, or a
    # reserved one like .invalid or .test. Falling back to the extracted domain
    # alone would collapse every host under that TLD onto a single key, pooling
    # politeness budgets and tier memory across unrelated sites. Use the last
    # two labels instead, which is the right answer for a flat TLD.
    host = candidate.strip().strip(".").lower()
    labels = [label for label in host.split(".") if label]
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host or (result.domain or candidate).lower()


def has_skipped_extension(url: str) -> bool:
    path = urlsplit(url).path.lower()
    dot = path.rfind(".")
    if dot == -1:
        return False
    return path[dot:] in SKIP_EXTENSIONS


def same_registrable_domain(a: str, b: str) -> bool:
    return registrable_domain(a) == registrable_domain(b)


def is_subdomain_of(candidate_host: str, root_host: str) -> bool:
    candidate_host = candidate_host.lower()
    root_host = root_host.lower()
    return candidate_host == root_host or candidate_host.endswith("." + root_host)
