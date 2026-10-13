# bench-test — workstation benchmark, stress & fleet control plane

A reusable, host-agnostic toolkit to **baseline and stress workstations** the same
way every time — GPU compute, VRAM, PCIe, tensor-core soak, LLM inference, video
encode, CPU, RAM, disk, network, thermals, sensors, and a game-like render — with
live 4 Hz telemetry, a JSON + Markdown report per run, and an optional
**control plane** that turns a set of machines into one live, controllable fleet.

Built to compare machines (before/after a GPU, driver, or cooling change) with
reproducible numbers, and to catch instability under sustained load.

**Release version** lives in `VERSION` and is reported everywhere it matters: the
GUI/TUI headers, each runner (so the fleet view says what a box is running), the
web dashboard, and `GET /health` on the control plane. It is a release line plus
an automatic build: `<release>+<n>.<commit>`, so every update is a distinct
version rather than one number shared by many builds. Release artifacts name it
too (`ISO <gpu> v<release> #<run>`).

- **Runs on:** Windows 10/11 (PowerShell + Python), Linux (Python + Bash), and a
  bootable **live USB** (Arch) that needs no install.
- **Dependency-free core:** the runners, dashboards, agent and control plane use
  only the Python standard library. PyTorch/numpy are only needed by the compute
  phases (auto-installed on Windows; venv on Linux).

## Highlights

- **12 independently selectable test groups** (nothing runs implicitly).
- **Live dashboards** — a Tk GUI and a curses TUI with server link status,
  per-phase table, telemetry tiles + graphs, and result collection.
- **Control plane (`benchd`)** — a stdlib HTTP + SSE + SQLite server with a web
  dashboard: live runs, history, trends, compare, schedules, a live console,
  remote terminal, full inventory per run, and one-click start/stop/update.
- **Rich telemetry + full system inventory** captured for every run.
- **Backfill** existing `results/` into the server (`import_results.py`).

## What it measures

| Group | Runner phase(s) | Metric |
|---|---|---|
| `sys` | inventory + sensors + PCIe + health + env | CPU/DIMMs/GPU/driver/volumes/NICs/monitors, temps/fans, WHEA/TDR history |
| `gpu` | `gpu_bench.py`: info, matmul, membw, pcie, conv, integrity, streams, soak | TFLOPS fp16/bf16/tf32/fp32, GB/s, PCIe, cuDNN, VRAM integrity, sustained soak |
| `cpu` | `cpu_bench.py`: matmul, membw, hash, compress, aes, flops, soak | GFLOPS, GB/s, SHA-256, zlib/lzma, AES-256-GCM, fp32 FLOPS |
| `ram` | `ram_bench.py`: bandwidth, latency, integrity, soak | GB/s, ns/access, fill+verify, soak |
| `disk` | `disk_bench.py`: seqwrite/read, randread/write/mix, latency, steadywrite | MB/s, IOPS, µs percentiles, SLC-exhaustion |
| `combined` | `Invoke-CombinedStress.ps1` | GPU + CPU concurrently |
| `fullsoak` | `Invoke-FullSystemSoak.ps1` | GPU + CPU + RAM + disk **concurrently** + verdict |
| `net` | `net_bench.py` | ping latency, Cloudflare download/upload Mbps |
| `nvenc` | `nvenc_bench.py` | h264/hevc/av1 fps + concurrent sessions |
| `llm` | `Invoke-LlmBench.ps1` (Ollama) | tokens/s per model |
| `fan` | `Run-LinuxBench.sh` fan check | fan response / ramp under load |
| `gfx` | `Invoke-ValleyBench.ps1` / FurMark 2 | render score / fps |

## Telemetry

4 Hz rows per run (nvidia-smi cached at 1 Hz), written to `*.telemetry.csv` and streamed to the control plane.
Canonical fields: `elapsed, temp_c, fan_pct, power_w, gpu_util, gpu_fan_rpm,
cpu_util, ram_used, sm_mhz`, plus extras: `mem_util, mem_mhz,
vram_used_mib, cpu_temp_c, cpu_max_core_c, cpu_fan_rpm, load1, swap_used_gib,
nvme_c, gpu_enc_pct, gpu_dec_pct, pstate, power_limit_w, temp_mem_c, gr_mhz,
temp_limit_c, power_default_w, power_max_w, video_mhz, pcie_gen, pcie_width,
retired, throttle, throttle_bits, disk_read_mbs, disk_write_mbs, net_rx_mbs,
net_tx_mbs, ram_total_gib, cpu_pkg_w, cpu_mhz, psi_io, gpu_3d_pct,
gpu_copy_pct`. The runner only logs what the hardware exposes; blanks are
recorded rather than guessed. `server/benchd` derives std/p95/p99 per metric
plus energy, clock residency, throttled %, and thermal/power headroom.

## Quick start

**Windows workstation**
```powershell
git clone https://github.com/MaineCyberTech/bench-test.git
cd bench-test
powershell -ExecutionPolicy Bypass -File .\bench\Run-WorkstationBench.ps1
# dashboards (bootstrap a .venv on first run):
.\bench-gui.cmd        # graphical
.\bench-tui.cmd        # console
```
No Python? Download a prebuilt GUI/TUI bundle from
[Releases](https://github.com/MaineCyberTech/bench-test/releases?q=app-) — see
[*Packaged app*](#packaged-app--download-no-python-install) below.

**Linux workstation / live USB**
```bash
python3 -m venv .venv && .venv/bin/pip install numpy psutil cryptography
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu126  # pre-Turing
BENCH_PY=.venv/bin/python ./bench/Run-LinuxBench.sh
BENCH_PY=.venv/bin/python ./bench/gui.py     # or ./bench/tui.py
```

Common flags: `--gpu-soak S`, `--combined-seconds S`, `--fullsoak-seconds S`,
`--cpu-soak S`, `--ram-soak S`, `--disk-seconds S`, `--steady-gb G`, `--passes N`,
`--gpu-size N`, `--cpu-size N`, `--ram-size-mb N`, `--soak-until-stable`,
`--skip-<group>` for the groups the platform implements — all of them except
`fan` (Linux-only) on Windows and `llm` (Windows-only) on Linux; `--skip-fan`
and `--skip-llm` are accepted everywhere so the same profile can be sent to any
runner. `--gfx-seconds S` applies to the Linux graphics phase (Unigine Valley's
length is fixed by its preset on Windows).

`--soak-until-stable` makes the GPU soak stop early once the temperature has
plateaued (2 s-spaced samples; stable when the mean drift across ~16 s is under
0.5 °C, and never before 45 s) — `--gpu-soak` / `--fullsoak-seconds` become the
ceiling, not a fixed clock. The result records `"stable"` and the achieved
`seconds`. It is applied to the GPU soak, the combined soak and the fullsoak's
GPU component (the heat source).

## Dashboards

`bench/gui.py` (Tk) and `bench/tui.py` (curses) both: pick a **preset**, toggle
groups, set soak durations, watch the live phase table + telemetry, and collect
results. Both show **control-plane link status** and, when connected, stream
phases/telemetry/logs and accept remote start/stop. **Closing a dashboard cancels
its run.**

**Stabilized full soak (15 min)** is the "everything, but bounded" preset: all
groups except `llm`, small compute sizes (`gpu 4096`, `cpu 2048`, `ram 512 MiB`,
1 pass, 2 GB steady write) and short soaks, with the GPU soak running *until the
temperature plateaus* rather than for a fixed time. A preset carries those extra
knobs in its `tune` field (see `extra_runner_args`); they are the same flags the
runners already accepted, so the web start dialog exposes them directly.

Other flags: `gui.py --tab N --self-test`, `tui.py --demo --exit-after S`, and
`--instance NAME` on both (a label used when several dashboards share a host,e.g.
`tty1`); `agent.py --once` runs a single battery and exits.

On Windows, launch the GUI with **`pythonw.exe`** (`.venv\Scripts\pythonw.exe
bench\gui.py`): started with `python.exe` it keeps a console window open for as long
as the dashboard runs. The run's own children are already spawned with
`CREATE_NO_WINDOW`, and the packaged bundles are built without a console.

## Packaged app — download (no Python install)

Prebuilt **GUI + TUI + agent** bundles are attached to each **App bundles**
release. Download one and run — no Python and no toolkit install:

| Platform | Bundle |
|---|---|
| **Windows** | `bench-app-windows.zip` |
| **Linux** | `bench-app-linux.zip` |

Download from **Releases → newest “App bundles” build**:
<https://github.com/MaineCyberTech/bench-test/releases?q=app->
· or from the command line: `gh release download app-<n> -p 'bench-app-*.zip'`.

Each zip contains `bench-gui/` (GUI), `bench-tui/` (TUI) and `bench-agent/`.

```bash
unzip bench-app-windows.zip        # -> bench-gui/  bench-tui/  bench-agent/
./bench-gui/bench-gui              # GUI   (Windows: dist\bench-gui\bench-gui.exe)
./bench-tui/bench-tui              # TUI   (console dashboard; needs a real terminal)
./bench-agent/bench-agent --server http://lab:8722 --token …   # fleet agent
```

Each bundle carries its own interpreter (PyInstaller) **and the `bench/` toolkit**,
so the dashboards/agent run with nothing installed, and a **local battery** works
too: on first run the app provisions a runner interpreter + deps — numpy/psutil
always, plus DirectML (`onnxruntime-directml`) for AMD/Intel or CUDA PyTorch for
NVIDIA — into a per-user cache (`%LOCALAPPDATA%\bench-test` on Windows,
`~/.local/share/bench-test` elsewhere) via `bench/bootstrap.py`. Clear it any time
with **Clean up environment** in the GUI (or `python bench/bootstrap.py clean`); a
repo `.venv` or `$BENCH_REPO` still takes precedence when present. See
[`bench/bootstrap.py`](bench/bootstrap.py) for `status` / `ensure` / `clean`.

A downloaded app also **updates itself in place**: the launcher runs the dashboard
and the runners from `<bundle>/bench/` on disk, so the control plane's `update`
command — or the startup check, throttled by `BENCH_APP_UPDATE_HOURS` (default 6;
`0` checks every launch) — refreshes that toolkit and the app restarts into the
new code. Only the launcher and its interpreter come from the download, so
updating *those* still needs a new bundle.

CI also **bakes the control-plane config** into each bundle — the
`BENCH_AGENT_CONF` secret, the same one the live USB writes to
`/etc/bench-agent.conf` — so a downloaded app **auto-registers** with no flags
(the GUI/TUI/agent read `<toolkit>/bench-agent.conf` as the last fallback, so a
`./bench-agent.conf`, `~/.config/bench-agent.conf` or `/etc/bench-agent.conf` on
the machine still points it at a different control plane). The
releases are public, so that token is world-readable: use a limited, rotatable
one.

Build the bundles yourself with `python packaging/build_app.py` (on Windows also
`pip install windows-curses` so the TUI can be bundled); set `BENCH_BAKE_AGENT_CONF`
(or `BENCH_BAKE_AGENT_CONF_FILE`) to bake your own. CI builds Windows + Linux
(`.github/workflows/build-app.yml`) and attaches them to each `app-<n>` release.

## Control plane (`benchd`)

A single pane of glass over a fleet. Stdlib HTTP + SSE + SQLite, plus a
self-contained web dashboard.

```bash
# server (Proxmox lab box / anywhere with python3)
python3 server/benchd.py --host 0.0.0.0 --port 8722     # token via /etc/benchd.conf

# a machine reports + is controllable:
BENCHD_SERVER=http://lab:8722 BENCHD_TOKEN=… python3 bench/agent.py
python3 bench/gui.py --server http://lab:8722 --token …    # GUI also checks in
python3 bench/tui.py --server http://lab:8722 --token …    # TUI (live USB) too
```

Open `http://<server>:8722/?token=<token>`. Tabs: **Live** (a fleet view —
online runners first, stale ones behind a toggle, each with gauges, charts, live
console, remote terminal, plus confirm-gated **reboot / shut down**), **History**
(sortable/filterable, with a **▲/▼ vs the previous run of the same preset on
that host**), **Trends** (per-series median / p95 / max), **Compare**, **Fleet**
(median throughput + peak temp **per machine** and **per GPU model**),
**Schedules**. Run detail shows the summary, hardware **verdicts** (WHEA/TDR/
ECC/disk), remarks, full **system inventory**, per-field **min/avg/max · std ·
p95/p99**, derived **perf/watt, energy, thermal headroom, time throttled, clock
residency**, phases, console and downloads. Charts have hover tooltips; `Esc`
closes dialogs, `/` jumps to search.

- **Auth:** shared join token for operators; per-runner API keys for runners.
- **Runner identity:** stable per machine (`machine_id` + role) — a re-registration
  or an ephemeral live-USB boot reuses one row instead of piling up duplicates,
  and the UI shows `name #short-uid` so hosts sharing a hostname differ.
- **Backfill:** `BENCHD_TOKEN=… python3 server/import_results.py --server URL --dir results`
  (`--token-file PATH` / `$BENCHD_TOKEN_FILE` also work — a token on `argv` is visible
  in `ps`). Each unit gets a deterministic id, so re-running is a no-op; `--reset`
  deletes every existing run first (all pages, not just the first 1000).
- Full API + deployment (Proxmox LXC): [`docs/CONTROL_PLANE.md`](docs/CONTROL_PLANE.md).

## Presets

Identical across GUI/TUI/web; each sets every group and the four duration boxes,
and carries the extra knobs the boxes do not cover in a `tune` field where it has
one — anything absent falls back to the runner's defaults.

| Preset | Groups | Soaks | Tune (extra knobs) |
|---|---|---|---|
| Quick check (short soaks) | all except llm/fan | gpu 60 · combined 60 · fullsoak 60 · gfx 30 | cpu soak 60 · ram soak 30 · disk 15 s / 256 MB · gpu matrix 4096 · cpu 2048 · ram 512 MiB · 1 pass · steady 2 GB |
| Stabilized full soak (15 min) | all except llm | gpu 180 · combined 45 · fullsoak 90 · gfx 30 | cpu soak 45 · ram soak 30 · disk 30 s / 512 MB · gpu matrix 4096 · cpu 2048 · ram 512 MiB · 1 pass · steady 2 GB · soak-until-stable (60 s hold) |
| System info + 60s soak (all) | sys, fullsoak | fullsoak 60 | runner defaults |
| Full run (defaults) | all 12 | gpu 600 · combined 300 · fullsoak 300 · gfx 60 | runner defaults |
| GPU compute | gpu, sys | gpu 60 | runner defaults |
| CPU + RAM + disk | cpu, ram, disk, sys | — | runner defaults |
| Storage + network | disk, net, sys | — | runner defaults |
| Video / graphics | nvenc, gfx, sys | gfx 60 | runner defaults |
| Graphics only (FurMark/Valley) | gfx, sys | gfx 30 | gfx engine `furmark` |
| LLM inference (Ollama) | llm, sys | — | runner defaults |
| Fan response | fan, sys | — | runner defaults |
| Soak only (long) | gpu, fullsoak, sys | gpu 1800 · fullsoak 1800 | runner defaults |
| Full-system soak (long) | fullsoak, sys | fullsoak 1800 | runner defaults |

Measured per-phase durations to estimate preset runtimes:
[`docs/PHASE_TIMINGS.md`](docs/PHASE_TIMINGS.md).

## Updates available

Each runner reports the identity of the toolkit it is running, and the control
plane compares it with its own commit — so the Fleet tab's **toolkit** column,
a per-runner **update available** badge and a **stale** counter show which sticks
are behind. Update one from its card's *update…* dialog (see below).

## Live USB

`live/Build-LiveUSB.sh` builds a bootable Arch ISO carrying the toolkit at
`/opt/bench-test`. It boots to the TUI, checks in to the control plane when a
config is present, and collects results to the **BENCHDATA** partition.

```bash
sudo ./live/Build-LiveUSB.sh --gpu nvidia-legacy --write /dev/sdX
# or via the "Build live ISO" GitHub workflow (attaches the ISO to a release).
# The workflow rebuilds automatically on push to main touching bench/** or live/**.
```

### Updating a live runner without reflashing

A booted stick is read-only, so "updating" means pointing the toolkit at a fresher
copy on the stick's writable partition. The control plane can do that remotely —
from the fleet card's **update…** dialog, or as commands:

| Command | Payload | Effect |
|---|---|---|
| `update_toolkit` | `{ref, sha256?}` | fetch the toolkit at `ref` (branch/tag/sha), verify, extract onto BENCHDATA and re-point `/opt/bench-test`; re-pointed again at boot |
| `iso_fetch` | `{tag, gpu?}` | download a release's ISO parts, reassemble and check `SHA256SUMS` |
| `iso_write` | `{iso, device, confirm:true}` | write a staged ISO to **another** block device (refuses anything backing `/`, `/opt` or BENCHDATA) |
| `iso_write_slot` | `{iso, confirm:true}` | **A/B reflash**: write the staged ISO to the *inactive* slot and flip the boot flag; the active slot and BENCHDATA are untouched, and grub auto-reverts after three unconfirmed boots |

`update` on a live runner now runs the toolkit update automatically (a git
checkout still does `git pull`). An update **restarts the runner** so the new code
is what actually runs — otherwise it keeps reporting the commit it started with and
the "update available" badge never clears. The restart is deferred while a run is
in progress (force it with `payload.restart`). The same operations are available
locally:
```bash
python bench/live_update.py toolkit --ref main      # self-update + switch
python bench/live_update.py iso-list iso-nvidia-legacy-38
python bench/live_update.py iso-fetch iso-nvidia-legacy-38
python bench/live_update.py iso-write-slot <iso> --yes
python bench/live_update.py iso-write <iso> --device /dev/sdX --yes
```

BENCHDATA is **vfat**, so it cannot hold the `current` symlink that names the
fetched copy; the fetch leaves a `current.txt` pointer instead, and the boot-time
re-point follows the symlink, then that pointer, then falls back to the freshest
fetched copy. A stick flashed before that fix keeps using the toolkit baked into
its ISO until it is updated once more (or reflashed).

Kernel/driver/package changes need a new image. On an **A/B stick** the runner can
take that itself, with no hands on it: `iso_fetch` stages the ISO in RAM, then
`iso_write_slot` writes it to the *inactive* slot and flips the boot flag, and the
next `reboot` boots it — if that image fails to boot three times, grub falls back
to the slot that last worked. A single-slot stick still has to be written from
another device or reflashed in person (a running system cannot safely overwrite
the partition it is booted from).

Pick the release by the **commit in its notes**, not by its number or date: the tag
is `iso-<gpu>-<run_number>`, and a run number says nothing about which commit was
built (`iso-nvidia-legacy-61` was two commits behind `iso-nvidia-legacy-65`).

### Updating at start

The GUI, TUI and agent **check their own checkout when they start** and
fast-forward it when it is behind (`git pull --ff-only`), then restart into the new
code. The check is skipped when the tree has local changes (your edits win), when
it is not a git checkout (a live stick updates through `update_toolkit` and the
boot link instead), and when offline. Set `BENCH_NO_UPDATE=1` to opt out.

To auto-connect, put this on the BENCHDATA partition (or bake it via the
`BENCH_AGENT_CONF` secret / `live/private/bench-agent.conf`):
```
BENCHD_SERVER=http://lab:8722
BENCHD_TOKEN=…
```
The launchers mount BENCHDATA on demand and the dashboards retry registration, so
the stick checks in as soon as the network is up. The disk group is **skipped** on
the live USB unless a real internal volume is mounted at `/mnt/internal` (never
benchmarks the USB or RAM); desktop/CI hosts test the machine's real disk.

## Tests

Run it from the repo venv — the TUI tests import `curses` (needs
`windows-curses` on Windows), a TUI check needs `psutil`, and a RAM-integrity
check only runs when `numpy` is importable:

```bash
.venv\Scripts\python.exe tests\run_all.py     # Windows — RESULT: PASS
.venv/bin/python tests/run_all.py             # Linux / live USB
```

The suite itself adds no dependencies; it starts an in-process `benchd` on an
ephemeral port and covers the control-plane API, the reporting client, preset
parity across GUI/TUI/web, and the dashboard phase-table logic.

## Interpreting results

- **Compute** should approach the GPU's spec sheet ([`docs/METHODOLOGY.md`](docs/METHODOLOGY.md)).
- **Soak drift:** compare first vs last telemetry window; a steady temp climb with
  flat power is usually case airflow, not the GPU cooler.
- **Throttle reasons:** `nvidia-smi -q -d PERFORMANCE` — `SW Power Cap` is expected
  at the power limit; `HW Thermal Slowdown`/`HW Power Braking` are real problems.
- **Stability:** the report flags TDR (`Event ID 4101`) / `nvlddmkm` / WHEA / BugCheck.

## Layout

```
bench/     PowerShell runners + Python phases + telemetry + dashboards + agent
server/    benchd control plane (stdlib) + web dashboard + importer + systemd units
live/      live-USB build + airootfs (launchers, units, conf)
tests/     stdlib test suite (run_all.py)
docs/      METHODOLOGY · CONTROL_PLANE · PHASE_TIMINGS · FUTURE_WORK · DATA ·
           PRIVACY · KNOWN_ISSUES
examples/  real Windows + Linux result sets
results/   output (mostly git-ignored; a few runs tracked as history)
```

`results/` keeps the newest 100 run directories and one Valley frame per run
(`BENCH_KEEP_RUNS=0` keeps everything); the control plane sweeps the per-sample
telemetry, logs and events of finished runs after 30 days
(`BENCHD_RETENTION_DAYS=0` keeps everything) — see
[`docs/DATA.md`](docs/DATA.md#5-retention).

## Scripts

| Script | Purpose |
|---|---|
| `Run-WorkstationBench.ps1` / `Run-LinuxBench.sh` | one-command battery (all groups) |
| `run_win.py` | Windows runner bridging the dashboards/agent to the battery + telemetry |
| `gpu_bench.py` · `cpu_bench.py` · `ram_bench.py` · `disk_bench.py` · `net_bench.py` · `nvenc_bench.py` | phases |
| `Invoke-*.ps1` | the Windows battery's phase implementations (GPU/CPU/RAM/disk/net/NVENC/LLM/gfx/soaks) |
| `telemetry_log.py` | telemetry CSV (Linux) |
| `gui.py` · `tui.py` | Tk / curses dashboards (report + accept remote control) |
| `fan_curve.py` · `gpu_util.py` · `rawlog.py` | GPU fan-curve tool, AMD/Intel gpu telemetry, raw-slot result writer |
| `benchreport.py` | control-plane client (Reporter, PhaseTracker, Pump, exec, inventory) |
| `bootstrap.py` | provision the runner interpreter + deps in a per-user cache (`status` / `ensure` / `clean`) |
| `agent.py` | headless agent: register, run on command, stream, update (`--once` for a single run) |
| `live_update.py` | live-USB toolkit self-update + A/B ISO write, without the control plane |
| `server/benchd.py` | control plane: REST + SSE + SQLite + web dashboard |
| `server/import_results.py` | backfill existing results into the server |
| `upload.py` | upload a results dir to GitHub |
| `linux_info.py` · `Get-*.ps1` | inventory / sensors / PCIe / health / env |
| `packaging/build_app.py` | build the standalone GUI/TUI/agent bundles (PyInstaller) |
| `tests/run_all.py` | the test suite |

## Security

The control plane uses a shared join token (operators) and per-runner keys
(runners). Bind it to the LAN/lab; put TLS in front if exposed further. Tokens and
the SQLite db are git-ignored — never commit them. The repo and both release
streams are public — app bundles (`.github/workflows/build-app.yml`) and live ISOs
(`.github/workflows/build-iso.yml`) — so the control-plane config baked into either
is world-readable: use a limited, rotatable value, and rotate it if an artifact
leaves the lab. Today that baked value is the shared join token, which is also the
operator bearer; when the registration-only join-token model lands, bake that
instead so a downloaded artifact cannot act as an operator (see
[`docs/CONTROL_PLANE.md`](docs/CONTROL_PLANE.md) § Roadmap). Report a leaked or
baked credential privately — see [`SECURITY.md`](SECURITY.md).

## License

Proprietary — see [`LICENSE`](LICENSE).
