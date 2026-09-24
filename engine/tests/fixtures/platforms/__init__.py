"""Captured platform responses. The big HTML pages are gzipped: a 2.3MB
browser-rendered Amazon page is evidence worth keeping and not worth carrying
uncompressed in every clone."""

from __future__ import annotations

import gzip
from pathlib import Path

HERE = Path(__file__).parent


def load(name: str) -> str:
    """Read a fixture by its logical name, gzipped or not."""
    plain = HERE / name
    if plain.exists():
        return plain.read_text()
    return gzip.decompress((HERE / (name + ".gz")).read_bytes()).decode("utf-8")
