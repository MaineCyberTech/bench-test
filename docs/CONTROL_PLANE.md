# Control plane (benchd) — single pane of glass

`benchd` is a dependency-free server that turns a set of bench-test machines into
one live, controllable fleet: every runner registers, streams its phases and 4 Hz
telemetry, and can be started / stopped / updated from a web dashboard.

```
              ┌───────────── runner machines ─────────────┐
   Windows ──▶ bench/agent.py ─┐
   Linux   ──▶ bench/agent.py ─┤   HTTP (bearer token)
   live USB ─▶ bench-tui ──────┤        │
              bench-gui ───────┘        ▼
                              ┌───────────────────────────────┐
                              │ benchd  (stdlib http + sqlite)│
                              │  /api/v1/…   REST + SSE       │
                              │  SQLite: runners/runs/phases/ │
                              │          telemetry/commands   │
                              └───────────────┬───────────────┘
                                              ▼
                              web dashboard (live · history · trends ·
                               compare · fleet · schedules · controls)
```

Nothing outside the Python standard library is required — no pip, no Node, no
build step — so the same code runs on the Proxmox lab box and on the live USB.

Operator runbook — start/stop, logs, `/health` vs `/ready`, failure triage, update
rollback and backup/restore: [`OPERATIONS.md`](OPERATIONS.md).

## Components

| Piece | Path | Role |
|---|---|---|
| Server | `server/benchd.py` | REST + SSE control plane, SQLite history |
| Web UI | `server/web/` | self-contained dashboard (vanilla JS/CSS) |
| Agent | `bench/agent.py` | headless runner: registers, runs the battery on command, streams |
| Reporter | `bench/benchreport.py` | shared client used by the agent **and** the GUI/TUI |
| Units | `server/*.service`, `bench-agent.cmd` | systemd / Windows launchers |

## Quick start

### 1. Server (Proxmox lab box)

```bash
# on the lab box (172.23.128.0/20), toolkit at /opt/bench-test
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # make a token
sudo install -m600 /dev/stdin /etc/benchd.conf <<'EOF'
BENCHD_TOKEN=<paste token>
EOF

python3 /opt/bench-test/server/benchd.py --host 0.0.0.0 --port 8722
# 0.0.0.0 is deliberate for the single-interface LXC - see Deployment / Security
# or install the service:
sudo cp server/benchd.service /etc/systemd/system/
sudo systemctl enable --now benchd
```

Open `http://<lab-box>:8722/?token=<token>` — the dashboard stores the token and
connects to the live stream.

### 2. Runners

Headless agent (recommended for always-on machines):

```bash
# Linux
BENCHD_SERVER=http://<lab-box>:8722 BENCHD_TOKEN=<token> \
  python3 bench/agent.py
```

```powershell
:: Windows
set BENCHD_SERVER=http://<lab-box>:8722
set BENCHD_TOKEN=<token>
bench-agent.cmd
```

GUI / TUI dashboards (interactive, still report + accept remote control):

```bash
python3 bench/gui.py --server http://<lab-box>:8722 --token <token> --name workstation-1
python3 bench/tui.py --server http://<lab-box>:8722 --token <token> --name live-usb
```

The live USB already starts `bench-tui`; add the flags via `bench-agent` or an
`EnvironmentFile` (`/etc/bench-agent.conf`).

## What the dashboard shows

- **Live** — every runner (online/busy/offline, GPU + driver, current phase) with
  start / stop / update / fan-curve controls, plus a live card per active run:
  temp/power/util/fan gauges, a rolling telemetry chart, and the phase table.
- **History** — every finished run with its headline metrics; click a row for the
  full phase breakdown, telemetry chart and captured log events.
- **Trends** — any metric over time for a host (or all hosts), one or two metrics
  at once, so a driver/GPU/cooling change is visible as a step.
- **Compare** — pick two runs and see per-metric deltas (%).
- **Schedules** — recurring runs (every N seconds, or daily at a wall-clock time)
  per runner; the server queues them automatically.
- **Downloads** — from a run's detail view: the telemetry CSV and any artifacts
  the runner attached (report JSON, manifests).

## API reference (prefix `/api/v1`, `Authorization: Bearer <token>`)

Runner-facing:

| Method | Path | Purpose |
|---|---|---|
| POST | `/register` | register a runner (adopts an existing row by `machine_id`+`kind`+`instance`) → `runner_id` + per-runner `api_key` |
| POST | `/runners/<id>/heartbeat` | liveness + status + capabilities |
| GET | `/runners/<id>/commands` | poll queued commands (drains the queue) |
| POST | `/runners/<id>/commands/<cid>/ack` | acknowledge a command |
| POST | `/runs` | open a run |
| POST | `/runs/<id>/phase` | open/close a phase (seq-keyed) |
| POST | `/runs/<id>/telemetry` | batched 4 Hz samples |
| POST | `/runs/<id>/event` | log/event line |
| POST | `/runs/<id>/artifact` | attach a report/manifest (base64) |
| POST | `/runs/<id>/finish` | close a run + attach the summary row |

UI-facing:

| Method | Path | Purpose |
|---|---|---|
| GET | `/state` | snapshot: runners, active runs (+latest telemetry, phases), recent, `hosts`, `server_git` |
| GET | `/stream?token=` | SSE: `snapshot`/`runner`/`run`/`phase`/`telemetry` (coalesced)/`command`/`log`/`schedule`/`event` |
| GET | `/runners` · `/runs` · `/runs/<id>` | fleet / history / detail |
| GET | `/runs/<id>/telemetry?max=N` | downsampled series (named fields) |
| GET | `/runs/<id>/logs?after=&limit=` | the run's live console lines |
| GET | `/runs/<id>/stats` | per-field n / min / avg / max / std / p50 / p95 / p99 (+ derived: energy, clock residency, throttled %, headroom) |
| GET | `/runs/<id>/telemetry.csv?token=` | full telemetry as CSV (download) |
| GET | `/runs/<id>/artifact/<name>?token=` | download an attached artifact (binary-safe) |
| GET | `/trends?host=&metric=` | metric-over-time series + per-series median/p95/p99 |
| GET | `/health` | public liveness + the control plane's commit (`git`, `latest`) |
| POST | `/server/update` | pull the control plane's own checkout + restart it (operator only; git deploy) |
| GET | `/metrics` | the metric catalogue (key → label) |
| GET/POST/DELETE | `/schedules` · `/schedules/<id>` | list / create / remove recurring runs (operator only) |
| DELETE | `/runs/<id>` · `/runners/<id>` | delete a run (+data) / a runner (+its commands & schedules) |
| POST | `/runners/<id>/commands` | issue `start`/`stop`/`update`/`fan_curve`/`power_limit`/`restart`/`shutdown`/`reboot`/`exec`/`shell`/`update_toolkit`/`iso_fetch`/`iso_write`/`iso_write_slot` (operator only) |

Runner-scoped routes (`/register`, heartbeat, `/runners/<id>/commands`, command
`ack`, and run writes) require a **per-runner API key** and are scoped to the
caller's own runner/run; the shared operator token may act on anything.

### Live stream (SSE)

`GET /stream?token=` is the dashboard's live path (an `EventSource`):

- **Cadence.** `telemetry` events are coalesced: a run's samples accumulate on
  the server and one event per run per tick (0.5 s, so at most 2/s) carries
  every sample since the previous event, instead of one event per posted batch.
  A `runner` event carries the full record only when something besides liveness
  changed; otherwise it is a slim update (`id`, `last_seen`, `online`), so the
  fleet view still moves without re-sending capabilities on every 2 s heartbeat.
- **Slow consumers are closed, not starved.** Each stream has a bounded queue
  (1000 events) and there is a cap of 64 concurrent streams (further connects
  get a 503 and retry on their own). A subscriber that still falls behind has
  its stream closed; the browser reconnects automatically and refetches each
  active run's telemetry, so a stalled dashboard recovers instead of silently
  missing samples.
- **Commit comparison is cached.** `update_available`/`toolkit_status` come from
  a cache refreshed in the background (4 s cap per GitHub call), so `/state`,
  `/runners` and heartbeats never block on GitHub being slow or down.

**Who handles which command:** the headless agent implements every kind; the
GUI/TUI dashboards implement `start`/`stop`/`fan_curve`/`shutdown`/`reboot`, the
update family (`update`/`update_toolkit`/`iso_*`), `restart` and the remote
terminal (`exec`/`shell`); `power_limit` is agent-only (the dashboards ack it as
unsupported rather than pretending).

Telemetry sample order (9 floats): `elapsed, temp_c, fan_pct, power_w, gpu_util,
gpu_fan_rpm, cpu_util, ram_used, sm_mhz`. A sample may instead be a **named dict**
(preferred), which is how the richer fields — `vram_used_mib`, `vram_total_mib`,
`mem_util`, clocks, throttle, disk/net, … — are carried; unknown numeric keys are
stored in the `extra` column and merged back on read.

### Command payloads

```jsonc
{ "kind": "start", "payload": {
    "label": "Quick check (short soaks)", "preset": "Quick check (short soaks)",  // history label
    "skip": ["llm", "fan"],                    // groups left off
    "groups": { "gpu": 1, "cpu": 1, "ram": 1, "disk": 1, "combined": 1, "fullsoak": 1,
                "net": 1, "nvenc": 1, "llm": 0, "fan": 0, "sys": 1, "gfx": 1 },
    "gpu_soak": 60, "combined": 60, "fullsoak": 60, "gfx": 30,   // the four duration boxes
    // the tune the four boxes do not cover - a preset sends these too:
    "cpu_soak": 60, "ram_soak": 30, "disk_seconds": 15, "disk_size_mb": 256,
    "gpu_size": 4096, "cpu_size": 2048, "ram_size_mb": 512, "passes": 1,
    "steady_gb": 2, "stable": 0, "stable_hold": 60, "gfx_engine": "furmark" } }
{ "kind": "stop" }
{ "kind": "update" }                      // git pull --ff-only on a checkout; toolkit/app self-update otherwise
{ "kind": "fan_curve", "payload": { "on": true } }
{ "kind": "power_limit", "payload": { "watts": 250 } }
{ "kind": "restart" }                       // re-exec the agent (fresh checkout)
{ "kind": "reboot" }                        // power-cycle the machine
{ "kind": "shutdown" }                      // power off the machine
{ "kind": "update_toolkit", "payload": { "ref": "main", "sha256": null } }   // live self-update
{ "kind": "iso_fetch", "payload": { "tag": "iso-nvidia-legacy-38", "gpu": "nvidia-legacy" } }
{ "kind": "iso_write", "payload": { "iso": "/mnt/benchdata/iso/bench-live-*.iso",
                                    "device": "/dev/sdX", "confirm": true } }  // destructive
{ "kind": "iso_write_slot", "payload": { "iso": "/var/tmp/bench-iso-stage/iso/bench-live-*.iso",
                                         "confirm": true } }  // A/B: inactive slot + boot flag
{ "kind": "exec", "payload": { "cmd": "nvidia-smi -q | head", "timeout": 30 } }  // remote terminal
```

**A `preset` name is a label, not a lookup.** The runner applies the payload's
group selection (`groups` for the dashboards, `skip` for the agent), the four
durations and the tune keys — nothing else. Send a preset's name without its tune
and the run falls back to the runner's own values (the dashboards' current boxes;
otherwise cpu 120 s, ram 120 s, gpu matrix 12288, 10 GiB steady write, disk
2048 MB) — which is how a "Quick check" ran 600 s soaks. The web start dialog
builds the full shape above.

## Deployment (Proxmox)

Run `benchd` as a small unprivileged **LXC container** so it shows in the Proxmox UI:

```bash
# on the Proxmox node (root)
pct create 108 local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst \
  --hostname benchd --cores 1 --memory 1024 --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.222.188/24,gw=192.168.222.1 \
  --nameserver 192.168.222.1 --unprivileged 1 --onboot 1 --start 1 --password '...'
pct exec 108 -- mkdir -p /opt/bench-test/server/web
pct push 108 benchd.py /opt/bench-test/server/benchd.py          # + web/ + unit
pct push 108 benchd.conf /etc/benchd.conf && pct exec 108 -- chmod 600 /etc/benchd.conf
pct exec 108 -- cp /opt/bench-test/server/benchd.service /etc/systemd/system/
pct exec 108 -- systemctl enable --now benchd
# mask the unprivileged-LXC mount units so systemd is not 'degraded'
pct exec 108 -- systemctl mask tmp.mount run-lock.mount dev-mqueue.mount
```

`onboot=1` starts it with the node. Live-USB and workstation runners reach it over the LAN/overlay.

### Service hardening and the `User=` migration

`server/benchd.service` runs with `NoNewPrivileges`, `PrivateTmp`,
`ProtectSystem=full`, `ProtectHome`, `RestrictSUIDSGID`, `ProtectKernelTunables`,
`ProtectKernelModules`, `ProtectControlGroups`, `LockPersonality` and
`RestrictRealtime`, and scopes `ReadWritePaths` to the data dir
(`server/data/`). It deliberately still runs as **root**: the lab deployment
keeps its state under `/opt/bench-test` (the git checkout used by
`POST /server/update` and the default SQLite DB), and `ProtectSystem=full`
leaves `/opt` writable so both keep working.

To move it to a dedicated account on a fresh deployment:

1. `useradd --system --home /opt/bench-test --shell /usr/sbin/nologin benchd`
2. `chown -R benchd: /opt/bench-test` (or move the DB out: run with
   `--db /var/lib/benchd/benchd.sqlite`, `install -d -o benchd /var/lib/benchd`)
3. Add `User=benchd` / `Group=benchd` to the unit. Keep the checkout writable if
   you want the self-update; to give the service *only* its data dir, switch
   `ProtectSystem=strict` with `ReadWritePaths=/var/lib/benchd` and deploy by
   copy instead of `git pull`.
4. `systemctl daemon-reload && systemctl restart benchd`, then check
   `journalctl -u benchd` for startup errors.

Numeric env values (`BENCHD_RETENTION_DAYS`, `BENCHD_AUTO_UPDATE_HOURS`) are
parsed defensively: a typo logs a warning and falls back to the default instead
of crash-looping the unit.

`--host 0.0.0.0` in the unit is deliberate: the unprivileged container has a single
interface (the lab bridge `vmbr0`), so `0.0.0.0` is the same as binding its lab
address (`192.168.222.188`) and the runners on the LAN/overlay must be able to
connect. The token gate (operator token or per-runner key) is the real control; a
firewall rule on the node or `--host <lab-ip>` narrows it further — see
[Security](#security).

## Data captured per run

- **Summary** (runs-index-style): GPU matmul (fp16/bf16/tf32/fp32), soak TFLOPS + telemetry,
  CPU matmul/soak, RAM, disk seq write, network, full-system verdict — the same keys on
  Windows and Linux so trends/compare line up.
- **Telemetry** (4 Hz rows; nvidia-smi cached at 1 Hz, sensors at 1 Hz): GPU util/mem-util/temp(+mem temp)/fan/power(+limit)/SM+mem+gr clocks/
  VRAM/pstate/enc+dec, CPU util + package/core temp, RAM + swap, disk read/write, net rx/tx.
- **Full inventory** per run: CPU, board/BIOS, DIMMs, GPUs + driver, storage, NICs
  (Windows `sysinfo` / Linux `sys` phase) — attached to the run as `system.inventory`.
- **Live console**: the runner's stdout streams to the server (`/logs`) and shows in the
  dashboard; the terminal box sends `exec` and streams output back.
- **Preset** used, plus host, GPU, driver, torch, git and start/end times.

## Security

- Two auth tiers: **runners** use a per-runner API key issued at registration
  (stored in `~/.config/bench-agent.json`); **operators/UI** use the shared join
  token (`BENCHD_TOKEN`) as a bearer, or `?token=` for the SSE stream and file
  downloads (EventSource / link navigation can't set headers).
- **Network exposure.** The shipped unit and the deployment snippet bind
  `0.0.0.0`, because the unprivileged LXC has a single lab-bridge interface — so
  that is the same as binding its lab address, and the runners on the LAN/overlay
  have to reach it. The container lives on the isolated lab network
  (`172.23.128.0/20`, reached over the `wgaudit0` WireGuard overlay — keep `wg0`
  untouched) and every request still has to pass the token gate (operator token
  or per-runner key). To narrow it, pass `--host <lab-ip>` or add a firewall rule
  on the Proxmox node. Put a TLS reverse proxy in front if it is ever exposed
  beyond the lab.
- `server/benchd.conf`, `server/data/` and `~/.config/bench-agent.json` are
  git-ignored — never commit the token, keys or the database.

## Operating notes

- The agent **fails soft**: if the control plane is down, the run proceeds and the
  dashboard keeps working locally; reporting resumes when the server is back.
- The **live USB** bakes `/etc/bench-agent.conf` (via the `BENCH_AGENT_CONF`
  secret) and its launchers mount BENCHDATA on demand; the dashboards retry
  registration, so the stick checks in as soon as the network is up. The ISO
  rebuilds automatically on push to `main` touching `bench/**` or `live/**`.
- Runners are considered **offline** after 15 s without a heartbeat; their history
  is retained.
- **Staleness:** a runner reports its toolkit identity (`capabilities.toolkit`:
  `mode` git|copy, `ref`, `commit`, `sha256`) and the server compares its commit
  with the control plane's own (`/health` → `git`, `/state` → `server_git`) —
  surfaced as `update_available` on each runner (Live badge, Fleet "toolkit"
  column, a "stale" topstat). A server deployed by copying files (no `.git`) reads
  a `.deployed-commit` / `.bench-version` stamp instead, so write one in the
  deploy (or `git clone` the server and it works for free).
- **Runner identity is stable per machine + role + instance.** Each runner sends
  a `machine_id` (Windows `MachineGuid`, Linux DMI uuid / `/etc/machine-id`, else
  a random id persisted next to the agent config; `$BENCH_MACHINE_ID` overrides)
  plus an `instance` label (`$BENCH_AGENT_INSTANCE`, `--instance`), and the
  server **adopts the row that machine already owns for its `kind` + `instance`**
  instead of minting a new one — so a re-registration, an OS reinstall or an
  **ephemeral live-USB boot** reuses one row. The fleet view shows
  `name (instance) #uid`. The live USB runs a TUI on tty1 *and* ttyS0 (the
  template unit sets `BENCH_AGENT_INSTANCE=%i`), so they appear as
  `bench-live (tty1)` / `bench-live (ttyS0)` rather than two identical rows; the
  ttyS0 unit only starts with a `console=` on the cmdline and a serial
  device present (`console=ttyS0,115200` counts). One-shot
  `--self-test` runners are flagged *ephemeral* and dropped once they stop
  heartbeating.
- `update` runs `git pull --ff-only` on a checkout and reports the new commit; on
  a live stick or app bundle it refreshes the toolkit copy in place instead.
  `restart` re-execs the agent so a fresh checkout takes effect. The GUI, TUI and
  agent repeat that check **at startup** and restart into the new code when they
  pulled something (a dirty tree or `BENCH_NO_UPDATE=1` skips it). A control-plane
  update also restarts the runner (deferred while a run is in progress, or forced
  with `payload.restart`), so it stops reporting the commit it started with.
- **The control plane keeps itself current too.** When its commit differs from
  `latest` (GitHub main), the web top bar shows an **update server** alert/button:
  it confirms, POSTs `/api/v1/server/update`, and pulls its own checkout
  (`git fetch` + `git pull --ff-only`), restarting `benchd` through systemd when
  the commit changed. This is git-deploy only — a copy deploy gets a "refresh the
  files and restart benchd" message instead. The scheduler can do the same
  unattended: it re-checks every `BENCHD_AUTO_UPDATE_HOURS` (default 1) and skips
  while any run is active (so the restart never costs a live run's view);
  `BENCHD_AUTO_UPDATE=0` disables the automatic path (a copy deploy does nothing).
- **The telemetry pipeline reports itself.** The dashboards' reporter pump keeps a
  bounded queue (1200 samples ≈ five minutes at 4 Hz); when the server is
  unreachable it drops the *oldest* sample and counts it, and every ~20 s writes
  `[report] telemetry N produced, N sent, N dropped` to the run's console — lost
  live telemetry is visible instead of silent.
- A long job is never mistaken for a dropped one: the stale-command reaper gives
  `iso_fetch` a 3 h budget, `iso_write`/`iso_write_slot` an hour,
  `update_toolkit` 15 min and `exec` its own payload timeout, against 120 s for
  start/stop and friends.
- SQLite uses WAL; a run at 4 Hz writes ~4.5 MB per 15 minutes. Once an hour the
  scheduler sweeps the per-sample tables (**telemetry/logs/events only**) of
  **finished** runs older than `BENCHD_RETENTION_DAYS` (default 30 days, `0` keeps
  everything), materialising each run's stats into the run row first. It never
  touches a running run; the run rows, phases, artifacts, commands and summaries
  are kept, because that is what History, Trends, Compare and Fleet read. See
  [`DATA.md`](DATA.md#5-retention) for the runner-side knobs too.
- **Backfill** (`server/import_results.py`): each unit gets a deterministic run
  id, so re-running over the same results directory is a no-op (already-imported
  units are skipped); `--reset` pages through and deletes *all* existing runs
  first. Prefer `$BENCHD_TOKEN` or `--token-file PATH` over `--token`, which is
  visible in the process list.

## Tests

Run it from the repo venv — the TUI tests import `curses` (needs
`windows-curses` on Windows), a TUI check needs `psutil`, and a RAM-integrity
check only runs when `numpy` is importable:

```bash
.venv/bin/python tests/run_all.py             # Linux / live USB
.venv\Scripts\python.exe tests\run_all.py     # Windows — RESULT: PASS expected
```

The suite itself adds no dependencies; each module starts an in-process `benchd`
on an ephemeral port. It covers the API (auth, per-runner keys,
runs/phases/telemetry/logs/artifacts/stats, commands, schedules, delete, reaper,
SSE), the reporting client (Reporter, PhaseTracker, ReporterPump, exec,
inventory/summary builders), preset parity across GUI/TUI/web, and the dashboard
phase-table logic.

## Measured phase durations

See [`PHASE_TIMINGS.md`](PHASE_TIMINGS.md) for per-phase durations measured on a
reference workstation (used to estimate how long each preset takes).

## Roadmap

- Operator logins (per-user accounts) instead of the shared join token.
- Server-side run queues with priorities and nightly soak jobs.
- Phase-log artifacts and richer artifact browsing from the UI.
- Alerting on the control plane's `/metrics` series (throttle, TDR, over-temp);
  the Prometheus exposition itself ships — see [`OBSERVABILITY.md`](OBSERVABILITY.md).
