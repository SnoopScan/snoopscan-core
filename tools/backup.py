#!/usr/bin/env python3
"""Backup and restore, with a restore that is actually exercised.

10-build-plan.md: "Test the restore. A backup that has never been restored is
a hypothesis." So `--verify` does not inspect the dump file — it restores it
into a scratch database, counts the rows, and drops it again. Anything less
tests that pg_dump exits zero, which is not the thing you need to know at 3am.

    python tools/backup.py --dump /path/backups        # write a dump
    python tools/backup.py --verify /path/backup.sql   # restore and check
    python tools/backup.py --list /path/backups        # what exists

One table matters more than the rest. `suppression_list` records people who
asked not to be contacted; losing it means contacting them again, which is the
failure the whole compliance posture exists to prevent. The verifier checks it
survived and refuses to pass if it is empty when the source was not.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

# Tables whose absence after a restore is a failure, not a warning.
CRITICAL_TABLES = ("api_keys", "suppression_list", "contacts", "companies", "domain_profiles")

# Never deleted, ever. See 11-compliance.md section 3.
IRREPLACEABLE = "suppression_list"


def dsn_parts(dsn: str) -> dict[str, str]:
    parts = urlsplit(dsn)
    return {
        "host": parts.hostname or "localhost",
        "port": str(parts.port or 5432),
        "user": parts.username or os.environ.get("USER", "postgres"),
        "database": (parts.path or "/").lstrip("/") or "postgres",
        "password": parts.password or "",
    }


def psql_env(parts: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    if parts["password"]:
        env["PGPASSWORD"] = parts["password"]
    return env


def run(command: list[str], env: dict[str, str], quiet: bool = False) -> tuple[int, str]:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command, env=env, capture_output=True, text=True
    )
    if result.returncode != 0 and not quiet:
        print(result.stderr.strip()[:600], file=sys.stderr)
    return result.returncode, result.stdout


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        print(f"{name} is not on PATH. Install the PostgreSQL client tools.", file=sys.stderr)
        raise SystemExit(2)
    return path


def dump(dsn: str, target_dir: Path) -> Path:
    parts = dsn_parts(dsn)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = target_dir / f"{parts['database']}-{stamp}.sql"

    code, _ = run(
        [
            require_tool("pg_dump"),
            "-h",
            parts["host"],
            "-p",
            parts["port"],
            "-U",
            parts["user"],
            "-d",
            parts["database"],
            "--no-owner",
            "--no-privileges",
            "-f",
            str(path),
        ],
        psql_env(parts),
    )
    if code != 0:
        raise SystemExit("pg_dump failed")

    size_mb = path.stat().st_size / 1_048_576
    print(f"Wrote {path.name} ({size_mb:.1f}MB)")
    return path


def count_rows(dsn: str, database: str, table: str) -> int:
    parts = dsn_parts(dsn)
    code, out = run(
        [
            require_tool("psql"),
            "-h",
            parts["host"],
            "-p",
            parts["port"],
            "-U",
            parts["user"],
            "-d",
            database,
            "-tAc",
            # Table name comes from the fixed CRITICAL_TABLES tuple above,
            # never from input, and an identifier cannot be bound as a
            # parameter in any case.
            f"SELECT count(*) FROM {table}",  # noqa: S608
        ],
        psql_env(parts),
        quiet=True,
    )
    if code != 0:
        return -1
    return int(out.strip() or 0)


def verify(dsn: str, dump_path: Path) -> int:
    """Restore into a scratch database and check what actually arrived.

    This is the whole point. A dump that parses is not a backup; a dump that
    restores with the rows present is.
    """
    if not dump_path.is_file():
        print(f"No such dump: {dump_path}", file=sys.stderr)
        return 1

    parts = dsn_parts(dsn)
    env = psql_env(parts)
    scratch = f"restore_check_{datetime.now(UTC).strftime('%H%M%S')}"
    source_db = parts["database"]

    print(f"Restoring {dump_path.name} into scratch database {scratch}")
    createdb = require_tool("createdb")
    dropdb = require_tool("dropdb")
    psql = require_tool("psql")

    code, _ = run(
        [createdb, "-h", parts["host"], "-p", parts["port"], "-U", parts["user"], scratch],
        env,
    )
    if code != 0:
        print("could not create the scratch database", file=sys.stderr)
        return 1

    try:
        code, _ = run(
            [
                psql,
                "-h",
                parts["host"],
                "-p",
                parts["port"],
                "-U",
                parts["user"],
                "-d",
                scratch,
                "-v",
                "ON_ERROR_STOP=1",
                "-q",
                "-f",
                str(dump_path),
            ],
            env,
        )
        if code != 0:
            print("RESTORE FAILED — the backup is not usable.", file=sys.stderr)
            return 1

        print(f"\n{'table':<22} {'source':>8} {'restored':>9}")
        print("-" * 42)
        failures: list[str] = []

        for table in CRITICAL_TABLES:
            source_count = count_rows(dsn, source_db, table)
            restored_count = count_rows(dsn, scratch, table)
            print(f"{table:<22} {source_count:>8} {restored_count:>9}")

            if restored_count < 0:
                failures.append(f"{table} is missing from the restore")
            elif restored_count < source_count:
                failures.append(f"{table} lost rows: {source_count} -> {restored_count}")

        # The one that must never be lost.
        source_suppressions = count_rows(dsn, source_db, IRREPLACEABLE)
        restored_suppressions = count_rows(dsn, scratch, IRREPLACEABLE)
        if source_suppressions > 0 and restored_suppressions < source_suppressions:
            failures.append(
                f"{IRREPLACEABLE} did not survive. Losing it means contacting "
                f"people who explicitly asked not to be."
            )

        if failures:
            print("\nRESTORE VERIFICATION FAILED:", file=sys.stderr)
            for failure in failures:
                print(f"  ✗ {failure}", file=sys.stderr)
            return 1

        print("\nRestore verified: every critical table came back intact.")
        return 0
    finally:
        run([dropdb, "-h", parts["host"], "-p", parts["port"], "-U", parts["user"], scratch], env)
        print(f"Scratch database {scratch} dropped.")


def list_backups(directory: Path) -> int:
    if not directory.is_dir():
        print(f"No such directory: {directory}", file=sys.stderr)
        return 1
    dumps = sorted(directory.glob("*.sql"), reverse=True)
    if not dumps:
        print("No backups found.")
        return 1
    print(f"{'file':<44} {'size':>9}  age")
    for path in dumps:
        stat = path.stat()
        age_days = (datetime.now(UTC).timestamp() - stat.st_mtime) / 86_400
        print(f"{path.name:<44} {stat.st_size / 1_048_576:>7.1f}MB  {age_days:.1f}d")
    return 0


def main() -> int:
    from engine.settings import settings

    parser = argparse.ArgumentParser(description="Backup and verified restore")
    parser.add_argument("--dump", metavar="DIR", help="write a dump into DIR")
    parser.add_argument("--verify", metavar="FILE", help="restore FILE and check it")
    parser.add_argument("--list", metavar="DIR", help="list dumps in DIR")
    parser.add_argument("--dsn", default=settings.asyncpg_dsn)
    args = parser.parse_args()

    if args.list:
        return list_backups(Path(args.list))
    if args.verify:
        return verify(args.dsn, Path(args.verify))
    if args.dump:
        path = dump(args.dsn, Path(args.dump))
        # Verify immediately. A backup nobody has restored is a hypothesis,
        # and the cheapest moment to find out is now.
        return verify(args.dsn, path)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
