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
BENCHD_TOKEN=<operator token>          # never bake into a release
BENCHD_JOIN_TOKEN=<join token>         # optional: registration-only, safe to bake
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

The machine-readable contract lives in [`openapi.json`](openapi.json) (OpenAPI
3.0): every route, its methods, its auth expectation and the command catalogue.
It is kept honest by `tests/test_contract.py`, which checks it against the
server's own route table and the live dispatcher.

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
| GET | `/state` | snapshot: runners, active runs (+latest telemetry, phases), recent, `hosts`, `command_kinds`, `server_git`, `version`, `version_source` |
| GET | `/stream?token=` | SSE: `snapshot`/`runner`/`run`/`phase`/`telemetry` (coalesced)/`command`/`log`/`schedule`/`event`/`artifact` |
| GET | `/runners` · `/runs` · `/runs/<id>` | fleet / history / detail |
| GET | `/runs/<id>/telemetry?max=N` | downsampled series (named fields) |
| GET | `/runs/<id>/logs?after=&limit=` | the run's live console lines |
| GET | `/runs/<id>/stats` | per-field n / min / avg / max / std / p50 / p95 / p99 (+ derived: energy, clock residency, throttled %, headroom) |
| GET | `/runs/<id>/telemetry.csv?token=` | full telemetry as CSV (download) |
| GET | `/runs/<id>/artifact/<name>?token=` | download an attached artifact (binary-safe) |
| DELETE | `/runs/<id>/artifact/<name>` | remove one attached artifact (its run's owner or the operator; audited) |
| GET | `/trends?host=&metric=` | metric-over-time series + per-series median/p95/p99 |
| GET | `/commands` | the command catalogue: every accepted kind with `destructive`/`needs_confirm`/`description` (operator only) |
| GET | `/health` | public liveness + the control plane's `version`, commit (`git`) and `version_source` |
| POST | `/server/update` | pull the control plane's own checkout + restart it (operator only; git deploy) |
| GET | `/metrics` | the metric catalogue (key → label) |
| GET | `/audit?limit=N` | audit trail (newest first): `ts`, `actor`, `action`, `target`, `detail` (operator only) |
| GET/POST/DELETE | `/schedules` · `/schedules/<id>` | list / create / remove recurring runs (operator only) |
| DELETE | `/runs/<id>` · `/runners/<id>` | delete a run (+data) / a runner (+its runs and their data, commands & schedules) |
| POST | `/runners/<id>/commands` | issue `start`/`stop`/`update`/`fan_curve`/`power_limit`/`restart`/`shutdown`/`reboot`/`exec`/`shell`/`update_toolkit`/`iso_fetch`/`iso_write`/`iso_write_slot` (operator only; the four destructive kinds need `payload.confirm=true`) |
| POST | `/runners/<id>/rotate` | mint a new `api_key` for that runner (its owner or the operator); the old key stops working at once, and the rotation is audited |

Runner-scoped routes (`/register`, heartbeat, `/runners/<id>/commands`, command
`ack`, and run writes) require a **per-runner API key** and are scoped to the
caller's own runner/run; the shared operator token may act on anything. The
**join token** (`BENCHD_JOIN_TOKEN`) is accepted by `/register` alone — it can
mint a runner and nothing else.

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
{ "kind": "reboot", "payload": { "confirm": true } }     // power-cycle the machine
{ "kind": "shutdown", "payload": { "confirm": true } }   // power off the machine
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

The **command catalogue is a registry**, not a hardcoded list: each kind carries
`destructive`, `needs_confirm` and a `description`, and the server derives both the
accepted set and the confirm gate from it. A client can read it (read-only) at
`GET /api/v1/commands`, or as `command_kinds` in `GET /api/v1/state`, so it never
has to guess which kinds a server accepts. Today `shutdown`, `reboot`, `iso_write`
and `iso_write_slot` are the **destructive** kinds: they are refused with `400`
unless the payload carries an explicit `confirm: true` (the browser ticks its
confirm box; a script must echo the flag). The API no longer relies on the
browser's prompt alone.

### Operator controls (all optional, off by default)

| Flag / config key | Effect |
|---|---|
| `--exec-allowlist REGEX` / `BENCHD_EXEC_ALLOWLIST` | when set, only `exec`/`shell` commands that match are accepted (403 otherwise) |
| `--exec-denylist REGEX` / `BENCHD_EXEC_DENYLIST` | any `exec`/`shell` command that matches is always refused (403) |
| `--key-ttl-days N` / `BENCHD_KEY_TTL_DAYS` | a runner `api_key` older than N days is rejected with `401` on heartbeat / command poll (`0`, the default, never expires) |
| `--artifact-max-bytes N` / `BENCHD_ARTIFACT_MAX_BYTES` | per-artifact size cap in bytes (default 25 MiB) |
| `--artifact-quota N` / `BENCHD_ARTIFACT_QUOTA` | maximum artifacts per run (default 200) |

A denied `exec`/`shell` command and every key rotation append an `audit` row
(`command_denied` / `key_rotate`); so does an unconfirmed destructive command.
An invalid allow/deny regex refuses to start (fail closed).

## Deployment (Proxmox)

Run `benchd` as a small unprivileged **LXC container** so it shows in the Proxmox UI:

```bash
# on the Proxmox node (root)
pct create <CTID> local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst \
  --hostname benchd --cores 1 --memory 1024 --rootfs local-lvm:4 \
  --net0 name=eth0,bridge=vmbr0,ip=<benchd-ip>/24,gw=<gateway> \
  --nameserver <gateway> --unprivileged 1 --onboot 1 --start 1 --password '...'
pct exec <CTID> -- mkdir -p /opt/bench-test/server/web
pct push <CTID> benchd.py /opt/bench-test/server/benchd.py          # + web/ + unit
pct push <CTID> benchd.conf /etc/benchd.conf && pct exec <CTID> -- chmod 600 /etc/benchd.conf
pct exec <CTID> -- cp /opt/bench-test/server/benchd.service /etc/systemd/system/
pct exec <CTID> -- systemctl enable --now benchd
# mask the unprivileged-LXC mount units so systemd is not 'degraded'
pct exec <CTID> -- systemctl mask tmp.mount run-lock.mount dev-mqueue.mount
```

**Substitute your lab values.** `<CTID>` (the LXC container id), `<benchd-ip>` and
`<gateway>`, and the storage/bridge names (`local-lvm`, `vmbr0`) are placeholders —
set them to match your own Proxmox node; nothing here is tied to a specific host.

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

A copy-deploy has no `.git`, so `/health` and `/state` resolve the control plane's
**commit** and **release version** from files shipped with the code: the repo-root
`VERSION`, and/or a `.bench-version` stamp (JSON `{"version": ..., "commit": ...}`).
Push them alongside `server/` (or `git clone` the server) so `/health` reports the
real version and the commit that a release tag maps to — otherwise the version
reads `0.0.0+unknown`. The startup log line records which source was used, and
`/health`/`/state` expose it as `version_source`.

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

- **Two tokens, two roles.** `BENCHD_TOKEN` is the **operator** token: as a bearer
  (API/CLI) or `?token=` for the SSE stream and file downloads (EventSource / link
  navigation can't set headers), it authorizes everything — state, runs, commands,
  schedules, delete, `exec`/`shell`, SSE. `BENCHD_JOIN_TOKEN` is the
  **registration-only join token**: it is accepted by `POST /api/v1/register` and
  nowhere else, so it registers a runner but can never satisfy an operator check or
  act as a runner api_key. Runners then use the per-runner API key issued at
  registration (stored in `~/.config/bench-agent.json`).
- **Never bake the operator token into a public artifact.** The repo is public and
  ISO/app releases are public downloads, so anything baked in is world-readable.
  Bake the **join token** (`BENCHD_JOIN_TOKEN`) into public releases for one-click
  onboarding; keep `BENCHD_TOKEN` operator-only and rotate it if it ever leaks.
- Bind to the LAN/lab address only. On the lab overlay it is reachable over the
  `wgaudit0` WireGuard network — keep `wg0` untouched.
- **CORS is same-origin by default.** No wildcard `Access-Control-Allow-Origin` is
  sent; the header is echoed only when the request `Origin` is the request's own
  host, or exactly matches `--cors-origin` / `$BENCHD_CORS_ORIGIN` (one dashboard
  origin). OPTIONS preflight still works.
- **Abuse limits (in-process, per client IP).** Failed auth attempts and
  `/api/v1/register` calls are each token-bucketed at 10/minute; over the limit the
  server answers `429` with `Retry-After`. Successful operator requests are never
  limited. Counters are in memory and reset on restart.
- **Audit trail.** Privileged actions append a row to the `audit` table (readable
  at `GET /api/v1/audit`, operator only, newest first): runner registration,
  command issuance (with the kind), a denied `exec`/`shell` (`command_denied`), a
  key rotation (`key_rotate`), run/runner/schedule deletion, and auth
  failures (`actor=anonymous`). Actors are `operator`, `join`, `runner:<id>` or
  `anonymous`; the shared operator token is one identity, so per-user attribution
  still needs per-user accounts (see Roadmap).
- A write request whose body is not a JSON object is rejected with
  `400 {"error":"invalid json"}` instead of being treated as `{}`. Any other
  malformed client input (a non-list `samples`, a non-string `kind`, an object
  where a string is expected, ...) is likewise a generic `400 {"error":"invalid
  request"}` — never a `500`, and never the internal exception type or message.
  A request that declares a body it never sends is cut off by a per-connection
  socket timeout rather than pinning a handler thread.
- An attached artifact's `sha256` is **recomputed from the bytes received** and
  verified on upload; a mismatch is a `400` and the digest the server stores (and
  serves) is its own recomputation, not the client's claim. An upload is also
  bounded by a per-artifact size cap (default 25 MiB) and a per-run quota (default
  200), both `413` when exceeded, and its name must be a bare filename — an empty,
  absolute or path-traversal (`../`) name is a `400`.
- **Destructive commands are confirmed server-side.** `shutdown`, `reboot`,
  `iso_write` and `iso_write_slot` require `payload.confirm=true`; without it the
  API answers `400` (the browser's confirm dialog is unchanged, it just now also
  sets the flag). `exec`/`shell` can be constrained with `--exec-allowlist` /
  `--exec-denylist` (both unset = allow all, as before); a denied command is `403`
  and audited.
- **Runner keys can be rotated and expire.** `POST /runners/<id>/rotate` (owner or
  operator) mints a new key and invalidates the old one immediately; only the
  digest is stored. With `--key-ttl-days N`, a key older than N days is rejected
  with `401` on heartbeat / command poll until it is rotated.
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
- **Staleness:** a runner reports its toolkit identity (`capabilities.toolkit`:
  `mode` git|copy, `ref`, `commit`, `sha256`) and the server compares its commit
  with the control plane's own (`/health` → `git`, `/state` → `server_git`) —
  surfaced as `update_available` on each runner (Live badge, Fleet "toolkit"
  column, a "stale" topstat). A server deployed by copying files (no `.git`) has
  no git at all, so both its **commit** and its **release version** resolve from a
  stamp shipped with the deploy: `.deployed-commit` (a plain commit) or
  `.bench-version` (JSON: `{"version": ..., "commit": ...}`), the same stamp the
  app bundle bakes. `/health` and `/state` report `version`, `git` and the
  `version_source` (`VERSION` / `.bench-version` / `none`), so a release tag can
  be tied to the exact build — write a stamp in the deploy (or `git clone` the
  server and it works for free). With neither a `VERSION` file nor a stamp the
  version stays `0.0.0+unknown`, never an invented release.
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
- **`bench-agent.conf` is an allow-list.** The live launcher
  (`live/airootfs/usr/local/bin/bench-agent-env`) honours exactly
  `BENCHD_SERVER` / `BENCHD_TOKEN` (client config), `BENCH_AGENT_NAME` (the
  runner label; default the hostname) and `BENCH_AGENT_INSTANCE` (the instance
  suffix `bench-tui@.service` sets); every other key is ignored. There is **no**
  `BENCHD_INSECURE` toggle: the client never disables TLS verification, so use a
  real certificate (or the TLS reverse proxy above) for an HTTPS control plane.
  (`BENCHD_INSECURE` and `BENCH_AGENT_NAME` used to be accepted but unread —
  `BENCH_AGENT_NAME` is now wired to `Reporter.name`, `BENCHD_INSECURE` removed.)
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
  start/stop and friends. The queue is at-most-once (a fetched command is marked
  `sent`), so a fetched-but-unacked command is **re-queued once** (its `attempts`
  is bumped and it is delivered again); a second timeout marks it `expired`.
  `/metrics` exposes the queued depth (`benchd_command_queue_depth`) and the
  expired count (`benchd_commands_expired_total`).
- A late `/finish` (after the reaper already failed the run, or a duplicate
  finish) is refused with `409` instead of overwriting the closed run. Static
  dashboard assets are served with `ETag`/`Last-Modified` and a short
  `max-age`; the API stays `Cache-Control: no-store`.
- SQLite uses WAL; a run at 4 Hz writes ~4.5 MB per 15 minutes. Once an hour the
  scheduler sweeps the per-sample tables (**telemetry/logs/events only**) and the
  **artifacts** of **finished** runs older than `BENCHD_RETENTION_DAYS` (default 30
  days, `0` keeps everything), materialising each run's stats into the run row
  first. It never touches a running run; the run rows, phases, commands and
  summaries are kept, because that is what History, Trends, Compare and Fleet read
  (an artifact is downloadable data, not a headline metric, so it ages out with the
  samples). See
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

Stdlib only; each module starts an in-process `benchd` on an ephemeral port. It
covers the API (auth, per-runner keys, runs/phases/telemetry/logs/artifacts/stats,
commands, schedules, delete, reaper, SSE), control-plane P3 hardening (malformed
input → generic 4xx, the late-finish guard, command re-queue/expiry, artifact
sha256 verification and per-artifact delete, hot-path indexes, static-asset
caching, artifact retention, the version stamp), control-plane P2 enforcement
(server-side destructive-command confirmation, the exec allow/deny lists, runner
key rotation and TTL expiry, and the artifact size/quota/name limits), the
reporting client
(Reporter, PhaseTracker, ReporterPump, exec, inventory/summary builders), preset
parity across GUI/TUI/web, and the dashboard phase-table logic. It also keeps the
API contract honest: `tests/test_contract.py` checks `docs/openapi.json` against
the server's route table and the live dispatcher (every documented path+method is
dispatched, every dispatched route is documented) and checks the command
catalogue against `COMMAND_KINDS` and the confirm gate.

## Measured phase durations

See [`PHASE_TIMINGS.md`](PHASE_TIMINGS.md) for per-phase durations measured on a
reference workstation (used to estimate how long each preset takes).

## Roadmap

- Operator logins (per-user accounts) instead of the shared join token.
- Server-side run queues with priorities and nightly soak jobs.
- Phase-log artifacts and richer artifact browsing from the UI.
- Alerting on the control plane's `/metrics` series (throttle, TDR, over-temp);
  the Prometheus exposition itself ships — see [`OBSERVABILITY.md`](OBSERVABILITY.md).
