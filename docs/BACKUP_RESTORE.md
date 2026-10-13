# Back up and restore the benchd database

`benchd` keeps the entire bench-test system of record — the runner registry, every
run, its phases, telemetry, logs, events and artifacts — in one SQLite file
(`server/data/benchd.sqlite` on the lab box, `DEFAULT_DB` in
`server/benchd.py`). Lose that file and the whole history is gone. This runbook
covers the scheduled backup, the restore procedure, and a short restore drill.

The helper is `server/backup_benchd.py` (stdlib only). It uses sqlite3's
**online backup API**, so it is safe to run while `benchd` is live and writing:
the copy is a transactionally-consistent snapshot, not a byte copy of a file
mid-write. `benchd` runs in WAL mode, so a plain `cp` can capture a torn
database — always use the helper.

## Schedule

Back up on a timer and rotate. Both examples keep the 7 newest snapshots in
`/var/backups/benchd`.

**systemd timer** (preferred on the lab box):

```ini
# /etc/systemd/system/benchd-backup.service
[Unit]
Description=Back up the benchd SQLite database

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/bench-test/server/backup_benchd.py \
    --db /opt/bench-test/server/data/benchd.sqlite \
    --out /var/backups/benchd --keep 7
```

```ini
# /etc/systemd/system/benchd-backup.timer
[Unit]
Description=Hourly benchd backup

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl enable --now benchd-backup.timer
```

**cron** (any host):

```cron
0 * * * * /usr/bin/python3 /opt/bench-test/server/backup_benchd.py \
    --db /opt/bench-test/server/data/benchd.sqlite \
    --out /var/backups/benchd --keep 7 >/dev/null 2>&1
```

A manual backup, and the knobs, are:

```bash
python3 server/backup_benchd.py --help
python3 server/backup_benchd.py --db server/data/benchd.sqlite \
    --out /var/backups/benchd --keep 7
```

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `server/data/benchd.sqlite` (`$BENCHD_DB`) | source database |
| `--out` | `server/data/backups` (`$BENCHD_BACKUP_DIR`) | snapshot directory |
| `--keep` | `7` | keep the N newest snapshots; `0` keeps all |

Snapshots are named `benchd-YYYYMMDD-HHMMSS.sqlite`; rotation only ever removes
files that match that pattern, so an operator's unrelated file in `--out` is
left alone. The job fails closed (no file written) if the source database does
not exist.

## Restore

1. **Stop benchd** so nothing writes during the swap:

   ```bash
   sudo systemctl stop benchd
   ```

2. **Pick a snapshot** and verify it before trusting it:

   ```bash
   ls -1 /var/backups/benchd
   python3 - <<'PY'
   import sqlite3
   db = "/var/backups/benchd/benchd-20261013-030000.sqlite"
   con = sqlite3.connect(db)
   print(con.execute("PRAGMA integrity_check").fetchone()[0])
   print(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], "runs")
   PY
   ```

   `integrity_check` must print `ok`. Any other value: try the next snapshot.

3. **Replace the live database.** Remove the WAL sidecars too — a stale
   `-wal`/`-shm` next to a fresh main file will corrupt the result:

   ```bash
   DB=/opt/bench-test/server/data/benchd.sqlite
   cp "$DB" "$DB.broken-$(date +%Y%m%d-%H%M%S)"   # keep the bad one for post-mortem
   rm -f "$DB" "$DB-wal" "$DB-shm"
   cp /var/backups/benchd/benchd-20261013-030000.sqlite "$DB"
   chown benchd:benchd "$DB" 2>/dev/null || true
   ```

4. **Start benchd** and verify:

   ```bash
   sudo systemctl start benchd
   curl -s http://127.0.0.1:8722/health
   ```

   `/health` reports `ok: true` and a `capacity` block (row counts per table and
   the on-disk size). Confirm the `runs` count matches the snapshot, then open
   the dashboard and spot-check a recent run and its telemetry.

## Restore drill (do this once, then on a schedule)

The drill proves the backup is *usable*, not merely present. Run it against a
throwaway copy so the live instance is untouched:

```bash
# 1. take a fresh snapshot
python3 server/backup_benchd.py --db server/data/benchd.sqlite \
    --out /tmp/drill --keep 1

# 2. restore it to a scratch path and start benchd there on another port
SNAP=$(ls -1 /tmp/drill/benchd-*.sqlite | tail -1)
cp "$SNAP" /tmp/drill/restored.sqlite
python3 server/benchd.py --db /tmp/drill/restored.sqlite --port 8723 \
    --allow-anonymous &

# 3. verify the restored instance serves the history
curl -s http://127.0.0.1:8723/health
curl -s http://127.0.0.1:8723/api/v1/runs | head -c 400
kill %1
```

Expected: `/health` is `ok` with a non-zero `runs` count, and `/api/v1/runs`
lists the runs from the snapshot. Record the date, the snapshot name and the
result (`PASS`/`FAIL`) in the run log. A backup that has never been restored is
an assumption, not a recovery capability.
