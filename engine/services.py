"""The long-running services, defined once.

Two supervisors need the same three facts: macOS launchd for a laptop, systemd
for a Linux server. Hand-keeping both is how the plists and the units end up
disagreeing about which module a worker runs — the same failure as every other
duplicated list in this repo, and the licence file is the cautionary tale.

Held in the open core: a self-hoster runs exactly these three.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    name: str
    # argv AFTER the interpreter. `python -m <module>` for everything, never a
    # console script: pip writes those as `#!/bin/sh` wrappers when the venv
    # path contains a space, and launchd then cannot exec them.
    args: tuple[str, ...]
    description: str
    # systemd only: what must be up first.
    after: tuple[str, ...] = ("network.target",)


SERVICES: tuple[Service, ...] = (
    Service(
        name="api",
        args=("-m", "uvicorn", "engine.api.app:app", "--host", "127.0.0.1", "--port", "8099"),
        description="SnoopScan API",
        after=("network.target", "postgresql.service", "redis-server.service"),
    ),
    Service(
        name="worker",
        args=("-m", "engine.workers.http_worker"),
        description="SnoopScan job worker (crawl and batch)",
        after=("network.target", "postgresql.service", "redis-server.service"),
    ),
    Service(
        name="scheduler",
        args=("-m", "engine.workers.scheduler"),
        description="SnoopScan maintenance scheduler",
        after=("network.target", "postgresql.service", "redis-server.service"),
    ),
)


def by_name(name: str) -> Service:
    for service in SERVICES:
        if service.name == name:
            return service
    raise KeyError(name)
