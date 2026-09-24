# Backups

Two databases, one schedule. The engine's Postgres holds jobs, pages, proxy scores
and the usage ledger; the app's MariaDB holds accounts, credits, plans and monitors.
A backup of one without the other restores to a system that can fetch but cannot
bill, or bill but cannot fetch — so they are dumped together under one timestamp.

## Install

```
sudo mkdir -p /var/backups/snoop && sudo chown "$USER" /var/backups/snoop
```

Then, in `crontab -e`:

```
# Nightly dump at 03:15, and a real restore an hour later.
15 3 * * *  ENGINE_DATABASE_URL=... APP_DB_USER=... BACKUP_REMOTE=... /srv/snoop/deploy/backup/backup.sh         >> /var/log/snoop-backup.log 2>&1
15 4 * * *  ENGINE_DATABASE_URL=...                                  /srv/snoop/deploy/backup/verify-restore.sh  >> /var/log/snoop-backup.log 2>&1
```

The verify job is not optional decoration. The failure it exists to catch is a dump
that has been silently writing nothing for weeks, and there is exactly one moment
you find that out otherwise.

## Offsite

`BACKUP_REMOTE` is an rclone destination — `b2:snoop-backups`, `s3:snoop-backups`.
Leave it empty and the script says so loudly on every run, because a dump sitting on
the same disk as the database is not a backup, it is a second copy of the thing that
is about to fail.

```
rclone config          # once, to add the remote
```

## What "verified" means here

`verify-restore.sh` restores the newest dump into a database it creates, counts rows
in six tables, and compares them against the live source. It never touches the live
database. On success it removes its scratch copy; on failure it keeps it and tells
you the name, because the first thing anyone wants is to look inside the restore
that went wrong.

Proven against a deliberately truncated dump on 4 September 2026: `usage_events`
came back 596 → 0 and the check failed, which is the whole point. A restore that
exits 0 having restored an empty schema is the failure mode this catches.

## Not yet done

**WAL archiving.** The spec asks for point-in-time recovery "once the data matters".
Nightly dumps mean up to 24 hours of loss. Worth setting up before real customer
data lands, not before.
