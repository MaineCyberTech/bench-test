# AGENTS.md — working in `bench-test`

Guidance for AI agents (and humans) contributing to **bench-test**, a host-agnostic
workstation benchmark & stress toolkit with a fleet control plane.

## What this repo is

- **`bench/`** — the runners and phases. PowerShell runners + Python phases for
  Windows, a Bash runner for Linux, a telemetry logger (4 Hz rows, expensive stats
  cached), and the GUI/TUI
  dashboards. Everything is **stdlib-only** (Python) — no pip installs required to
  run; the venv is only for numpy/PyTorch used by the compute phases.
- **`server/`** — `benchd`, a dependency-free control plane (HTTP + SSE + SQLite)
  and its self-contained web dashboard, plus `import_results.py`.
- **`live/`** — the bootable Arch live-USB build.
- **`tests/`** — the test suite (run it from the repo venv:
  `.venv\Scripts\python.exe tests\run_all.py` on Windows, `.venv/bin/python
  tests/run_all.py` on Linux; the TUI tests import `curses`, and some behaviour
  checks need `psutil`/`numpy`).
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
docs/      METHODOLOGY.md, CONTROL_PLANE.md, FUTURE_WORK.md, PHASE_TIMINGS.md,
           DATA.md, PRIVACY.md, KNOWN_ISSUES.md
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
else; a local battery needs a toolkit checkout (`$BENCH_REPO`).

## Control plane (benchd)

- **Auth:** operators use the shared **operator** token (`BENCHD_TOKEN`) as a
  bearer or `?token=`; runners get a **per-runner API key** at `/register` and
  reuse it (stored in `~/.config/bench-agent.json`). A separate **join token**
  (`BENCHD_JOIN_TOKEN`) is registration-only — accepted by `POST /api/v1/register`
  and nothing else — so public releases can ship one-click onboarding without
  carrying the operator token.
- **Runner identity:** a runner also sends a `machine_id` (`machine_id()` in
  `benchreport.py` — Windows `MachineGuid`, Linux DMI uuid / `/etc/machine-id`,
  else a random id persisted next to the config) and the server **adopts the row
  that machine already owns for its `kind` + `instance`**, so a re-registering
  runner or an ephemeral live-USB boot (whose `~/.config` does not persist)
  reuses one row instead of minting duplicates. The UI shows `label #short-uid`.
  The live USB runs a TUI on **both** tty1 and ttyS0: `bench-tui@.service` sets
  `BENCH_AGENT_INSTANCE=%i`, so they check in as `bench-live (tty1)` /
  `bench-live (ttyS0)` — genuinely distinct rows (a shared id would make a
  command run twice). The ttyS0 unit now only starts when `console=ttyS0` is on
  the kernel cmdline, so a normal boot has one dashboard/runner.
  `--self-test` runners are `ephemeral` and reaped once quiet.
- **Endpoints** (all `/api/v1`): `register`, `heartbeat`, `runs` (create/phase/
  telemetry/log/event/artifact/finish), `runners/<id>/commands` (+`/ack`),
  `schedules`, and the UI side `state`, `stream` (SSE), `runs`, `runs/<id>`,
  `runs/<id>/telemetry`|`telemetry.csv`|`logs`|`stats`|`artifact/<name>`, `trends`.
  Command kinds: start/stop/update/fan_curve/power_limit/restart/**shutdown**/
  **reboot**/**update_toolkit**/**iso_fetch**/**iso_write**/exec/shell — power
  commands stop any active run first and are confirmed in the UI; `update` is a
  `git pull` on a checkout and a toolkit self-update on a live stick.
- **Telemetry samples** may be a **named dict** (preferred) or a positional list;
  unknown numeric keys are stored in the `extra` JSON column and returned merged.
- **Closing a dashboard cancels its run**; a server-side reaper fails any run
  whose runner stops heartbeating, so runs never get stuck.

## Telemetry

4 Hz rows, per run (`--interval 0.25`). Only the **cheap** stats are sampled per
row: a row's `nvidia-smi` (GPU util/temp/fan/power/clocks/VRAM/pstate, ~66 ms a
call) is cached at 1 Hz (`--gpu-interval`, default 1.0) and
`sensors`/`nvidia-settings` (CPU temps, fan RPM) at 1 Hz (`--slow-interval`).
Polling everything every row cost ~0.3 s an iteration, so a 4 Hz request really
delivered 2.9 rows/s. Canonical
fields: `elapsed, temp_c, fan_pct, power_w, gpu_util,
gpu_fan_rpm, cpu_util, ram_used, sm_mhz, mem_used` plus extras (`mem_util,
mem_mhz, vram_used_mib, cpu_temp_c, cpu_max_core_c, cpu_fan_rpm, load1,
swap_used_gib, nvme_c, gpu_enc_pct, gpu_dec_pct, pstate, power_limit_w,
disk_read_mbs, disk_write_mbs, net_rx_mbs, net_tx_mbs, temp_mem_c, gr_mhz,
temp_limit_c, power_default_w, power_max_w, video_mhz, pcie_gen, pcie_width,
retired, throttle, throttle_bits`). The mapping lives in
`benchreport.CSV_ALIASES`; add new columns there **and** in `telemetry_log.py`
(Linux) / `run_win.py` (Windows). Both samplers also write **one `cpu_coreN` column
per logical CPU**, appended after the fixed columns; a test asserts each CSV stays
rectangular, because a short row would shift every column after it.

**AMD/Intel Windows has no `nvidia-smi`**, so `bench/Sample-Sensors.ps1`
(LibreHardwareMonitor) supplies GPU temp/power/clocks/util/VRAM, fan RPM, engine
loads, CPU temp/package watts/clock and per-core load. It is best-effort: a sensor
it cannot read goes out **blank, never a fake zero** (a 0 temp/power/clock is
treated as unreadable), and CPU temp/power/clock normally need an **administrator**
shell - without elevation those stay blank and the runner prints why instead of
failing silently. The DLL is vendored under `tools/lhm` and shipped in the app
bundle, with a mirror + retries when a download is needed. `run_win.py` maps its
`key=value` line in `_parse_lhm_line()` (unit-tested, so extending the script is
safe); a key the runner does not know is ignored rather than fatal.

The dashboards render the same fields from one shared list
(`benchreport.DASH_FIELDS`) and one hardware line (`benchreport.hardware_line()`),
so the GUI and TUI cannot drift — add a column there and both surface it (a test
asserts every `DASH_FIELDS` key is a real telemetry field).

**One writer per row.** A dashboard streams each sample live as the run happens,
and `upload_telemetry_csv` sends the run's CSV at the end. The dashboards pass what
they already streamed (`sent={csv: {elapsed, ...}}`) so the upload only fills gaps;
without it every sample is stored twice and the charts draw each point twice.
Headless `agent.py` streams nothing and sends every row. Keep it that way when
touching either path.

## Testing — required before every commit

Run the suite from the repo venv: the TUI tests import `curses` (`windows-curses`
on Windows), a TUI behaviour check needs `psutil`, and a RAM-integrity check only
runs when `numpy` is importable. The suite files themselves add no third-party
imports.

```bash
.venv\Scripts\python.exe tests\run_all.py     # Windows — RESULT: PASS expected
.venv/bin/python tests/run_all.py             # Linux / live USB
python -m py_compile bench/*.py server/*.py
bash -n bench/Run-LinuxBench.sh live/Build-LiveUSB.sh live/airootfs/usr/local/bin/bench-*
```

`tests/run_all.py` covers the control-plane API, the reporting client, preset
parity across GUI/TUI/web, and the dashboard phase-table logic. Add a test when
you add behaviour. Never commit with a failing suite.

## Security

- **Two tokens, two roles.** `BENCHD_TOKEN` is the **operator** token (bearer or
  `?token=`) and authorizes everything — state, commands, delete, schedules,
  `exec`/`shell`, SSE. `BENCHD_JOIN_TOKEN` is the **registration-only join
  token**: `POST /api/v1/register` accepts it and nothing else, so it mints a
  runner but never satisfies an operator check and is not a runner api_key.
- **Never bake the operator token into a public artifact.** The repo is **public**
  and ISO/app releases are **public downloads** — anything baked into the image is
  world-readable. Bake the **join token** for one-click onboarding; keep
  `BENCHD_TOKEN` operator-only. The bake secrets (`BENCH_AGENT_CONF` /
  `BENCH_UPLOAD_CONF`) are applied by `.github/workflows/build-iso.yml`; rotate
  the operator token if it ever leaks or an ISO is shared outside the lab.
- `server/benchd.conf`, `server/data/`, `live/private/`, `~/.config/bench-agent.conf`
  are git-ignored. Never commit a token, key, or the SQLite db.
- The remote `exec` command is a real shell on the runner — it's gated by the
  operator token; treat it as privileged.

## Conventions & gotchas

- **Presets** live in `gui.PRESETS` (dict), `tui.PRESETS` (list), and
  `ALL_GROUPS`/`gset`/`PRESETS` in `app.js`. Change all three together.
- **Versions are derived from git, not maintained by hand.** `bench_version()`
  reads `git describe --tags --long`: exactly on a tag it is the release
  (`1.1.0`), commits past it are build metadata (`1.1.0+3` = three commits after
  `v1.1.0`), and `version_string()` appends the commit (`1.1.0+3.abc1234`). So the
  number changes **on every commit** and the *release* changes **when a tag is
  pushed** — `.github/workflows/release.yml` does that automatically from the
  commit subjects (`feat:` → minor, otherwise patch) and keeps `VERSION` in step.
  A copy with no git (a live stick) falls back to the `VERSION` file it was fetched
  with, which that workflow keeps equal to the latest tag — so a stick and a
  checkout of the same release agree. `version_info()` is the single authoritative
  descriptor (release, build, commit, mode, ref, commits_since_tag, protocol) and
  every surface reads it: GUI/TUI headers, runner registration, the web, and the
  control plane (`/health`, `/state`; it is deployed as a copy with no `.git`, so
  `VERSION` ships with it and a copy deploy writes a `.deployed-commit` /
  `.bench-version` stamp — see `docs/CONTROL_PLANE.md` § Deployment). A malformed
  `VERSION` yields `0.0.0+invalid` rather than being passed around as a release,
  and a runner whose `PROTOCOL` differs is flagged in the fleet view.
  `tests/test_version.py` keeps this honest.
- **Tuning knobs beyond the four durations** (`cpu_soak`, `ram_soak`,
  `disk_seconds`, `disk_size_mb`, `gpu_size`, `cpu_size`, `ram_size_mb`, `passes`,
  `steady_gb`, `stable`) travel in a preset's `tune` field and in the start payload;
  both runners accept them as flags, and `benchreport.extra_runner_args` is the
  single mapping. Without them a run falls back to the runner's long defaults
  (cpu 120 s, ram 120 s, gpu matrix 12288, 10 GiB steady write, disk 2048 MB) —
  which is why a "Quick check" used to take ~31 min.
  **`disk_size_mb` is the disk lever, not `disk_seconds`:** the randread/randwrite/
  randmix phases are size-bound, so the 2048 MB default cost 226 s of a 1368 s
  desktop run and `disk_seconds` does not change that.
- **`--soak-until-stable`** (Linux) / `-SoakUntilStable` (Windows) runs the GPU
  soak until the temperature plateaus; both runners accept it, so keep them in
  step. The plateau test is `gpu_bench._temp_stable` / `_soak_continue`.
- **Phase lines:** runners emit `[group] label`; the dashboards and
  `benchreport.PhaseTracker` parse them. A new phase closes every still-open row.
  `run_win.py` also rewrites the bench scripts' `[phase] X` lines to `[group] X`
  so Windows sub-phases show as rows.
- **`--skip-llm`/`--skip-fan`** are accepted by both runners (`fan` is Linux-only,
  `llm` Windows-only) so the dashboards can pass the same skip set everywhere.
  Which groups a platform implements is declared once in
  `benchreport.supported_groups()` and advertised in each runner's
  `capabilities.groups`; the web start dialog disables the rest. Keep that in
  step with the batteries.
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
  when changing their top-of-file path setup. The bundle ships `bench/` next to the
  exe, so `_repo_dir()` finds it and a local battery needs no checkout.
  **A bundle can update itself fully, in place.** The build freezes only
  `packaging/launcher.py`; the dashboard and runners run from `<bundle>/bench/` on
  disk, so refreshing that directory updates the *whole* app - which is what the
  server's `update` command and the `auto_update()` startup check both do (the
  latter throttled, `BENCH_APP_UPDATE_HOURS`, default 6; `0` checks every launch).
  Keep it that way when touching the build: freezing `benchreport`/`tui.py` into the
  exe would shadow the on-disk copies and silently pin the app to the download's
  version. The toolkit's stdlib deps are bundled explicitly by `stdlib_imports()`
  because PyInstaller can only see what the launcher imports - and a missing one
  fails inside `except Exception`, so the failure is invisible. A dashboard update
  restarts the app (`update_needs_restart` honours `kind=app`), which is how the
  refreshed code actually takes effect.
  **A bundle carries two version files, deliberately:** `VERSION` (the release, read
  by `bench_version()`) and `.bench-version` (the build, read by
  `toolkit_version()`). With only the stamp a downloaded app reported
  `0.0.0+unknown` for its version. `bench_version()` also falls back to the stamp's
  `version` field, so a bundle built by an older script still recovers.
  **A bundle can update its shipped toolkit in place** (`update` →
  `_app_bundle_root`/`_install_into_bundle`): it is neither a git checkout nor a
  live runner, so the old "use git pull" guard refused it and a downloaded app
  could not update at all. Only `bench/` is replaced — the frozen dashboard itself
  needs a new bundle download, which the result says explicitly.
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
