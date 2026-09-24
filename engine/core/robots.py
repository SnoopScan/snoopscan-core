"""robots.txt handling (11-compliance.md section 1).

Consulted when `respectRobots` is set; the default is off, and the reasoning
for that is on the field in models.py. This docstring said "respected by
default" for as long as the default has been False — a comment that states the
opposite of the code is worse than none, because it is believed.

Cached in domain_profiles with a 24h TTL so it is not re-fetched per URL.
Crawl-delay is honoured when higher than our own floor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit


@dataclass
class RobotsRules:
    allow: list[str] = field(default_factory=list)
    disallow: list[str] = field(default_factory=list)
    crawl_delay_ms: int | None = None
    sitemaps: list[str] = field(default_factory=list)
    fetched: bool = False

    def permits(self, path: str) -> bool:
        """Longest-match wins, with Allow beating Disallow at equal length —
        the behaviour every major crawler implements."""
        if not self.fetched:
            # A robots.txt we could not fetch is not a disallow.
            return True
        best_allow = max((len(p) for p in self.allow if _matches(p, path)), default=-1)
        best_disallow = max((len(p) for p in self.disallow if _matches(p, path)), default=-1)
        if best_disallow < 0:
            return True
        return best_allow >= best_disallow


def _matches(pattern: str, path: str) -> bool:
    if pattern == "":
        return False
    if "*" not in pattern and "$" not in pattern:
        return path.startswith(pattern)
    regex = re.escape(pattern).replace(r"\*", ".*")
    if regex.endswith(r"\$"):
        regex = regex[:-2] + "$"
    try:
        return re.match(regex, path) is not None
    except re.error:
        return path.startswith(pattern.split("*")[0])


def robots_url_for(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))


def parse(body: str, user_agent: str = "*") -> RobotsRules:
    """Parse for our agent, falling back to the wildcard group.

    Sitemap directives are global and collected regardless of group.
    """
    rules = RobotsRules(fetched=True)
    ua_lower = user_agent.lower()

    groups: dict[str, RobotsRules] = {}
    current_agents: list[str] = []
    starting_group = False

    for raw_line in body.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "sitemap":
            if value:
                rules.sitemaps.append(value)
            continue

        if field_name == "user-agent":
            if not starting_group:
                current_agents = []
                starting_group = True
            current_agents.append(value.lower())
            groups.setdefault(value.lower(), RobotsRules(fetched=True))
            continue

        starting_group = False
        for agent in current_agents:
            group = groups.setdefault(agent, RobotsRules(fetched=True))
            if field_name == "disallow":
                group.disallow.append(value)
            elif field_name == "allow":
                group.allow.append(value)
            elif field_name == "crawl-delay":
                try:
                    group.crawl_delay_ms = int(float(value) * 1000)
                except ValueError:
                    continue

    # Our own agent's group wins; otherwise the wildcard group applies.
    chosen: RobotsRules | None = None
    for agent, group in groups.items():
        if agent and agent != "*" and agent in ua_lower:
            chosen = group
            break
    if chosen is None:
        chosen = groups.get("*")

    if chosen is not None:
        rules.allow = chosen.allow
        rules.disallow = chosen.disallow
        rules.crawl_delay_ms = chosen.crawl_delay_ms
    return rules


def path_of(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
