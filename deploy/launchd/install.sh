#!/usr/bin/env bash
# Install SnoopScan's API, worker and scheduler as launchd agents.
#
# Why: these are long-running services. Run from a terminal tab they die when
# the tab closes, and a worker left behind from an older session keeps eating
# jobs off the shared queue on stale code — which is exactly how a crawl was
# orphaned on 6 Sep 2026. launchd owns them instead: started at login,
# restarted on crash, one of each, killable by name.
#
#   ./deploy/launchd/install.sh          install and start
#   ./deploy/launchd/install.sh --stop   stop and remove
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/snoopscan"
# Derived from the templates on disk, never listed twice. A fourth service
# added as a template but forgotten here would simply not be installed, and
# nothing would say so.
SERVICES=()
for t in "$REPO"/deploy/launchd/com.snoopscan.*.plist.template; do
  svc="${t##*/com.snoopscan.}"
  SERVICES+=("${svc%.plist.template}")
done
if [[ ${#SERVICES[@]} -eq 0 ]]; then
  echo "No plist templates found in $REPO/deploy/launchd" >&2
  exit 1
fi
DOMAIN="gui/$(id -u)"

stop_all() {
  for svc in "${SERVICES[@]}"; do
    label="com.snoopscan.$svc"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
    echo "  removed $label"
  done
}

if [[ "${1:-}" == "--stop" ]]; then
  echo "Stopping SnoopScan agents:"
  stop_all
  echo "Done. Postgres and Redis are Homebrew's, untouched."
  exit 0
fi

if [[ ! -x "$REPO/.venv/bin/python" ]]; then
  echo "No venv at $REPO/.venv — run: uv venv --python 3.12 && uv pip install -e '.[dev]'" >&2
  exit 1
fi

# Unload our own agents BEFORE looking for strays, so reinstalling is
# idempotent. Without this the guards below match the very processes this
# script started last time — `python -m uvicorn engine.api.app:app` contains
# "uvicorn engine.api.app" — and every run after the first refuses itself.
stop_all >/dev/null
# launchd's SIGTERM is asynchronous; give the ports and the queue a moment to
# come free before deciding that whatever is left is somebody else's.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  pgrep -f "uvicorn engine.api.app|engine.workers." >/dev/null 2>&1 || break
  sleep 1
done

# Anything STILL running was started by hand. A manually-run API would fight
# the agent for port 8099 and both would thrash; a stale worker keeps eating
# jobs off the shared queue on old code.
if pgrep -f "uvicorn engine.api.app" >/dev/null 2>&1; then
  echo "An API is already running from a terminal. Stop it first (Ctrl-C in its tab)," >&2
  echo "or: pkill -f 'uvicorn engine.api.app'" >&2
  exit 1
fi
if pgrep -f "engine.workers." >/dev/null 2>&1; then
  echo "A worker or scheduler is already running from a terminal. Stop those first:" >&2
  echo "  pkill -f 'engine.workers.'" >&2
  exit 1
fi

mkdir -p "$AGENTS" "$LOGS"
echo "Installing from $REPO"

for svc in "${SERVICES[@]}"; do
  label="com.snoopscan.$svc"
  template="$REPO/deploy/launchd/$label.plist.template"
  target="$AGENTS/$label.plist"
  # The repo path is machine-specific, so it is substituted at install time
  # rather than committed. `|` as the sed delimiter: the path contains slashes.
  sed -e "s|__REPO__|$REPO|g" -e "s|__LOGS__|$LOGS|g" "$template" > "$target"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$target"
  echo "  started $label"
done

echo
echo "Logs:    $LOGS/{api,worker,scheduler}.log"
echo "Status:  launchctl list | grep snoopscan"
echo "Check:   curl -s localhost:8099/ready | jq"
echo "Stop:    ./deploy/launchd/install.sh --stop"
