"""Per-site defaults applied to every fetch, at every tier (site_rules.yaml).

The first thing a person's browser does on google.com from the EU is click
"Accept all". The cookie that click leaves behind is what turns the consent
interstitial into the page you asked for. A scraper that does not carry that
state is not being blocked — it is being shown the same door everyone sees,
and refusing to open it. That is what happened to Maps on 5 Sep 2026: a 200
with 230 words of legal copy, at every tier, from a UK exit.

Rules are data, not code: a consent cookie's value rotates, and updating one
must not need a deploy. Only consent and preference state belongs here —
never a session, never anything that authenticates (11-compliance.md).

Applied in one place (scrape_service, when the FetchRequest is built) and
honoured by every tier: tiers 0/1 send a `Cookie` header, browser tiers
install the cookies into the fresh context before navigation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from engine.core.urls import registrable_domain

_RULES_PATH = Path(__file__).with_name("site_rules.yaml")


@dataclass(frozen=True)
class SiteRule:
    """What a fetch of this URL should carry by default. Empty means nothing."""

    cookies: tuple[dict[str, str], ...] = field(default_factory=tuple)
    why: str = ""

    def cookie_header(self) -> str | None:
        """`name=value; name2=value2` for the HTTP tiers, or None."""
        if not self.cookies:
            return None
        return "; ".join(f"{c['name']}={c['value']}" for c in self.cookies)

    def browser_cookies(self) -> list[dict[str, str]]:
        """Playwright's shape: needs `domain` + `path` (or `url`) per cookie."""
        return [dict(c) for c in self.cookies]


EMPTY = SiteRule()


@dataclass(frozen=True)
class _Rule:
    hosts: tuple[str, ...]
    cookies: tuple[dict[str, str], ...]
    why: str
    # False: the Firefox rungs run without Media Source Extensions here (see
    # tier3h_camoufox._NO_MSE). Read by `media_streaming()`, not `for_url`.
    media_streaming: bool = True

    def matches(self, host: str) -> bool:
        for suffix in self.hosts:
            if suffix.endswith("."):
                # "google." → any TLD: google.com, google.co.uk, www.google.de …
                stem = suffix[:-1]
                labels = host.split(".")
                if stem in labels[:-1]:
                    return True
            elif host == suffix or host.endswith("." + suffix):
                return True
        return False


@lru_cache(maxsize=1)
def _load() -> tuple[_Rule, ...]:
    raw = yaml.safe_load(_RULES_PATH.read_text()) or {}
    out: list[_Rule] = []
    for item in raw.get("rules", []):
        cookies = tuple(
            {
                "name": str(c["name"]),
                "value": str(c["value"]),
                "domain": str(c.get("domain", "")),
                "path": str(c.get("path", "/")),
            }
            for c in item.get("cookies", [])
        )
        out.append(
            _Rule(
                hosts=tuple(str(h).lower() for h in item.get("hosts", [])),
                cookies=cookies,
                why=str(item.get("why", "")).strip(),
                media_streaming=bool(item.get("media_streaming", True)),
            )
        )
    return tuple(out)


def reload() -> None:
    """After editing the YAML in a running process (tests, ops)."""
    _load.cache_clear()


def for_url(url: str) -> SiteRule:
    """The rule for this URL's host, with cookie domains resolved to the host
    actually being fetched — a `google.` wildcard rule fetching google.co.uk
    must set the cookie on .google.co.uk, not .google.com."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return EMPTY
    for rule in _load():
        if rule.matches(host):
            reg = registrable_domain(url)
            cookies = tuple(
                {**c, "domain": c["domain"] if _domain_fits(c["domain"], host) else f".{reg}"}
                for c in rule.cookies
            )
            return SiteRule(cookies=cookies, why=rule.why)
    return EMPTY


def media_streaming(url: str) -> bool:
    """False when a rule for this host says its players stream regardless.

    Every matching rule is consulted, not only the first: the cookie rules
    stop at the first match, and a streaming rule must not have to share an
    entry with — or be shadowed by — a consent rule for the same host.
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return True
    return not any(r.matches(host) and not r.media_streaming for r in _load())


def _domain_fits(cookie_domain: str, host: str) -> bool:
    d = cookie_domain.lstrip(".").lower()
    return bool(d) and (host == d or host.endswith("." + d))


async def effective(url: str, *, persist: bool) -> SiteRule:
    """The YAML rule with any cookie the engine has harvested for itself laid
    over it (fetch/consent.py, `site_cookies`). A harvested value is newer than
    the shipped one, so it wins; the YAML is what a fresh install starts from.

    Storage is optional here as everywhere: no database, or any error reading
    it, means the YAML answer — never a failed fetch.
    """
    rule = for_url(url)
    if not rule.cookies or not persist:
        return rule
    try:
        from engine.storage import repositories as repo

        reg = registrable_domain(url)
        cookies = []
        for c in rule.cookies:
            fresh = await repo.get_site_cookie(reg, c["name"])
            cookies.append({**c, "value": fresh} if fresh else c)
        return SiteRule(cookies=tuple(cookies), why=rule.why)
    except Exception:  # noqa: BLE001 - the YAML default is the safe answer
        return rule
