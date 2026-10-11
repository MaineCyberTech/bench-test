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
| POST | `/register` | register/heartbeat identity → `runner_id` + per-runner `api_key` |
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
| GET | `/runs/<id>/telemetry?max=N` | downsampled series |
| GET | `/runs/<id>/telemetry.csv?token=` | full 1 Hz telemetry as CSV (download) |
| GET | `/runs/<id>/artifact/<name>?token=` | download an attached artifact |
| GET | `/trends?host=&metric=` | metric-over-time series |
| GET/POST/DELETE | `/schedules` · `/schedules/<id>` | list / create / remove recurring runs |
| POST | `/runners/<id>/commands` | issue `start`/`stop`/`update`/`fan_curve`/`power_limit`/`restart` |

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
```

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
- Runners are considered **offline** after 15 s without a heartbeat; their history
  is retained.
- `update` runs `git pull --ff-only` on the runner and reports the new commit;
  `restart` re-execs the agent so a fresh checkout takes effect.
- SQLite uses WAL; the DB grows ~1 row/second per active run (fine for a lab).

## Roadmap

- Operator logins (per-user accounts) instead of the shared join token.
- Server-side run queues with priorities and nightly soak jobs.
- Phase-log artifacts and richer artifact browsing from the UI.
- Prometheus/`/metrics` export and alerting (throttle, TDR, over-temp).
