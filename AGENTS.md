# AGENTS.md — working in `bench-test`

Guidance for AI agents (and humans) contributing to **bench-test**, a host-agnostic
workstation benchmark & stress toolkit with a fleet control plane.

## What this repo is

- **`bench/`** — the runners and phases. PowerShell runners + Python phases for
  Windows, a Bash runner for Linux, a 1 Hz telemetry logger, and the GUI/TUI
  dashboards. Everything is **stdlib-only** (Python) — no pip installs required to
  run; the venv is only for numpy/PyTorch used by the compute phases.
- **`server/`** — `benchd`, a dependency-free control plane (HTTP + SSE + SQLite)
  and its self-contained web dashboard, plus `import_results.py`.
- **`live/`** — the bootable Arch live-USB build.
- **`tests/`** — the stdlib test suite (`python tests/run_all.py`).
- **`docs/`**, **`examples/`**, **`results/`**.

## Ground rules

1. **Stdlib only in the control plane and dashboards.** `benchd.py`,
   `benchreport.py`, `agent.py`, `gui.py`, `tui.py`, `import_results.py`,
   `telemetry_log.py` must run on the live USB's system Python with nothing
   installed. No new third-party imports.
2. **Never break a run.** The control-plane client **fails soft**: if the server is
   unreachable the benchmark continues. Keep it that way.
3. **Presets are load-bearing and must stay identical** across the GUI (`gui.py`),
   TUI (`tui.py`) and web (`server/web/app.js`). Every preset must set **all**
   groups and **all** durations so switching is deterministic. `tests/test_presets.py`
   enforces this — run it after any preset change.
4. **Every battery phase is a group.** The Windows battery phases (sys, gpu, cpu,
   combined, llm, nvenc, gfx, ram, disk, net, fullsoak) and the Linux extra (fan)
   must all be individually selectable. Don't add a phase that can't be skipped.
5. **Windows ↔ Linux parity.** Both runners must produce the same summary metric
   keys (`gpu_soak_tflops`, `cpu_soak_gflops`, `ram_soak_gbps`,
   `disk_seqwrite_mbps`, `net.download_mbps`, `gpu_matmul_tflops.*`, `fullsoak`
   verdict, `telemetry` min/avg/max) so trends and compare line up.
6. **Evidence or nothing.** Don't fabricate numbers, timings or test results. If a
   phase was skipped or didn't run, record that — never invent a value.
7. **No secrets in the repo.** Tokens live in `server/benchd.conf`,
   `~/.config/bench-agent.conf`, `/etc/benchd.conf` and the `live/private/` dir
   (all git-ignored). See *Security* below.
8. **Line endings & exec bits.** Text files are **LF**; `*.sh` are executable
   (`100755`). A Windows checkout can silently CRLF them — verify with
   `git ls-files --eol` / `git ls-files -s`, and note that a CRLF `.sh` will fail
   on Linux with `$'\r': command not found`.

## Layout

```
bench/     runners (Run-*.{ps1,sh}, run_win.py), phases (*_bench.py), dashboards
           (gui.py, tui.py), control-plane client (benchreport.py, agent.py)
server/    benchd.py (API + web), web/ (dashboard), import_results.py, systemd units
live/      Build-LiveUSB.sh + airootfs (launchers, units, conf examples)
tests/     run_all.py + test_*.py (stdlib)
docs/      METHODOLOGY.md, CONTROL_PLANE.md, FUTURE_WORK.md, PHASE_TIMINGS.md
packaging/ build_app.py — PyInstaller bundles (CI: .github/workflows/build-app.yml)
examples/  real Windows + Linux result sets
results/   output (mostly git-ignored; a few runs are tracked as history)
```

## Running it

Windows (workstation):
```powershell
powershell -ExecutionPolicy Bypass -File .\bench\Run-WorkstationBench.ps1
.\bench-gui.cmd            # Tk dashboard    (bootstraps .venv on first run)
.\bench-tui.cmd            # curses dashboard
```

Linux (workstation / live USB):
```bash
BENCH_PY=python3 ./bench/Run-LinuxBench.sh --skip-gfx
./bench/gui.py   ./bench/tui.py   ./bench/agent.py
```

Control plane:
```bash
python3 server/benchd.py --host 0.0.0.0 --port 8722   # token via /etc/benchd.conf
python3 server/import_results.py --server URL --token T --dir results   # backfill
```

Packaged app (no Python install) — build the GUI/TUI/agent bundles:
```bash
pip install pyinstaller && python packaging/build_app.py --out dist
./dist/bench-gui/bench-gui        # ./dist/bench-tui/…  ./dist/bench-agent/…
```
CI builds Windows + Linux bundles and attaches them to an `app-<n>` release.
The bundles are stdlib-only, so connecting to a control plane needs nothing
else; a local battery needs a toolkit checkout (`--repo` / `$BENCH_REPO`).

## Control plane (benchd)

- **Auth:** operators use the shared join token (`BENCHD_TOKEN`) as a bearer or
  `?token=`; runners get a **per-runner API key** at `/register` and reuse it
  (stored in `~/.config/bench-agent.json`).
- **Runner identity:** a runner also sends a `machine_id` (`machine_id()` in
  `benchreport.py` — Windows `MachineGuid`, Linux DMI uuid / `/etc/machine-id`,
  else a random id persisted next to the config) and the server **adopts the row
  that machine already owns for its `kind`**, so a re-registering runner or an
  ephemeral live-USB boot (whose `~/.config` does not persist) reuses one row
  instead of minting duplicates. The UI shows `name #short-uid`; `--self-test`
  runners are `ephemeral` and reaped once quiet.
- **Endpoints** (all `/api/v1`): `register`, `heartbeat`, `runs` (create/phase/
  telemetry/log/event/artifact/finish), `runners/<id>/commands` (+`/ack`),
  `schedules`, and the UI side `state`, `stream` (SSE), `runs`, `runs/<id>`,
  `runs/<id>/telemetry`|`telemetry.csv`|`logs`|`stats`|`artifact/<name>`, `trends`.
- **Telemetry samples** may be a **named dict** (preferred) or a positional list;
  unknown numeric keys are stored in the `extra` JSON column and returned merged.
- **Closing a dashboard cancels its run**; a server-side reaper fails any run
  whose runner stops heartbeating, so runs never get stuck.

## Telemetry

1 Hz, per run. Canonical fields: `elapsed, temp_c, fan_pct, power_w, gpu_util,
gpu_fan_rpm, cpu_util, ram_used, sm_mhz, mem_used` plus extras (`mem_util,
mem_mhz, vram_used_mib, cpu_temp_c, cpu_max_core_c, cpu_fan_rpm, load1,
swap_used_gib, nvme_c, gpu_enc_pct, gpu_dec_pct, pstate, power_limit_w,
disk_read_mbs, disk_write_mbs, net_rx_mbs, net_tx_mbs`). The mapping lives in
`benchreport.CSV_ALIASES`; add new columns there **and** in `telemetry_log.py`
(Linux) / `run_win.py` (Windows).

## Testing — required before every commit

```bash
python tests/run_all.py            # RESULT: PASS expected (stdlib only)
python -m py_compile bench/*.py server/*.py
bash -n bench/Run-LinuxBench.sh live/Build-LiveUSB.sh live/airootfs/usr/local/bin/bench-*
```

`tests/run_all.py` covers the control-plane API, the reporting client, preset
parity across GUI/TUI/web, and the dashboard phase-table logic. Add a test when
you add behaviour. Never commit with a failing suite.

## Security

- The repo is **public**, and ISO releases are **public downloads** — anything
  baked into the image is world-readable. The control-plane token and the GitHub
  upload token are baked via the `BENCH_AGENT_CONF` / `BENCH_UPLOAD_CONF` secrets
  by `.github/workflows/build-iso.yml`; rotate the token if an ISO is shared
  outside the lab.
- `server/benchd.conf`, `server/data/`, `live/private/`, `~/.config/bench-agent.conf`
  are git-ignored. Never commit a token, key, or the SQLite db.
- The remote `exec` command is a real shell on the runner — it's gated by the
  operator token; treat it as privileged.

## Conventions & gotchas

- **Presets** live in `gui.PRESETS` (dict), `tui.PRESETS` (list), and
  `ALL_GROUPS`/`gset`/`PRESETS` in `app.js`. Change all three together.
- **Phase lines:** runners emit `[group] label`; the dashboards and
  `benchreport.PhaseTracker` parse them. A new phase closes every still-open row.
  `run_win.py` also rewrites the bench scripts' `[phase] X` lines to `[group] X`
  so Windows sub-phases show as rows.
- **`--skip-llm`/`--skip-fan`** are accepted by both runners (`fan` is Linux-only,
  `llm` Windows-only) so the dashboards can pass the same skip set everywhere.
- **The Windows battery has no `-SkipGpu` unless `Run-WorkstationBench.ps1` has it**
  — if you add a skip flag, wire it in the `.ps1`, in `run_win.py`, and in the
  dashboards' group list.
- **Windows manifests use 7-digit fractional seconds** — `datetime.fromisoformat`
  rejects them; normalise before parsing (see `import_results.epoch`).
- **Disk on live vs desktop:** desktop/CI hosts test the machine's real disk
  (scratch under `/var/tmp` when it is not tmpfs); the **live USB** is skipped
  unless a real volume is mounted at `/mnt/internal` — it never benchmarks the USB
  stick or RAM.
- **curses keys:** Python's `curses` has no `KEY_PGUP`/`KEY_PGDN`; use
  `KEY_PPAGE`/`KEY_NPAGE` (a PgUp press otherwise raises `AttributeError`).
- **CI builds the ISO automatically** on push to `main` touching `bench/**` or
  `live/**` (`.github/workflows/build-iso.yml`, default variant `nvidia-legacy`),
  and on manual dispatch.
- **Dashboards/agent are packaged for install-free use** (`packaging/build_app.py`
  → PyInstaller bundles; CI `.github/workflows/build-app.yml` attaches them to an
  `app-<n>` release). They resolve the toolkit via `_repo_dir()` —
  `$BENCH_REPO` → next to the frozen exe → cwd → repo root — so keep that helper
  when changing their top-of-file path setup.
- **Read result files as UTF-8.** Windows' default code page (cp1252) throws on
  the sysinfo/report JSON (`°C`, etc.), silently dropping the inventory — always
  pass `encoding="utf-8-sig"` when opening a run's artifacts.
- **Timing expectations:** see `docs/PHASE_TIMINGS.md` for measured per-phase
  durations (used to estimate preset runtimes).

## Do not

- Add a third-party dependency to the server/dashboards/agent.
- Let a phase run implicitly (un-selectable) — every test must be a group.
- Commit a token, the SQLite db, a built ISO, or a venv.
- Push to `main` with a red `tests/run_all.py`, a CRLF shell script, or a
  non-executable `*.sh`.
