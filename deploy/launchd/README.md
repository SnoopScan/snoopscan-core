# Running SnoopScan as launchd agents (macOS)

The API, the HTTP worker and the scheduler are long-running services. Started
from a terminal tab they die when the tab closes — and a worker left over from
an older session keeps consuming jobs off the shared Redis queue while running
stale code. That is not hypothetical: on 6 Sep 2026 a 500-page crawl was popped
by a 14-hour-old worker and dropped, leaving the job `queued` forever.

launchd fixes the ownership problem: one of each, started at login, restarted on
crash, addressable by name rather than by hunting PIDs.

```bash
./deploy/launchd/install.sh          # install and start all three
./deploy/launchd/install.sh --stop   # stop and remove all three
```

The installer refuses to run while an API, worker or scheduler is already
running from a terminal — two of either would fight over port 8099 and the
queue. Stop those first (`pkill -f 'engine.workers.'`).

## Everyday commands

| | |
|---|---|
| Are they up? | `launchctl list \| grep snoopscan` |
| Is the stack healthy? | `curl -s localhost:8099/ready \| jq` |
| Logs | `tail -f ~/Library/Logs/snoopscan/{api,worker,scheduler}.log` |
| Restart one | `launchctl kickstart -k gui/$(id -u)/com.snoopscan.worker` |
| Stop one | `launchctl bootout gui/$(id -u)/com.snoopscan.worker` |

`/ready` lists the live workers by name. **One entry is correct.** More than one
means a stray worker is attached — the thing these agents exist to prevent.

## What is deliberately not here

- **Postgres and Redis** are Homebrew services with their own LaunchAgents and
  already start at login. The installer does not touch them. launchd has no
  dependency ordering, so an agent that starts before Postgres is ready simply
  exits and `KeepAlive` restarts it ten seconds later.
- **`--reload`** is absent from the API agent. It is a development convenience
  that runs a second supervising process; a service manager should own one.
- **The plists are templates.** The repo path is machine-specific and contains a
  space, so it is substituted at install time. The `ProgramArguments` array form
  means the space never reaches a shell.

## Linux servers

These are macOS agents. The production deployment on Linux wants systemd units
with `After=postgresql.service redis.service` and `Restart=always` — the same
three services, the same one-of-each rule.
