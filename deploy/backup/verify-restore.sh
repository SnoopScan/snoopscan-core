#!/usr/bin/env bash
#
# Restore the latest engine dump into a THROWAWAY database and check it arrived.
#
# The spec's line is "test the restore — a backup that has never been restored
# is a hypothesis", and this is what turns the hypothesis into a fact. It runs
# on a schedule, not only when someone remembers, because the failure it catches
# is a dump that has been quietly writing zero useful bytes for weeks.
#
# It NEVER touches the live database. It creates its own, restores into that,
# and compares row counts against the source.
#
# On SUCCESS it removes its own scratch database — this runs nightly, and a
# script that leaves one behind every night has quietly become a disk problem.
# On FAILURE it keeps it, because the first thing anyone will want is to look
# inside the restore that went wrong.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/snoop}"
SOURCE_URL="${ENGINE_DATABASE_URL:?ENGINE_DATABASE_URL is required}"
ADMIN_URL="${ADMIN_DATABASE_URL:-${SOURCE_URL%/*}/postgres}"

latest="$(ls -1t "$BACKUP_DIR"/engine-*.dump 2>/dev/null | head -1 || true)"
[ -n "$latest" ] || { echo "no engine dump found in $BACKUP_DIR"; exit 1; }
echo "→ restoring $(basename "$latest")"

scratch="restorecheck_$(date -u +%Y%m%d%H%M%S)"
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -qc "CREATE DATABASE $scratch"
target="${SOURCE_URL%/*}/$scratch"

# --no-owner: the dump's roles do not exist on a recovery box, and a restore
# that fails on ownership is a restore that fails at 3am for a cosmetic reason.
pg_restore --no-owner --no-privileges --dbname="$target" "$latest" 2>&1 | grep -v "^$" || true

# The real check. A dump can restore its SCHEMA cleanly and carry no rows —
# that is what a broken --data-only flag or a half-written file looks like, and
# an exit code of 0 says nothing about it.
tables=(api_keys owners pages domain_profiles usage_events credit_costs)
fail=0
printf '  %-18s %10s %10s\n' TABLE SOURCE RESTORED
for t in "${tables[@]}"; do
    src="$(psql "$SOURCE_URL" -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo skip)"
    dst="$(psql "$target"     -tAc "SELECT count(*) FROM $t" 2>/dev/null || echo skip)"
    [ "$src" = skip ] && continue
    printf '  %-18s %10s %10s' "$t" "$src" "$dst"
    if [ "$src" = "$dst" ]; then echo "  ok"; else echo "  MISMATCH"; fail=1; fi
done

echo
if [ "$fail" = 0 ]; then
    psql "$ADMIN_URL" -qc "DROP DATABASE $scratch"
    echo "→ RESTORE VERIFIED (scratch database removed)"
else
    echo "→ RESTORE FAILED — row counts differ"
    echo "   the restored copy is kept for inspection: $scratch"
    echo "   remove it with:  psql \"$ADMIN_URL\" -c 'DROP DATABASE $scratch'"
    exit 1
fi
