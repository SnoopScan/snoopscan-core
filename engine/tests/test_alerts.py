"""The alert rules must be capable of firing.

An alert naming a metric that has been renamed does not error. It evaluates to
nothing, for ever, and a dashboard of alerts that never fire is indistinguishable
from a healthy system — right up until the incident it was meant to catch.

So this checks the rules against the metric registry the engine actually
exposes, and the runbook links against the headings that actually exist. Both
are the kind of thing that rots silently between the day it is written and the
day it is needed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from prometheus_client import REGISTRY

ALERTS_FILE = Path(__file__).resolve().parents[2] / "deploy" / "alerts.yml"
RUNBOOK_FILE = Path(__file__).resolve().parents[2] / "docs" / "RUNBOOK.md"

# Metrics Prometheus provides about itself, which we reference but do not define.
EXTERNAL_METRICS = {"up"}

# Suffixes the client library appends to a histogram's base name.
HISTOGRAM_SUFFIXES = ("_sum", "_count", "_bucket", "_total")


def load_rules() -> list[dict[str, Any]]:
    document = yaml.safe_load(ALERTS_FILE.read_text())
    return [rule for group in document["groups"] for rule in group["rules"]]


def registry_metric_names() -> set[str]:
    """Base names of every metric the engine exposes."""
    import engine.core.metrics  # noqa: F401  — registers the collectors

    names: set[str] = set()
    for metric in REGISTRY.collect():
        names.add(metric.name)
        for sample in metric.samples:
            names.add(sample.name)
    return names


def metrics_referenced(expression: str) -> set[str]:
    """Every `engine_*` identifier an expression depends on."""
    # Strip label selectors so label VALUES are not mistaken for metric names.
    without_labels = re.sub(r"\{[^}]*\}", "", expression)
    identifiers = set(re.findall(r"\b[a-z_][a-z0-9_]*\b", without_labels))
    return {i for i in identifiers if i.startswith("engine_") or i in EXTERNAL_METRICS}


def base_name(metric: str) -> str:
    for suffix in HISTOGRAM_SUFFIXES:
        if metric.endswith(suffix):
            return metric[: -len(suffix)]
    return metric


# --------------------------------------------------------------------------
# The check this file exists for
# --------------------------------------------------------------------------


def test_every_alert_references_a_metric_that_exists() -> None:
    """A rule naming a renamed metric fires never and looks like health."""
    exposed = registry_metric_names()
    exposed_bases = {base_name(name) for name in exposed}

    unknown: list[str] = []
    for rule in load_rules():
        for metric in metrics_referenced(rule["expr"]):
            if metric in EXTERNAL_METRICS:
                continue
            if metric in exposed or base_name(metric) in exposed_bases:
                continue
            unknown.append(f"{rule['alert']} -> {metric}")

    assert not unknown, (
        "alert rules reference metrics the engine does not expose. These "
        "rules can never fire:\n  " + "\n  ".join(sorted(unknown))
    )


def test_the_check_would_notice_a_renamed_metric() -> None:
    """Proves the test above is capable of failing. Without this, a broken
    extractor makes every rule look valid."""
    assert metrics_referenced("sum(rate(engine_typo_total[1h])) > 0") == {"engine_typo_total"}


def test_label_values_are_not_mistaken_for_metric_names() -> None:
    """`{tier="impersonate"}` must not read as a metric called impersonate."""
    found = metrics_referenced('sum(rate(engine_blocks_total{tier="impersonate"}[1h]))')
    assert found == {"engine_blocks_total"}


# --------------------------------------------------------------------------
# An alert nobody can act on is noise
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rule", load_rules(), ids=lambda r: str(r["alert"]))
def test_every_alert_is_actionable(rule: dict[str, Any]) -> None:
    annotations = rule.get("annotations", {})
    assert rule.get("labels", {}).get("severity") in {"warning", "critical"}, (
        f"{rule['alert']} has no usable severity"
    )
    assert annotations.get("summary"), f"{rule['alert']} has no summary"
    assert annotations.get("runbook"), (
        f"{rule['alert']} has no runbook link. At 3am the alert is all there is."
    )


@pytest.mark.parametrize("rule", load_rules(), ids=lambda r: str(r["alert"]))
def test_every_alert_waits_before_firing(rule: dict[str, Any]) -> None:
    """Without `for`, a single scrape blip pages someone. An alert that cries
    wolf gets silenced, and a silenced alert is worse than an absent one
    because it still looks like coverage."""
    assert rule.get("for"), f"{rule['alert']} fires instantly"


@pytest.mark.parametrize("rule", load_rules(), ids=lambda r: str(r["alert"]))
def test_every_runbook_link_resolves(rule: dict[str, Any]) -> None:
    """A link to a heading that was renamed is a dead end at the worst moment."""
    if not RUNBOOK_FILE.exists():
        pytest.skip("the runbook is operations-only and not in the published core")
    target = rule["annotations"]["runbook"]
    assert "#" in target, f"{rule['alert']} links to a file with no anchor"
    _, anchor = target.split("#", 1)

    headings = {
        re.sub(r"[^a-z0-9\s-]", "", line.lstrip("# ").lower()).strip().replace(" ", "-")
        for line in RUNBOOK_FILE.read_text().splitlines()
        if line.startswith("#")
    }
    assert anchor in headings, (
        f"{rule['alert']} points at RUNBOOK.md#{anchor}, which does not exist. "
        f"Available: {sorted(headings)}"
    )


def test_alert_names_are_unique() -> None:
    names = [rule["alert"] for rule in load_rules()]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"duplicate alert names: {duplicates}"


def test_the_spec_conditions_are_all_covered() -> None:
    """07-orchestration.md section 10 lists the conditions that must alert.
    Matched loosely by subject, because the rule names are ours."""
    expressions = " ".join(rule["expr"] for rule in load_rules())
    required = {
        "block rate": "engine_blocks_total",
        "proxy bandwidth": "engine_proxy_budget_used_ratio",
        "queue depth": "engine_queue_depth",
        "extraction confidence": "engine_extraction_confidence",
    }
    missing = [subject for subject, metric in required.items() if metric not in expressions]
    assert not missing, f"spec conditions with no alert: {missing}"
