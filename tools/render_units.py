#!/usr/bin/env python3
"""Write the systemd units from engine/services.py.

    python tools/render_units.py           # write
    python tools/render_units.py --check   # exit 1 if stale (CI)

launchd runs the same three services on a laptop from
`deploy/launchd/*.plist.template`. Both supervisors read one definition, so a
worker cannot end up running a different module depending on the machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.services import SERVICES  # noqa: E402

OUT = ROOT / "deploy" / "systemd"

TEMPLATE = """\
# {description}. GENERATED from engine/services.py — run tools/render_units.py.
#
# Edit the user and the two paths, then:
#   sudo cp deploy/systemd/{unit} /etc/systemd/system/
#   sudo systemctl daemon-reload && sudo systemctl enable --now {name}

[Unit]
Description={description}
After={after}
Wants={wants}

[Service]
Type=simple
User=snoopscan
Group=snoopscan
WorkingDirectory=/srv/snoopscan
# python -m, never a console script: pip writes those as `#!/bin/sh` wrappers
# when the venv path contains a space, and the supervisor cannot exec them.
ExecStart=/srv/snoopscan/.venv/bin/python {args}
Restart=always
RestartSec=3
# A stuck fetch must not take the box with it.
MemoryMax={memory}
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:/var/log/snoopscan/{name}.log
StandardError=append:/var/log/snoopscan/{name}.log

[Install]
WantedBy=multi-user.target
"""

# The worker holds browsers; the other two do not.
MEMORY = {"api": "2G", "worker": "4G", "scheduler": "1G"}


def render(service) -> str:
    wants = " ".join(a for a in service.after if a != "network.target")
    return TEMPLATE.format(
        description=service.description,
        unit=f"snoopscan-{service.name}.service",
        name=f"snoopscan-{service.name}",
        after=" ".join(service.after),
        wants=wants,
        args=" ".join(service.args),
        memory=MEMORY.get(service.name, "1G"),
    )


def main() -> int:
    stale = []
    for service in SERVICES:
        target = OUT / f"snoopscan-{service.name}.service"
        rendered = render(service)
        if "--check" in sys.argv:
            if not target.exists() or target.read_text() != rendered:
                stale.append(target.name)
            continue
        target.write_text(rendered)
        print(f"Wrote {target.relative_to(ROOT)}")
    if stale:
        print("Stale systemd units: " + ", ".join(stale))
        print("Run: python tools/render_units.py")
        return 1
    if "--check" in sys.argv:
        print("systemd units are up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
