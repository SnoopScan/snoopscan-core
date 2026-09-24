"""Scraping engine — in-house web scraping, crawling and extraction."""

from __future__ import annotations

__all__ = ["__version__"]


def _version() -> str:
    """The version from pyproject.toml, never a second copy of the number.

    `__version__` feeds three things a wrong answer actually damages: the AGPL
    section 13 offer at `/v1/source` (which must name the version running
    here), the OpenAPI schema, and `/health`. Hard-coded here as well as in
    pyproject.toml, a release that bumps one and not the other makes all three
    lie, and nothing fails.

    pyproject.toml first WHEN IT IS THERE, which is only true in a source
    checkout: an editable install's metadata is a snapshot from install time, so
    reading it first meant a bumped version kept reporting the old number until
    somebody happened to reinstall. A wheel has no pyproject.toml beside the
    package, so it falls through to its own metadata — which is built from that
    same file. One number, written once, correct in both shapes.

    If neither is available it says so rather than returning a confident wrong
    version: `/v1/source` is a legal statement about what is running.
    """
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.is_file():
        import tomllib

        try:
            with pyproject.open("rb") as fh:
                return str(tomllib.load(fh)["project"]["version"])
        except (OSError, KeyError, tomllib.TOMLDecodeError):
            pass

    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("scraping-engine")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _version()
