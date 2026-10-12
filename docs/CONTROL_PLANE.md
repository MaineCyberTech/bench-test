# Control plane (benchd) — single pane of glass

`benchd` is a dependency-free server that turns a set of bench-test machines into
one live, controllable fleet: every runner registers, streams its phases and 1 Hz
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
                              web dashboard (live · history ·
                               trends · compare · controls)
```

Nothing outside the Python standard library is required — no pip, no Node, no
build step — so the same code runs on the Proxmox lab box and on the live USB.

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
| POST | `/register` | register a runner (adopts an existing row by `machine_id`+`kind`) → `runner_id` + per-runner `api_key` |
| POST | `/runners/<id>/heartbeat` | liveness + status + capabilities |
| GET | `/runners/<id>/commands` | poll queued commands (drains the queue) |
| POST | `/runners/<id>/commands/<cid>/ack` | acknowledge a command |
| POST | `/runs` | open a run |
| POST | `/runs/<id>/phase` | open/close a phase (seq-keyed) |
| POST | `/runs/<id>/telemetry` | batched 1 Hz samples |
| POST | `/runs/<id>/event` | log/event line |
| POST | `/runs/<id>/artifact` | attach a report/manifest (base64) |
| POST | `/runs/<id>/finish` | close a run + attach the summary row |

UI-facing:

| Method | Path | Purpose |
|---|---|---|
| GET | `/state` | snapshot: runners, active runs (+latest telemetry, phases), recent |
| GET | `/stream?token=` | SSE: `snapshot`/`runner`/`run`/`phase`/`telemetry`/`command` |
| GET | `/runners` · `/runs` · `/runs/<id>` | fleet / history / detail |
| GET | `/runs/<id>/telemetry?max=N` | downsampled series (named fields) |
| GET | `/runs/<id>/logs?after=&limit=` | the run's live console lines |
| GET | `/runs/<id>/stats` | per-field min / avg / max over the run |
| GET | `/runs/<id>/telemetry.csv?token=` | full 1 Hz telemetry as CSV (download) |
| GET | `/runs/<id>/artifact/<name>?token=` | download an attached artifact |
| GET | `/trends?host=&metric=` | metric-over-time series |
| GET/POST/DELETE | `/schedules` · `/schedules/<id>` | list / create / remove recurring runs |
| DELETE | `/runs/<id>` | delete a run and its data |
| POST | `/runners/<id>/commands` | issue `start`/`stop`/`update`/`fan_curve`/`power_limit`/`restart`/`exec` |

Telemetry sample order (10 floats): `elapsed, temp_c, fan_pct, power_w, gpu_util,
gpu_fan_rpm, cpu_util, ram_used, sm_mhz, mem_used`.

### Command payloads

```jsonc
{ "kind": "start", "payload": { "label": "Quick check", "skip": ["gfx"],
    "gpu_soak": 60, "combined": 60, "fullsoak": 60, "gfx": 30 } }
{ "kind": "stop" }
{ "kind": "update" }                      // git pull --ff-only on the runner
{ "kind": "fan_curve", "payload": { "on": true } }
{ "kind": "power_limit", "payload": { "watts": 250 } }
{ "kind": "restart" }
{ "kind": "exec", "payload": { "cmd": "nvidia-smi -q | head", "timeout": 30 } }  // remote terminal
```

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

## Data captured per run

- **Summary** (runs-index-style): GPU matmul (fp16/bf16/tf32/fp32), soak TFLOPS + telemetry,
  CPU matmul/soak, RAM, disk seq write, network, full-system verdict — the same keys on
  Windows and Linux so trends/compare line up.
- **Telemetry** (1 Hz): GPU util/mem-util/temp(+mem temp)/fan/power(+limit)/SM+mem+gr clocks/
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
- Bind to the LAN/lab address only. On the lab overlay it is reachable over the
  `wgaudit0` WireGuard network — keep `wg0` untouched.
- Put a TLS reverse proxy in front if it is ever exposed beyond the lab.
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
  ttyS0 unit only starts with `console=ttyS0` on the cmdline. One-shot
  `--self-test` runners are flagged *ephemeral* and dropped once they stop
  heartbeating.
- `update` runs `git pull --ff-only` on the runner and reports the new commit;
  `restart` re-execs the agent so a fresh checkout takes effect.
- SQLite uses WAL; the DB grows ~1 row/second per active run (fine for a lab).

## Tests

```bash
python tests/run_all.py          # RESULT: PASS expected
```

Stdlib only; each module starts an in-process `benchd` on an ephemeral port. It
covers the API (auth, per-runner keys, runs/phases/telemetry/logs/artifacts/stats,
commands, schedules, delete, reaper, SSE), the reporting client (Reporter,
PhaseTracker, ReporterPump, exec, inventory/summary builders), preset parity
across GUI/TUI/web, and the dashboard phase-table logic.

## Measured phase durations

See [`PHASE_TIMINGS.md`](PHASE_TIMINGS.md) for per-phase durations measured on a
reference workstation (used to estimate how long each preset takes).

## Roadmap

- Operator logins (per-user accounts) instead of the shared join token.
- Server-side run queues with priorities and nightly soak jobs.
- Phase-log artifacts and richer artifact browsing from the UI.
- Prometheus/`/metrics` export and alerting (throttle, TDR, over-temp).
