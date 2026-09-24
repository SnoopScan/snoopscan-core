"""Every module the engine imports must be a DECLARED dependency.

Two were not, and neither was noticeable while developing: `defusedxml` (the
sitemap parser) and `mcp` (the hosted MCP server, which is on by default). Both
sat in the virtualenv because something had installed them at some point, so
everything passed locally. A clean `uv sync` from the lockfile produced an
engine that raised ModuleNotFoundError on startup.

`types-defusedxml` WAS declared, which is the detail that hid it: mypy was
satisfied by the stubs and nothing checked that the library itself would be there.

This walks the imports rather than the environment, so it fails on the machine
that added the import rather than on the server that deployed it.
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT.parent / "pyproject.toml"

# Imported behind a capability check or a try/except, by design.
OPTIONAL = {"patchright", "playwright"}

# Guaranteed present by a declared package's own pin. Declaring starlette
# separately would let it drift from the version FastAPI actually requires.
TRANSITIVE = {"starlette"}


def _declared() -> set[str]:
    text = PYPROJECT.read_text()
    names: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith('"') or stripped[0] == "#":
            continue
        raw = stripped.strip('",').split("#")[0].strip().strip('"')
        name = raw.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip()
        if name and not name.startswith("types-"):
            names.add(name.lower().replace("-", "_"))
    return names


def _imported() -> set[str]:
    found: set[str] = set()
    for path in ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return {m.lower().replace("-", "_") for m in found}


def test_every_imported_package_is_declared() -> None:
    stdlib = set(sys.stdlib_module_names)
    declared = _declared()
    # Distribution names differ from import names often enough to be explicit.
    aliases = {
        "yaml": "pyyaml",
        "dns": "dnspython",
        "docx": "python_docx",
        "multipart": "python_multipart",
        "dateutil": "python_dateutil",
        "jose": "python_jose",
        "dotenv": "python_dotenv",
        "curl_cffi": "curl_cffi",
        "redis": "redis",
        "PIL": "pillow",
    }

    missing = sorted(
        name
        for name in _imported()
        if name not in stdlib
        and name != "engine"
        and name not in OPTIONAL
        and name not in TRANSITIVE
        and aliases.get(name, name) not in declared
        and name not in declared
    )

    assert missing == [], (
        "Imported but not declared in pyproject dependencies: "
        + ", ".join(missing)
        + ". A clean install would fail on these."
    )
