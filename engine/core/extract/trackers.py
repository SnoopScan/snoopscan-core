"""Which ad and analytics tags a page actually FIRED, read from its requests.

A tag in the HTML is a claim; a request to the vendor's collection endpoint is
the fact. A tag can sit on the page and never fire (consent withheld, a broken
trigger, a script error before it runs), and one can fire from a tag manager
the HTML never mentions. Ad verification needs the fact, so this reads the
network log, not the markup.

Two kinds, kept apart because they answer different questions:

    loaded   the vendor's library was fetched — the tag is INSTALLED
    hit      a collection request went out — the tag FIRED, with this ID
             and this event

Only first-party vendor endpoints are recognised. A site running server-side
tagging on its own subdomain sends nothing a third party can see here, and
this reports that as absent rather than guessing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qs, urlsplit


def _q(url: str) -> dict[str, str]:
    """First value of each query parameter."""
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items() if v}


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _on(host: str, domain: str) -> bool:
    """`host` is `domain` or a subdomain of it — never merely a string ending.

    `host.endswith("google-analytics.com")` also matched
    `evilgoogle-analytics.com`, which would let any site report itself as
    firing GA4. The dot is the boundary that makes it a subdomain.
    """
    return host == domain or host.endswith("." + domain)


# Every host a recognised tag reports to. The asset filter lets these through
# even when it is declining images: a Meta pixel IS an image request, 43 bytes
# of GIF, and blocking it would report every Meta conversion as failed.
TRACKER_DOMAINS = (
    "google-analytics.com",
    "analytics.google.com",
    "googletagmanager.com",
    "googleadservices.com",
    "googleads.g.doubleclick.net",
    "facebook.com",
    "connect.facebook.net",
    "analytics.tiktok.com",
    "px.ads.linkedin.com",
    "bat.bing.com",
)


def is_tracker_host(url: str) -> bool:
    host = _host(url)
    return any(_on(host, domain) for domain in TRACKER_DOMAINS)


def _path(url: str) -> str:
    return urlsplit(url).path or "/"


def _post_json(entry: dict[str, Any]) -> dict[str, Any]:
    raw = entry.get("_post")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dig(data: dict[str, Any], *keys: str) -> Any:
    """data[k1][k2]... or None the moment a level is missing or not a dict."""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _match(entry: dict[str, Any]) -> dict[str, Any] | None:
    url = str(entry.get("url") or "")
    host, path, q = _host(url), _path(url), _q(url)

    # --- Google Analytics 4 -------------------------------------------------
    # Hits go to /g/collect on google-analytics.com (any regional prefix) or
    # analytics.google.com, with the measurement ID in `tid` and the event in
    # `en`. Beacons may batch several events; `en` names the first.
    if (_on(host, "google-analytics.com") or host == "analytics.google.com") and (
        path.endswith("/g/collect")
    ):
        return {"vendor": "ga4", "kind": "hit", "id": q.get("tid"), "event": q.get("en")}

    # --- Google tag / Tag Manager (the libraries) ---------------------------
    if host == "www.googletagmanager.com" or host == "googletagmanager.com":
        if path == "/gtm.js":
            return {"vendor": "gtm", "kind": "loaded", "id": q.get("id"), "event": None}
        if path == "/gtag/js":
            tag = q.get("id") or ""
            vendor = "google_ads" if tag.startswith("AW-") else "ga4"
            return {"vendor": vendor, "kind": "loaded", "id": tag or None, "event": None}

    # --- Google Ads conversions ---------------------------------------------
    # /pagead/conversion/<id>/ and /pagead/viewthroughconversion/<id>/ carry
    # the numeric conversion ID in the path, which is the AW- ID's number.
    if host in ("www.googleadservices.com", "googleads.g.doubleclick.net"):
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "pagead" and "conversion" in parts[1]:
            return {
                "vendor": "google_ads",
                "kind": "hit",
                "id": f"AW-{parts[2]}" if parts[2].isdigit() else parts[2],
                "event": q.get("label") or parts[1],
            }

    # --- Meta (Facebook) pixel ----------------------------------------------
    # The pixel fires GET/POST to facebook.com/tr with `id` (pixel ID) and
    # `ev` (event). The library is fbevents.js on connect.facebook.net.
    if host in ("www.facebook.com", "facebook.com") and path.rstrip("/") == "/tr":
        return {"vendor": "meta", "kind": "hit", "id": q.get("id"), "event": q.get("ev")}
    if host == "connect.facebook.net" and path.endswith("/fbevents.js"):
        return {"vendor": "meta", "kind": "loaded", "id": None, "event": None}
    # The library then fetches its configuration by pixel ID — so the ID of an
    # INSTALLED pixel is known even when no event fires in the window.
    if host == "connect.facebook.net" and "/signals/config/" in path:
        pixel = path.rsplit("/signals/config/", 1)[1].split("/", 1)[0]
        return {"vendor": "meta", "kind": "loaded", "id": pixel or None, "event": None}

    # --- TikTok pixel --------------------------------------------------------
    # The library carries the pixel code as `sdkid`; events POST JSON to
    # /api/v2/pixel with `pixel_code` and `event` in the body.
    if host == "analytics.tiktok.com":
        if path.endswith("/pixel/events.js"):
            return {"vendor": "tiktok", "kind": "loaded", "id": q.get("sdkid"), "event": None}
        if path.startswith("/api/v2/pixel"):
            body = _post_json(entry)
            # Flat `pixel_code` on some builds; nested under context.pixel.code
            # on others (both seen live, Sep 2026).
            nested = _dig(body, "context", "pixel", "code")
            return {
                "vendor": "tiktok",
                "kind": "hit",
                "id": body.get("pixel_code") or nested or q.get("sdkid"),
                "event": body.get("event"),
            }

    # --- LinkedIn Insight ----------------------------------------------------
    if host == "px.ads.linkedin.com" and path.startswith("/collect"):
        return {"vendor": "linkedin", "kind": "hit", "id": q.get("pid"), "event": None}

    # --- Microsoft Advertising (Bing UET) -----------------------------------
    if host == "bat.bing.com" and path.startswith("/action"):
        return {"vendor": "microsoft_ads", "kind": "hit", "id": q.get("ti"), "event": q.get("evt")}

    return None


# Hard refusals: the request never left, or was turned away. ERR_ABORTED is
# deliberately NOT here — see _delivery.
_REFUSED_FAILURES = (
    "ERR_BLOCKED_BY_CLIENT",
    "ERR_BLOCKED_BY_RESPONSE",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_RESET",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CERT",
    "ERR_SSL",
    "NS_ERROR_CONNECTION_REFUSED",
    "NS_ERROR_UNKNOWN_HOST",
)


def _delivery(entry: dict[str, Any]) -> str:
    """Did the vendor get it? Three answers, because two would lie.

    confirmed     a 2xx/3xx came back
    refused       an error status, or a failure that means it never arrived
    unconfirmed   it went out and no answer was observed

    `unconfirmed` exists because of a live measurement: a retail site's GA4
    page_view went out with its ID and the browser reported net::ERR_ABORTED,
    which fire-and-forget beacons commonly get whether or not the vendor
    received them. A yes/no field called that FAILED and would have told an
    ad-verification customer their analytics was broken when the tag had
    fired. The TAG firing is proven by the request existing; delivery is a
    separate, weaker claim.
    """
    status = entry.get("status")
    if isinstance(status, int):
        return "confirmed" if status < 400 else "refused"
    failure = str(entry.get("failure") or "")
    if failure and any(code in failure for code in _REFUSED_FAILURES):
        return "refused"
    return "unconfirmed"


def detect(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recognised tag activity, one row per vendor/kind/ID/event, with a count.

    Order is first-seen, which is the order the page fired them — useful when
    the question is "did the conversion fire before the page view?".
    """
    seen: dict[tuple[str, str, str | None, str | None], dict[str, Any]] = {}
    for entry in entries:
        found = _match(entry)
        if found is None:
            continue
        key = (found["vendor"], found["kind"], found["id"], found["event"])
        if key in seen:
            seen[key]["count"] += 1
            continue
        delivery = _delivery(entry)
        seen[key] = {
            **found,
            "status": entry.get("status"),
            "delivery": delivery,
            "failed": delivery == "refused",
            "count": 1,
        }
    return list(seen.values())
