# Operating the control plane

Day-two runbook for `benchd`, the bench-test control plane: how to start and stop
it, where its logs go, how to tell "alive" from "ready", how to triage the common
failures, how to roll back a bad toolkit/app/ISO update, and how to back up and
restore the system of record.

Everything here is stdlib-only and matches the shipped artifacts — the systemd
unit `server/benchd.service` and the deployment in
[`CONTROL_PLANE.md`](CONTROL_PLANE.md) § Deployment. The detailed logging /
`/ready` / `/metrics` contract lives in
[`OBSERVABILITY.md`](OBSERVABILITY.md); the database backup procedure lives in
[`BACKUP_RESTORE.md`](BACKUP_RESTORE.md).

## Start and stop

**Under systemd (the lab box):**

```bash
sudo systemctl start benchd          # or: enable --now benchd
sudo systemctl status benchd
sudo systemctl stop benchd
sudo systemctl restart benchd        # after a config or unit change
```

The unit runs `/usr/bin/python3 /opt/bench-test/server/benchd.py --host 0.0.0.0
--port 8722` from `/opt/bench-test`, restarts on failure (`Restart=on-failure`,
`RestartSec=3`) and is lightly hardened (`NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=full`, `ProtectHome=true`).

**By hand (any host):**

```bash
python3 server/benchd.py --host 0.0.0.0 --port 8722
python3 server/benchd.py --host 127.0.0.1 --port 8722     # local-only
```

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | listen address (`0.0.0.0` in the unit — see [`CONTROL_PLANE.md`](CONTROL_PLANE.md) § Security) |
| `--port` | `8722` | listen port |
| `--db` | `server/data/benchd.sqlite` | SQLite database (`DEFAULT_DB` in `benchd.py`) |
| `--token` | — | join/operator bearer; also `$BENCHD_TOKEN` or a `benchd.conf` |
| `--web` | `server/web` | dashboard assets |
| `--quiet` | off | suppress the per-request access log line |

**The token** is resolved first-match-wins from `--token`, `$BENCHD_TOKEN`, then
`server/benchd.conf`, `~/.config/benchd.conf`, `/etc/benchd.conf`
(`BENCHD_TOKEN=…`, key=value). On the lab box it lives in `/etc/benchd.conf`
(mode `600`):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # make a token
sudo install -m600 /dev/stdin /etc/benchd.conf <<'EOF'
BENCHD_TOKEN=<paste token>
EOF
sudo systemctl restart benchd
```

Open `http://<host>:8722/?token=<token>`; the dashboard stores the token and
connects to the live SSE stream.

## Logs

`benchd` writes its banner, per-request access line and retention notices to
**stdout**, one line each, so under systemd:

```bash
journalctl -u benchd -f            # follow
journalctl -u benchd --since '1 hour ago'
```

What you will see:

- a startup banner — `[benchd] listening on http://HOST:PORT  db=…  auth=…`,
  plus a `WARNING: no BENCHD_TOKEN set` line when the token is missing;
- the per-request access line (suppressed by `--quiet`);
- retention sweeps — `[benchd] retention: removed N row(s) older than D days`.

The observability work turns every server-side line into a single JSON object on
stderr carrying a `request_id` (and an `X-Request-ID` on every response), so one
request can be reconstructed from the stream — see
[`OBSERVABILITY.md`](OBSERVABILITY.md) § Structured logging.

The **runners** keep their own logs: the live console in the dashboard (`/logs`)
mirrors the runner's stdout, and the GUI/TUI/agent write to their terminal (or
`journalctl -u bench-agent`).

## Health: `/health` vs `/ready`

Two public probes answer different questions, and they are deliberately
different in cost:

| | `GET /health` | `GET /ready` |
|---|---|---|
| Question | "is the process alive?" | "can this instance serve?" |
| I/O | none (cached snapshot) | SQLite read **and** write, plus the version compare |
| Status | always `200` | `200` when ready, `503` when not |
| Use | uptime probe (short timeout) | load-balancer / orchestrator readiness |

```bash
curl -s http://127.0.0.1:8722/health
curl -s http://127.0.0.1:8722/ready
```

`/health` also carries the control plane's commit (`git`) and `version`, which is
what the fleet's "update available" comparison reads. A `503` from `/ready` means
the database is not readable/writable — go to § Database locked / unavailable.

`GET /metrics` is the public Prometheus exposition (uptime, request counters,
auth failures, command-queue depth); `GET /api/v1/metrics` is the metric
*catalogue* (key → label) used by the web UI. Both are described in
[`OBSERVABILITY.md`](OBSERVABILITY.md).

## Common failures

### Token missing (everyone gets `401`)

Symptom: the dashboard/runner cannot connect; the API returns `401
unauthorized`; the log shows `auth=OPEN` or the no-token warning at startup.

- The operator token and the runner join token are the same shared secret today
  (`BENCHD_TOKEN`). Create `/etc/benchd.conf` (mode `600`) with `BENCHD_TOKEN=…`
  and `sudo systemctl restart benchd`.
- Runners get a **per-runner API key** at `/register`; if a runner was deleted
  server-side it must register again (`~/.config/bench-agent.json` is reissued).
- Never run a tokenless instance reachable beyond the lab. `--allow-anonymous`
  exists only for a throwaway/local instance; a token is the real gate (see
  [`CONTROL_PLANE.md`](CONTROL_PLANE.md) § Security).

### Database locked / unavailable

Symptom: `database is locked` in the log, `500`s on write endpoints, and
`/ready` returning `503`.

- SQLite runs in **WAL** mode and `benchd` serialises its own writes, so a lock
  usually means a **second writer**: a second `benchd` on the same file, an
  interactive `sqlite3` session, or a byte-copy backup (`cp`) racing the writer.
  Keep exactly one `benchd` per database file.
- For backups never `cp` a live WAL database — use the online-backup helper
  (§ Backup and restore), which takes a transactionally-consistent snapshot.
- WAL over a network filesystem (NFS/SMB) is unreliable; keep the database on
  local storage.
- Confirm the path `benchd` actually opened from the startup banner, and that
  the file exists and is writable by the service account.

### Runner offline

A runner is **offline** after `15 s` with no heartbeat (`ONLINE_WINDOW` in
`benchd.py`); its history is retained and the row is not deleted.

- Check the runner process/terminal (`bench-agent`, the GUI/TUI window) and its
  log. The agent **fails soft**: if the control plane is unreachable the run
  still proceeds locally and reporting resumes when the server is back.
- Verify the runner points at the right server and token — `BENCHD_SERVER` /
  `BENCHD_TOKEN`, `--server` / `--token`, or `bench-agent.conf`
  (`load_conf()` search order).
- Check the network path to the lab box (LAN, or the `wgaudit0` overlay).
- A closed/killed dashboard leaves its run `running`; the server-side reaper
  fails it once the runner stops heartbeating, so runs never get stuck.

### Schedule not firing

Schedules are stored per runner and fired by the control plane's in-process
scheduler loop, so **`benchd` must be up**. Each due schedule queues a `start`
command; the schedule row records why it did or did not:

| `last_status` | Meaning | Fix |
|---|---|---|
| `queued` | fired — a `start` command was queued | none |
| `skipped: runner offline` | the target runner is not heartbeating | bring the runner back (§ Runner offline) |
| `skipped: busy` | the runner is running, or a `start` is already queued | wait, or stop the current run |

Also check:

- **`next_run`** in the schedule (daily schedules fire at their wall-clock
  `at_time`; interval schedules wait `max(60, every_s)` seconds, so sub-minute
  intervals are rounded up to 60 s);
- the server clock/timezone, for `daily` schedules;
- that the schedule is `enabled` (a disabled row never fires).

## Rollback

Which path applies depends on how the runner is deployed. In every case, an
update that pulled/installed something new restarts the runner so the new code
is what actually runs; a run in progress defers the restart (force it with
`payload.restart`).

### `BENCH_NO_UPDATE=1` — stop the automatic update

The GUI/TUI/agent fast-forward their checkout **at startup** (`auto_update`).
Set `BENCH_NO_UPDATE=1` to opt out entirely (also skipped when the tree is dirty,
not a git checkout, or offline). For an app bundle the check is throttled by
`BENCH_APP_UPDATE_HOURS` (default 6; `0` checks every launch). Use this to hold a
machine on a known-good build while you investigate.

### A bad toolkit update

- **Git checkout:** `update` is `git pull --ff-only` on the runner. Roll back by
  checking out the last-good commit and restarting the runner:

  ```bash
  git -C <repo> checkout <last-good-commit>
  ```

  (Or set `BENCH_NO_UPDATE=1` and restart so the next launch does not re-pull.)

- **Live USB / copied install:** `update` / `update_toolkit` fetches the toolkit
  at `ref` onto the writable partition and re-points `/opt/bench-test` at it. Each
  fetched `ref` is kept in its own directory under `<BENCHDATA>/bench-toolkit/`,
  so the previous copy is still on disk. Roll back by re-pointing at it — fetch
  and switch to the last-good ref, then restart:

  ```bash
  python bench/live_update.py toolkit --ref <last-good-sha>
  python bench/live_update.py boot-link      # re-point at the persisted copy
  ```

  `boot-link` follows the `current` symlink, then the `current.txt` pointer (vfat
  cannot hold a symlink), then the freshest fetched copy — and it runs at boot,
  so a bad re-point self-corrects on the next boot to whatever `current` names.

- **App bundle:** `update` refreshes only the shipped `bench/` next to the exe;
  the frozen dashboard itself needs a new bundle. There is no in-place rollback
  of the frozen launcher — re-download the previous `app-<n>` bundle and replace
  the directory.

### A bad ISO (kernel/driver) update

On an **A/B stick**, `iso_write_slot` writes the staged ISO to the *inactive*
slot and flips the boot flag; the active slot and BENCHDATA are untouched. A bad
image **self-reverts**: grub counts attempts and, after three unconfirmed boots,
flips `active` back to the slot that last worked and resets the counter.

```bash
python bench/live_update.py iso-write-slot <iso> --yes   # stage + flip
# recovery is automatic after 3 failed boots; to force it, boot the good slot
# or flip `active` in the ESP's grubenv back to the previous slot.
```

A **single-slot** stick cannot overwrite the partition it booted from — reflash
it from another device (or `iso_write` to a different block device), as in
[`CONTROL_PLANE.md`](CONTROL_PLANE.md) § Deployment and the README's live-USB
section.

## Backup and restore

The entire system of record — runner registry, every run, its phases, telemetry,
logs, events, artifacts **and the per-runner API keys** — is one SQLite file
(`server/data/benchd.sqlite`). Lose it and the history and runner identities are
gone. **Back it up.**

The helper is [`server/backup_benchd.py`](../server/backup_benchd.py) (stdlib
only). It uses sqlite3's **online backup API**, so it is safe against a live,
WAL-mode `benchd` — never `cp` a live database.

```bash
python3 server/backup_benchd.py --db server/data/benchd.sqlite \
    --out /var/backups/benchd --keep 7
```

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `server/data/benchd.sqlite` (`$BENCHD_DB`) | source database |
| `--out` | `server/data/backups` (`$BENCHD_BACKUP_DIR`) | snapshot directory |
| `--keep` | `7` | keep the N newest snapshots; `0` keeps all |

Snapshots are `benchd-YYYYMMDD-HHMMSS.sqlite`; rotation only removes files
matching that pattern, and the job **fails closed** (writes nothing) if the
source database is missing.

**Full procedure, schedule (systemd timer / cron) and the restore steps:
[`BACKUP_RESTORE.md`](BACKUP_RESTORE.md).** In short, restore is: stop `benchd`,
verify the snapshot (`PRAGMA integrity_check` → `ok`), replace the database
(remove the stale `-wal`/`-shm` sidecars first), start `benchd`, and confirm
`/health` / `/ready` and the `runs` count.

## Disaster recovery, in one paragraph

`benchd` is a single node with local SQLite and an in-memory SSE bus
(`ARCH-P2-001`): the recovery capability **is** the backup plus a restore that
has been *exercised*. Keep the scheduled snapshot off-box, and run the restore
drill in [`BACKUP_RESTORE.md`](BACKUP_RESTORE.md) § Restore drill on a schedule —
against a throwaway copy and another port — recording the snapshot name and
`PASS`/`FAIL`. Also keep the operator token (`BENCHD_TOKEN`) recoverable
separately: it is not in the database, and per-runner keys *are*, so restoring a
snapshot restores runner access too. There is no encryption at rest today
(`DR-P2-004`) — protect the snapshot directory and the token store accordingly.
