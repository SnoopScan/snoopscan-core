#!/usr/bin/env python3
"""Write LICENSE-PROPRIETARY from engine/licensing.py.

    python tools/render_licence.py           # write
    python tools/render_licence.py --check   # exit 1 if stale (CI)

The list of withheld modules lives in exactly one place. This file makes the
licence text a view of it rather than a fifth copy somebody has to remember.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.licensing import render_licence  # noqa: E402

TARGET = ROOT / "LICENSE-PROPRIETARY"


def main() -> int:
    rendered = render_licence()
    if "--check" in sys.argv:
        current = TARGET.read_text() if TARGET.exists() else ""
        if current != rendered:
            print("LICENSE-PROPRIETARY is stale — run: python tools/render_licence.py")
            return 1
        print("LICENSE-PROPRIETARY is up to date.")
        return 0
    TARGET.write_text(rendered)
    print(f"Wrote {TARGET.relative_to(ROOT)} ({len(rendered.splitlines())} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
