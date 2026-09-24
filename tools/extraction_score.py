#!/usr/bin/env python3
"""Extraction quality harness. Tracks one headline number, and it should only
ever go up.

04-extraction.md section 9: "Track aggregate F1 across the fixture set as a
single headline number in CI output." That is the whole point — extraction
regressions are silent, so without a number nobody notices a change made
things worse until a customer does.

    python tools/extraction_score.py            # score, print the table
    python tools/extraction_score.py --baseline # rewrite the stored baseline
    python tools/extraction_score.py --check    # fail if below the baseline
    python tools/extraction_score.py --compare  # routed path vs heuristic

The --compare mode answers the Phase 5 acceptance criterion directly: forum
and product extraction must measurably beat the heuristic baseline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from engine.core.extract.classify import classify
from engine.core.extract.heuristic import HeuristicExtractor
from engine.core.extract.quality import (
    Score,
    count_code_blocks,
    count_tables,
    score,
)
from engine.core.extract.router import ExtractOptions, extract, preclean
from engine.tests.fixtures.corpus import Fixture, all_fixtures

BASELINE_PATH = Path(__file__).resolve().parent.parent / "engine" / "tests" / "baseline.json"

# How far the headline may fall before CI fails. Small movements are noise
# from a fixture being added; a real regression is larger than this.
REGRESSION_TOLERANCE = 0.02


def run_fixture(fixture: Fixture) -> tuple[Score, list[str]]:
    """Score one fixture and collect any hard assertion failures.

    F1 alone is not sufficient: a high score can still hide a lost heading or
    a surviving cookie banner, so the structural checks run alongside it.
    """
    result = extract(fixture.html, f"https://example.com/{fixture.name}")
    markdown = result.markdown
    problems: list[str] = []

    for needle in fixture.must_contain:
        if needle.lower() not in markdown.lower():
            problems.append(f"lost required content: {needle!r}")
    for needle in fixture.must_not_contain:
        if needle.lower() in markdown.lower():
            problems.append(f"kept boilerplate: {needle!r}")
    if result.word_count < fixture.min_words:
        problems.append(f"{result.word_count} words, expected at least {fixture.min_words}")
    if count_tables(markdown) < fixture.min_tables:
        problems.append(f"{count_tables(markdown)} tables, expected {fixture.min_tables}")
    if count_code_blocks(markdown) < fixture.min_code_blocks:
        problems.append(
            f"{count_code_blocks(markdown)} code blocks, expected {fixture.min_code_blocks}"
        )

    return score(markdown, fixture.reference), problems


def heuristic_only(fixture: Fixture) -> Score:
    """The baseline the structured path has to beat.

    Runs trafilatura directly, bypassing routing, which is what "the heuristic
    baseline" in the acceptance criterion means.
    """
    options = ExtractOptions()
    cleaned = preclean(fixture.html, options)
    cls = classify(cleaned)
    result = HeuristicExtractor().extract(
        cleaned, f"https://example.com/{fixture.name}", cls, options
    )
    return score(result.markdown, fixture.reference)


def report() -> tuple[float, dict[str, float], list[str]]:
    scores: dict[str, float] = {}
    failures: list[str] = []
    rows: list[tuple[str, str, Score, list[str]]] = []

    for fixture in all_fixtures():
        result, problems = run_fixture(fixture)
        scores[fixture.name] = result.f1
        rows.append((fixture.name, str(fixture.page_type), result, problems))
        failures.extend(f"{fixture.name}: {p}" for p in problems)

    print(f"{'fixture':<32} {'type':<9} {'F1':>6} {'prec':>6} {'rec':>6}  notes")
    print("-" * 92)
    for name, page_type, result, problems in rows:
        flag = "  <-- " + "; ".join(problems) if problems else ""
        print(
            f"{name:<32} {page_type:<9} {result.f1:>6.3f} "
            f"{result.precision:>6.3f} {result.recall:>6.3f}{flag}"
        )

    headline = sum(scores.values()) / len(scores) if scores else 0.0
    print("-" * 92)
    print(f"HEADLINE EXTRACTION F1: {headline:.4f}  across {len(scores)} fixtures")
    return headline, scores, failures


def compare() -> int:
    """Routed extraction against the heuristic baseline, per page type."""
    print(f"{'fixture':<32} {'type':<9} {'routed':>8} {'heuristic':>10} {'delta':>8}")
    print("-" * 74)

    wins: dict[str, list[float]] = {}
    for fixture in all_fixtures():
        routed, _ = run_fixture(fixture)
        base = heuristic_only(fixture)
        delta = routed.f1 - base.f1
        wins.setdefault(str(fixture.page_type), []).append(delta)
        print(
            f"{fixture.name:<32} {str(fixture.page_type):<9} "
            f"{routed.f1:>8.3f} {base.f1:>10.3f} {delta:>+8.3f}"
        )

    print("-" * 74)
    print("Mean delta by page type (routed minus heuristic):")
    for page_type, deltas in sorted(wins.items()):
        mean = sum(deltas) / len(deltas)
        print(f"  {page_type:<10} {mean:>+7.3f}")

    # The Phase 5 acceptance criterion names forum and product specifically.
    ok = True
    for page_type in ("forum", "product"):
        deltas = wins.get(page_type)
        if not deltas:
            continue
        mean = sum(deltas) / len(deltas)
        if mean <= 0:
            print(f"\n{page_type} does NOT beat the heuristic baseline ({mean:+.3f}).")
            ok = False
    if ok:
        print("\nForum and product both beat the heuristic baseline.")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Extraction quality harness")
    parser.add_argument("--baseline", action="store_true", help="rewrite the baseline")
    parser.add_argument("--check", action="store_true", help="fail on regression")
    parser.add_argument("--compare", action="store_true", help="routed vs heuristic")
    args = parser.parse_args()

    if args.compare:
        return compare()

    headline, scores, failures = report()

    if failures:
        print("\nHARD ASSERTION FAILURES:", file=sys.stderr)
        for failure in failures:
            print(f"  ✗ {failure}", file=sys.stderr)

    if args.baseline:
        BASELINE_PATH.write_text(
            json.dumps({"headline": round(headline, 4), "fixtures": scores}, indent=2) + "\n"
        )
        print(f"\nBaseline written to {BASELINE_PATH.name}: {headline:.4f}")
        return 0

    if args.check:
        if not BASELINE_PATH.exists():
            print("\nNo baseline recorded. Run with --baseline first.", file=sys.stderr)
            return 1
        stored = json.loads(BASELINE_PATH.read_text())
        previous = float(stored["headline"])

        print(f"\nBaseline {previous:.4f} -> current {headline:.4f} ({headline - previous:+.4f})")

        regressed = [
            f"{name}: {stored['fixtures'].get(name, 0.0):.3f} -> {value:.3f}"
            for name, value in scores.items()
            if value < stored["fixtures"].get(name, 0.0) - REGRESSION_TOLERANCE
        ]
        if regressed:
            print("\nPER-FIXTURE REGRESSIONS:", file=sys.stderr)
            for line in regressed:
                print(f"  ✗ {line}", file=sys.stderr)

        if failures or regressed or headline < previous - REGRESSION_TOLERANCE:
            print("\nExtraction quality regressed. It should only ever go up.", file=sys.stderr)
            return 1
        print("Extraction quality held.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
