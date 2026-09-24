"""Consent walls that heal themselves (site_rules.py's companion).

Google's GDPR interstitial is dismissed by the `SOCS` cookie that "Accept all"
sets. `site_rules.yaml` ships a value — but Google rotates it, and when the
old value stops working the wall comes back at every tier and someone has to
paste a new one into the YAML. That is an operational dependency the engine
can remove on its own: the "Accept all" control is an ordinary HTML form that
POSTs to `consent.google.com/save`, and replaying it yields the fresh cookie.

Measured 5 Sep 2026 from a UK exit, no browser: GET the target → the consent
page → parse the accept form's hidden fields → POST them → `SOCS` (87 chars)
in the jar → refetch the target → "Google Maps". Under a second.

This module does the harvest. The service decides when (a `challenge_redirect`
to a consent host, or a consent/challenge title after extraction, on a host
that has a site rule), stores what came back, and retries the request once.
Only consent state is ever harvested — the form is the public "Accept all"
button, nothing is signed in, nothing identifies a person.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import structlog
from selectolax.parser import HTMLParser

from engine.core.urls import registrable_domain

logger = structlog.get_logger(__name__)

CONSENT_HOSTS = ("consent.google.com", "consent.youtube.com")
SAVE_HOST = "consent.google.com"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class HarvestedCookie:
    name: str
    value: str
    domain: str
    path: str = "/"

    def as_cookie(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value, "domain": self.domain, "path": self.path}


def is_consent_redirect(final_url: str | None) -> bool:
    """Did the fetch land on the consent interstitial rather than the page?"""
    host = (urlsplit(final_url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in CONSENT_HOSTS)


def accept_form_fields(html: str) -> dict[str, str] | None:
    """The hidden fields of the "Accept all" form, or None if there is none.

    The page carries several `/save` forms (accept and reject, twice for two
    layouts). The accept one is the one whose button says so; its distinguishing
    fields are `set_sc`/`set_aps`, but the label is what a person reads, so the
    label is what we match.
    """
    tree = HTMLParser(html)
    for form in tree.css("form"):
        action = (form.attributes.get("action") or "").lower()
        if SAVE_HOST not in action:
            continue
        label = " ".join(
            (b.text() or "").strip() for b in form.css("button, input[type=submit]")
        ).lower()
        if "accept" not in label and "agree" not in label:
            continue
        fields: dict[str, str] = {}
        for node in form.css("input"):
            name = node.attributes.get("name")
            # .get(..., "hidden") only supplies the default when the key is
            # absent — a valueless attribute like `<input type>` parses to a
            # present key with a None value, which `or` also catches.
            if name and (node.attributes.get("type") or "hidden").lower() in ("hidden", "submit"):
                fields[name] = node.attributes.get("value") or ""
        if fields:
            return fields
    return None


async def harvest(
    url: str, *, proxy_url: str | None = None, timeout_s: float = 20.0
) -> HarvestedCookie | None:
    """Click "Accept all" the way a browser would, and return the cookie it set.

    Returns None — never raises — when the page is not a consent wall, the form
    is not found, or the POST does not set the cookie. The caller falls back to
    whatever it had.
    """
    domain = registrable_domain(url)
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
            proxy=proxy_url,
            headers={"User-Agent": _UA, "Accept-Language": "en-GB,en;q=0.9"},
        ) as client:
            first = await client.get(url)
            on_wall = is_consent_redirect(str(first.url)) or f"{SAVE_HOST}/save" in first.text
            if not on_wall:
                logger.info("consent_harvest_not_needed", domain=domain)
                return None
            fields = accept_form_fields(first.text)
            if fields is None:
                logger.warning("consent_harvest_no_accept_form", domain=domain)
                return None
            await client.post(
                f"https://{SAVE_HOST}/save", data=fields, headers={"Referer": str(first.url)}
            )
            value = client.cookies.get("SOCS")
            if not value:
                logger.warning("consent_harvest_no_cookie", domain=domain)
                return None
            logger.info("consent_cookie_harvested", domain=domain, length=len(value))
            return HarvestedCookie(name="SOCS", value=value, domain=f".{domain}")
    except Exception as exc:  # noqa: BLE001 - a failed heal must never fail the fetch
        logger.warning("consent_harvest_failed", domain=domain, error=str(exc)[:120])
        return None
