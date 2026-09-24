"""Code-block fidelity repair (04-extraction.md section 4a).

The heuristic extractor is good at deciding which prose belongs on the page
and weak at code. Two failures observed on a docs fixture:

  * a single-line `<pre><code>` came back as INLINE code, losing the block
    entirely — a consumer parsing fenced blocks out of the markdown would not
    see it at all
  * `class="language-python"` was dropped, so the fence had no language

Both are recoverable from the source HTML, so this repairs the output rather
than replacing an extractor that is otherwise doing its job well.

Fidelity here is not cosmetic. Code and tables are exactly where structure-
destroying extraction does the most damage, because the loss is invisible in
plain text.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

# Class conventions that carry a language, in the order they are worth trying.
_LANGUAGE_PREFIXES = ("language-", "lang-", "highlight-source-", "highlight-", "brush:")

# Values that name a highlighter rather than a language.
_NOT_LANGUAGES = frozenset({"js", "source", "highlight", "code", "pre", "syntax"})


def language_of(node: object) -> str:
    """The language for a <code> element, from its class attributes."""
    attributes = getattr(node, "attributes", {}) or {}
    classes = (attributes.get("class") or "").split()
    for candidate in classes:
        lowered = candidate.lower()
        for prefix in _LANGUAGE_PREFIXES:
            if lowered.startswith(prefix):
                language = lowered[len(prefix) :].strip(":")
                if language and language not in _NOT_LANGUAGES:
                    return language
    # Some sites put a bare language name in the class with no prefix.
    for candidate in classes:
        lowered = candidate.lower()
        if lowered in {"python", "bash", "sh", "json", "yaml", "sql", "go", "rust", "ruby"}:
            return lowered
    return ""


def source_code_blocks(html: str) -> list[tuple[str, str]]:
    """Every <pre> block in the source, as (language, text)."""
    tree = HTMLParser(html)
    out: list[tuple[str, str]] = []
    for pre in tree.css("pre"):
        code = pre.css_first("code") or pre
        text = code.text(deep=True)
        if not text or not text.strip():
            continue
        out.append((language_of(code), text.strip("\n")))
    return out


_FENCE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)


def _fenced_bodies(markdown: str) -> list[str]:
    return [m.group(2).strip() for m in _FENCE.finditer(markdown)]


def _normalise(text: str) -> str:
    """Collapse whitespace for comparison.

    Extractors rewrap and drop blank lines, so the same code block rarely
    survives byte-identical. Comparing normalised forms is what lets a
    language hint be reattached to a block that did survive.
    """
    return re.sub(r"\s+", " ", text).strip()


def _add_language(markdown: str, body: str, language: str) -> str:
    """Attach a language to the existing fence whose body matches."""
    target = _normalise(body)

    def replace(match: re.Match[str]) -> str:
        info, content = match.group(1), match.group(2)
        if info.strip() or _normalise(content) != target:
            return match.group(0)
        return f"```{language}\n{content}```"

    return _FENCE.sub(replace, markdown, count=0)


def restore(markdown: str, html: str) -> str:
    """Put back code blocks the extractor flattened, and their languages.

    Only ever upgrades: a block already fenced with a language is left alone,
    and nothing is inserted that was not in the source.
    """
    blocks = source_code_blocks(html)
    if not blocks:
        return markdown

    existing = _fenced_bodies(markdown)
    repaired = markdown

    for language, text in blocks:
        stripped = text.strip()
        if not stripped:
            continue

        normalised = _normalise(stripped)
        already = any(_normalise(body) == normalised for body in existing)
        fence = f"```{language}\n{stripped}\n```" if language else f"```\n{stripped}\n```"

        if already:
            # Present but possibly missing its language. Add it rather than
            # rewriting a block that is otherwise correct.
            if language:
                repaired = _add_language(repaired, stripped, language)
            continue

        single_line = stripped if "\n" not in stripped else None
        if single_line:
            # The inline-code case: `pip install x` should have been a block.
            inline = f"`{single_line}`"
            if inline in repaired:
                repaired = repaired.replace(inline, fence, 1)
                continue

        # Present as bare text, or absent. Only substitute when we can see it,
        # so nothing is invented.
        if stripped in repaired:
            repaired = repaired.replace(stripped, fence, 1)

    return repaired


def count_fenced(markdown: str) -> int:
    return len(_fenced_bodies(markdown))
