#!/usr/bin/env python3
"""Consistent, timestamped backup of the benchd SQLite system of record.

benchd keeps every run, its telemetry/logs/events/artifacts and the runner
registry in one SQLite file. There was no backup of it and no exercised restore,
so a corrupt or lost file meant losing the whole history.

This uses sqlite3's online backup API, which is safe against a *live* benchd in
WAL mode: it copies a transactionally-consistent snapshot instead of racing a
byte-for-byte file copy against the writer.

    python3 server/backup_benchd.py --db server/data/benchd.sqlite \
        --out /var/backups/benchd --keep 7

`--keep N` keeps the N newest snapshots in the output directory (0 keeps all).
The restore procedure is in docs/BACKUP_RESTORE.md.
"""
import argparse
import os
import re
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "data", "benchd.sqlite")
DEFAULT_OUT = os.path.join(HERE, "data", "backups")

PREFIX = "benchd-"
SUFFIX = ".sqlite"
NAME_RE = re.compile(r"^benchd-\d{8}-\d{6}\.sqlite$")


def snapshot_name(when=None):
    """Timestamped snapshot filename (sortable, second resolution)."""
    return "%s%s%s" % (PREFIX, time.strftime("%Y%m%d-%H%M%S", time.localtime(when)), SUFFIX)


def rotate(out_dir, keep):
    """Delete all but the `keep` newest snapshots. Returns the removed paths."""
    if keep is None or keep <= 0:
        return []
    names = sorted(n for n in os.listdir(out_dir) if NAME_RE.match(n))
    removed = []
    for n in names[:-keep]:
        p = os.path.join(out_dir, n)
        try:
            os.remove(p)
            removed.append(p)
        except OSError:
            pass
    return removed


def backup(db_path, out_dir, keep=7, when=None):
    """Write a consistent snapshot of db_path into out_dir; rotate to `keep`.

    Returns the path of the new snapshot. Raises FileNotFoundError when the
    source database is missing (fail closed: no empty/zero-byte "backup").
    """
    if not os.path.isfile(db_path):
        raise FileNotFoundError("no such database: %s" % db_path)
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, snapshot_name(when))
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    rotate(out_dir, keep)
    return dest


def main(argv=None):
    ap = argparse.ArgumentParser(description="back up the benchd SQLite database")
    ap.add_argument("--db", default=os.environ.get("BENCHD_DB") or DEFAULT_DB)
    ap.add_argument("--out", default=os.environ.get("BENCHD_BACKUP_DIR") or DEFAULT_OUT)
    ap.add_argument("--keep", type=int, default=7,
                    help="keep the N newest snapshots in --out (0 keeps all)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    dest = backup(a.db, a.out, a.keep)
    if not a.quiet:
        print("[backup] wrote %s" % dest, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
