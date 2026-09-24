"""Per-host pacing scales with the plan — and a domain can still slow it down.

A customer buying 150 concurrent requests was getting 2 against any one site
and one request a second: the free plan's pacing at 37x the price.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from engine.core.politeness import HostBudget, budget_for_plan
from engine.settings import settings

ROOT = Path(__file__).resolve().parents[2]


def test_the_curve_rewards_the_plan_and_never_inverts() -> None:
    plans = [5, 15, 50, 100, 150]
    budgets = [budget_for_plan(p) for p in plans]

    # Faster (or equal) and wider (or equal) at every step up. Never backwards.
    for lower, higher in zip(budgets, budgets[1:]):
        assert higher.delay_ms <= lower.delay_ms
        assert higher.concurrency >= lower.concurrency

    # And the ends are genuinely different, or the curve is decoration.
    assert budgets[-1].delay_ms < budgets[0].delay_ms
    assert budgets[-1].concurrency > budgets[0].concurrency


def test_the_free_plan_is_exactly_where_it_was() -> None:
    # Loosening the paid plans must not quietly loosen the free one too.
    free = budget_for_plan(settings.politeness_reference_concurrency)
    assert free.delay_ms == settings.politeness_default_delay_ms
    assert free.concurrency == settings.politeness_default_concurrency


def test_no_plan_gets_the_safe_end_not_the_fast_one() -> None:
    for missing in (None, 0, -1):
        budget = budget_for_plan(missing)
        assert budget.delay_ms == settings.politeness_default_delay_ms
        assert budget.concurrency == settings.politeness_default_concurrency


def test_the_floor_and_ceiling_hold_however_big_the_plan() -> None:
    huge = budget_for_plan(100_000)
    assert huge.delay_ms == settings.politeness_floor_delay_ms
    assert huge.concurrency == settings.politeness_max_host_concurrency

    # A host never gets the customer's whole budget: one site must not be able
    # to consume every slot they paid for.
    for plan in (50, 100, 150, 400):
        assert budget_for_plan(plan).concurrency < plan


@pytest.mark.parametrize(
    ("domain_delay", "domain_conc", "expect_delay", "expect_conc"),
    [
        (None, None, 100, 32),  # no opinion: the plan decides
        (5_000, None, 5_000, 32),  # a 429'd domain stays slow, whatever you pay
        (None, 3, 100, 3),  # a fragile domain caps concurrency
        (50, None, 100, 32),  # a domain may not go FASTER than the plan
    ],
)
def test_a_domain_may_only_ever_make_it_gentler(
    domain_delay: int | None, domain_conc: int | None, expect_delay: int, expect_conc: int
) -> None:
    budget = budget_for_plan(150)
    delay = max(budget.delay_ms, domain_delay or 0)
    concurrency = min(budget.concurrency, domain_conc or budget.concurrency)
    assert (delay, concurrency) == (expect_delay, expect_conc)


def test_every_scrape_caller_passes_the_plan() -> None:
    """A call site that forgets it silently paces that whole path at free speed.

    Checked structurally rather than by eye: there are call sites in the API
    routes, the crawler and the batch worker, and the failure is invisible —
    everything still works, just slowly, for the customers paying most.
    """
    missing: list[str] = []

    for path in [
        "engine/api/routes/scrape.py",
        "engine/api/routes/crawl.py",
        "engine/api/routes/extract.py",
        "engine/core/frontier/crawler.py",
        "engine/workers/http_worker.py",
    ]:
        tree = ast.parse((ROOT / path).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "scrape":
                continue
            if not any(k.arg == "plan_concurrency" for k in node.keywords):
                missing.append(f"{path}:{node.lineno}")

    assert not missing, "scrape() called without plan_concurrency at: " + ", ".join(missing)


def test_the_budget_is_a_value_not_a_mutable_surprise() -> None:
    budget = budget_for_plan(50)
    assert isinstance(budget, HostBudget)
    with pytest.raises((AttributeError, TypeError)):
        budget.delay_ms = 1  # type: ignore[misc]


def test_the_caps_that_protect_US_are_still_in_place() -> None:
    """Loosening what we do TO targets must not loosen what protects us.

    Proxy spend, response size and the circuit breaker are our own money,
    memory and reputation — nothing about pacing should have touched them.
    """
    assert settings.proxy_daily_budget_mb > 0
    assert settings.proxy_monthly_budget_mb > 0
    assert settings.proxy_max_response_mb > 0
    assert 0 < settings.circuit_failure_rate <= 1
    assert settings.parse_max_mb > 0
    # And a redirect chain still terminates.
    assert 0 < settings.max_redirects <= 20


def test_the_pacing_columns_carry_no_schema_default() -> None:
    """A default is not a decision.

    politeness_delay_ms was NOT NULL DEFAULT 1000, so every insert into
    domain_profiles — for country tracking, tier memory, statistics, anything —
    stamped the free plan's pacing onto that domain as an explicit override.
    3,594 of 3,652 domains carried it, and it beat the caller's plan every
    time: the whole per-host curve applied only to domains nobody had touched.

    Read from the migration rather than a live database so it fails in CI too.
    """
    migration = (
        ROOT / "engine/storage/migrations/versions/0015_pacing_is_not_a_default.py"
    ).read_text()

    for column in ("politeness_delay_ms", "max_concurrency"):
        assert f"ALTER COLUMN {column} DROP DEFAULT" in migration
        assert f"ALTER COLUMN {column} DROP NOT NULL" in migration
        assert f"SET {column} = NULL" in migration

    # Only the legacy values are cleared — a domain slowed on purpose keeps
    # what it was given. The migration names them as constants, so assert those
    # rather than a literal that interpolation would hide.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pacing_migration",
        ROOT / "engine/storage/migrations/versions/0015_pacing_is_not_a_default.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.LEGACY_DELAY_MS == 1000
    assert module.LEGACY_CONCURRENCY == 2
    upgrade_src = migration.split("def downgrade")[0]
    assert "WHERE politeness_delay_ms = {LEGACY_DELAY_MS}" in upgrade_src
    assert "WHERE max_concurrency = {LEGACY_CONCURRENCY}" in upgrade_src
    # Nothing above the legacy value is touched.
    assert ">" not in upgrade_src.split("UPDATE domain_profiles")[1].split('"""')[0]


def test_a_domain_with_no_opinion_reads_as_none_not_as_the_default() -> None:
    """get_politeness must be able to say 'nothing set'.

    Returning the global default here was indistinguishable from a domain
    deliberately paced at exactly that value, which is what made the schema
    default invisible for so long.
    """
    import inspect

    from engine.storage import repositories

    source = inspect.getsource(repositories.get_politeness)
    assert "return None, None" in source
    assert "int | None" in str(inspect.signature(repositories.get_politeness).return_annotation)
