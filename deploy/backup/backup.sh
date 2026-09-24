#!/usr/bin/env bash
#
# Nightly backup of both databases (10-build-plan.md, Backups).
#
# Two databases, not one: the engine's Postgres holds jobs, pages, proxy scores
# and the usage ledger; the app's MariaDB holds accounts, credits, plans and
# monitors. A backup of either alone restores to a system that cannot bill or
# cannot fetch, so they are taken together and named with the same timestamp.
#
# Custom-format dumps (-Fc), not plain SQL: they restore selectively, in
# parallel, and can be inspected with `pg_restore --list` without a database to
# restore into. That last part is what makes verification cheap.
#
# Environment:
#   ENGINE_DATABASE_URL   postgres connection string          (required)
#   APP_DATABASE          mariadb database name               (default snoopbot_app)
#   APP_DB_USER           mariadb user                        (default from the app)
#   APP_DB_PASSWORD       mariadb password, if the user needs one
#   BACKUP_DIR            where dumps land                    (default /var/backups/snoop)
#   BACKUP_RETAIN_DAYS    local retention                     (default 30)
#   BACKUP_REMOTE         rclone/s3 destination, e.g. b2:snoop-backups
#                         EMPTY = local only, and the script says so loudly.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/snoop}"
RETAIN="${BACKUP_RETAIN_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"

engine_out="$BACKUP_DIR/engine-$STAMP.dump"
app_out="$BACKUP_DIR/app-$STAMP.sql.gz"

echo "→ engine (postgres)"
pg_dump --format=custom --no-owner --no-privileges \
        --file="$engine_out" "${ENGINE_DATABASE_URL:?ENGINE_DATABASE_URL is required}"

echo "→ app (mariadb)"
# Defaulting to root was wrong: on this stack root authenticates over a unix
# socket and the app has its own user, so the nightly job failed on a
# permission error rather than on anything to do with the data. Use the same
# credentials the application does.
app_auth=(-u "${APP_DB_USER:-$(whoami)}")
[ -n "${APP_DB_PASSWORD:-}" ] && app_auth+=("-p${APP_DB_PASSWORD}")
mysqldump --single-transaction --quick --routines --events \
          "${app_auth[@]}" "${APP_DATABASE:-snoopbot_app}" | gzip -9 > "$app_out"

# A dump that cannot be listed is not a backup. This costs milliseconds and
# catches a truncated write, which is the failure that otherwise stays hidden
# until the night you need it.
pg_restore --list "$engine_out" > /dev/null
gzip -t "$app_out"

echo "→ verified: $(du -h "$engine_out" | cut -f1) engine, $(du -h "$app_out" | cut -f1) app"

if [ -n "${BACKUP_REMOTE:-}" ]; then
    echo "→ offsite: $BACKUP_REMOTE"
    rclone copy "$engine_out" "$BACKUP_REMOTE/" --quiet
    rclone copy "$app_out" "$BACKUP_REMOTE/" --quiet
else
    # Said loudly on purpose. A backup on the same disk as the database is not
    # a backup — it is a second copy of the thing that is about to fail.
    echo "!! BACKUP_REMOTE is not set: these dumps are ON THE SAME MACHINE as the databases."
fi

find "$BACKUP_DIR" -name 'engine-*.dump' -mtime "+$RETAIN" -delete
find "$BACKUP_DIR" -name 'app-*.sql.gz' -mtime "+$RETAIN" -delete
echo "→ done, keeping $RETAIN days"
