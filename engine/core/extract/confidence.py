"""Extraction confidence scoring (04-extraction.md section 5).

Returns 0-1. Consumers treat anything below 0.5 as suspect, and the score also
feeds block detection: a 200 that scores 0.1 is very likely a challenge or
consent page rather than a genuine extraction failure.

Weights come straight from the spec table and are kept as named constants so a
change is visible in review rather than buried in an expression.
"""

from __future__ import annotations

from dataclasses import dataclass

W_TEXT_RATIO = 0.25
W_LENGTH_BASELINE = 0.25
W_STRUCTURE = 0.20
W_BOILERPLATE = 0.20
W_METADATA = 0.10


@dataclass
class ConfidenceInputs:
    extracted_chars: int
    raw_text_chars: int
    # Structure present in source vs retained in output.
    source_headings: int = 0
    source_lists: int = 0
    source_tables: int = 0
    output_headings: int = 0
    output_lists: int = 0
    output_tables: int = 0
    boilerplate_hits: int = 0
    has_title: bool = False
    has_language: bool = False
    has_author_or_date: bool = False
    author_or_date_applicable: bool = False
    # Domain baseline, when one exists.
    baseline_mean: int | None = None
    baseline_stdev: int | None = None


def _text_ratio_score(extracted: int, raw: int) -> float:
    """Both extremes are failures.

    A ratio near 1.0 means we kept everything including nav — a precision
    failure. Under 0.05 means we kept almost nothing — a recall failure.
    """
    if raw <= 0:
        return 0.0
    ratio = extracted / raw
    if ratio <= 0.02 or ratio >= 0.995:
        return 0.0
    if ratio < 0.05:
        return 0.2
    if ratio > 0.95:
        return 0.3
    # Healthy band is roughly 0.15-0.85, peaking around 0.5.
    if 0.15 <= ratio <= 0.85:
        return 1.0
    if ratio < 0.15:
        return 0.5 + (ratio - 0.05) / 0.10 * 0.5
    return 1.0 - (ratio - 0.85) / 0.10 * 0.7


def _baseline_score(chars: int, mean: int | None, stdev: int | None) -> float:
    """Distance from the domain's typical content length, in stdevs."""
    if not mean or not stdev or stdev <= 0:
        # No baseline yet: neutral rather than penalising a first fetch.
        return 0.6
    deviations = abs(chars - mean) / stdev
    if deviations <= 1.0:
        return 1.0
    if deviations >= 4.0:
        return 0.0
    return 1.0 - (deviations - 1.0) / 3.0


def _structure_score(inputs: ConfidenceInputs) -> float:
    """Did structure present in the source survive into the output?"""
    pairs = (
        (inputs.source_headings, inputs.output_headings),
        (inputs.source_lists, inputs.output_lists),
        (inputs.source_tables, inputs.output_tables),
    )
    applicable = [(src, out) for src, out in pairs if src > 0]
    if not applicable:
        # Nothing structural in the source, so nothing could be lost.
        return 1.0
    retained = sum(min(out / src, 1.0) for src, out in applicable)
    return retained / len(applicable)


def _boilerplate_score(hits: int) -> float:
    if hits <= 0:
        return 1.0
    if hits >= 5:
        return 0.0
    return 1.0 - hits * 0.2


def _metadata_score(inputs: ConfidenceInputs) -> float:
    checks = [inputs.has_title, inputs.has_language]
    if inputs.author_or_date_applicable:
        checks.append(inputs.has_author_or_date)
    return sum(1.0 for c in checks if c) / len(checks)


def score(inputs: ConfidenceInputs) -> float:
    total = (
        _text_ratio_score(inputs.extracted_chars, inputs.raw_text_chars) * W_TEXT_RATIO
        + _baseline_score(inputs.extracted_chars, inputs.baseline_mean, inputs.baseline_stdev)
        * W_LENGTH_BASELINE
        + _structure_score(inputs) * W_STRUCTURE
        + _boilerplate_score(inputs.boilerplate_hits) * W_BOILERPLATE
        + _metadata_score(inputs) * W_METADATA
    )
    return round(min(max(total, 0.0), 1.0), 3)
