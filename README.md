# bench-test — workstation benchmark, stress & fleet control plane

A reusable, host-agnostic toolkit to **baseline and stress workstations** the same
way every time — GPU compute, VRAM, PCIe, tensor-core soak, LLM inference, video
encode, CPU, RAM, disk, network, thermals, sensors, and a game-like render — with
live 1 Hz telemetry, a JSON + Markdown report per run, and an optional
**control plane** that turns a set of machines into one live, controllable fleet.

Built to compare machines (before/after a GPU, driver, or cooling change) with
reproducible numbers, and to catch instability under sustained load.

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

1 Hz per run, written to `*.telemetry.csv` and streamed to the control plane.
Canonical fields: `elapsed, temp_c, fan_pct, power_w, gpu_util, gpu_fan_rpm,
cpu_util, ram_used, sm_mhz, mem_used`, plus extras: `mem_util, mem_mhz,
vram_used_mib, cpu_temp_c, cpu_max_core_c, cpu_fan_rpm, load1, swap_used_gib,
nvme_c, gpu_enc_pct, gpu_dec_pct, pstate, power_limit_w, disk_read_mbs,
disk_write_mbs, net_rx_mbs, net_tx_mbs`. The runner only logs what the hardware
exposes; blanks are recorded rather than guessed.

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

**Linux workstation / live USB**
```bash
python3 -m venv .venv && .venv/bin/pip install numpy psutil cryptography
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu126  # pre-Turing
BENCH_PY=.venv/bin/python ./bench/Run-LinuxBench.sh
BENCH_PY=.venv/bin/python ./bench/gui.py     # or ./bench/tui.py
```

Common flags: `--gpu-soak S`, `--combined-seconds S`, `--fullsoak-seconds S`,
`--gfx-seconds S`, `--skip-<group>` for any of the 12 groups (`--skip-llm` and
`--skip-fan` are accepted by both runners).

## Dashboards

`bench/gui.py` (Tk) and `bench/tui.py` (curses) both: pick a **preset**, toggle
groups, set soak durations, watch the live phase table + telemetry, and collect
results. Both show **control-plane link status** and, when connected, stream
phases/telemetry/logs and accept remote start/stop. **Closing a dashboard cancels
its run.**

## Packaged app (no Python install)

Prebuilt bundles of the dashboards + agent are attached to each **App bundles**
release, so the GUI/TUI can run on another machine without installing Python or
the toolkit:

```bash
gh release download app-<n> -p 'bench-app-*.zip'   # or grab it from Releases
unzip bench-app-windows.zip                        # -> bench-gui/ bench-tui/ bench-agent/
./bench-gui/bench-gui                              # or bench-tui / bench-agent
```

Each bundle carries its own interpreter (PyInstaller) and the dashboards/agent
are stdlib-only, so **viewing/controlling runs and connecting to a control plane
need nothing else.** Running a *local* battery additionally needs a toolkit
checkout (for numpy/PyTorch) — point the app at it with `--repo DIR` or
`$BENCH_REPO`, or unzip the bundle next to a `bench/` directory. Build the
bundles yourself with `python packaging/build_app.py`; CI builds them for
Windows + Linux (`.github/workflows/build-app.yml`).

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

Open `http://<server>:8722/?token=<token>`. Tabs: **Live** (runner cards, gauges,
charts, live console, remote terminal, controls), **History** (per-run system +
preset + metrics), **Trends**, **Compare**, **Schedules**. Run detail shows the
summary, remarks, full **system inventory**, per-field **telemetry min/avg/max**,
phases, console, and downloads (report JSON, `telemetry.csv`).

- **Auth:** shared join token for operators; per-runner API keys for runners.
- **Runner identity:** stable per machine (`machine_id` + role) — a re-registration
  or an ephemeral live-USB boot reuses one row instead of piling up duplicates,
  and the UI shows `name #short-uid` so hosts sharing a hostname differ.
- **Backfill:** `python3 server/import_results.py --server URL --token T --dir results`
- Full API + deployment (Proxmox LXC): [`docs/CONTROL_PLANE.md`](docs/CONTROL_PLANE.md).

## Presets

Identical across GUI/TUI/web; each sets every group and duration.

| Preset | Groups | Soaks |
|---|---|---|
| Quick check (short soaks) | all except llm/fan | gpu 60 · combined 60 · fullsoak 60 · gfx 30 |
| System info + 60s soak (all) | sys, fullsoak | fullsoak 60 |
| Full run (defaults) | all 12 | gpu 600 · combined 300 · fullsoak 300 · gfx 60 |
| GPU compute | gpu, sys | gpu 60 |
| CPU + RAM + disk | cpu, ram, disk, sys | — |
| Storage + network | disk, net, sys | — |
| Video / graphics | nvenc, gfx, sys | gfx 60 |
| LLM inference (Ollama) | llm, sys | — |
| Fan response | fan, sys | — |
| Soak only (long) | gpu, fullsoak, sys | gpu 1800 · fullsoak 1800 |
| Full-system soak (long) | fullsoak, sys | fullsoak 1800 |

Measured per-phase durations to estimate preset runtimes:
[`docs/PHASE_TIMINGS.md`](docs/PHASE_TIMINGS.md).

## Live USB

`live/Build-LiveUSB.sh` builds a bootable Arch ISO carrying the toolkit at
`/opt/bench-test`. It boots to the TUI, checks in to the control plane when a
config is present, and collects results to the **BENCHDATA** partition.

```bash
sudo ./live/Build-LiveUSB.sh --gpu nvidia-legacy --write /dev/sdX
# or via the "Build live ISO" GitHub workflow (attaches the ISO to a release).
# The workflow rebuilds automatically on push to main touching bench/** or live/**.
```

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

```bash
python tests/run_all.py          # RESULT: PASS
```
Stdlib only; starts an in-process `benchd` on an ephemeral port. Covers the
control-plane API, the reporting client, preset parity across GUI/TUI/web, and the
dashboard phase-table logic.

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
docs/      METHODOLOGY · CONTROL_PLANE · PHASE_TIMINGS · FUTURE_WORK
examples/  real Windows + Linux result sets
results/   output (mostly git-ignored; a few runs tracked as history)
```

## Scripts

| Script | Purpose |
|---|---|
| `Run-WorkstationBench.ps1` / `Run-LinuxBench.sh` | one-command battery (all groups) |
| `run_win.py` | Windows runner bridging the dashboards/agent to the battery + telemetry |
| `gpu_bench.py` · `cpu_bench.py` · `ram_bench.py` · `disk_bench.py` · `net_bench.py` · `nvenc_bench.py` | phases |
| `telemetry_log.py` | 1 Hz telemetry CSV (Linux) |
| `gui.py` · `tui.py` | Tk / curses dashboards (report + accept remote control) |
| `benchreport.py` | control-plane client (Reporter, PhaseTracker, Pump, exec, inventory) |
| `agent.py` | headless agent: register, run on command, stream, update |
| `server/benchd.py` | control plane: REST + SSE + SQLite + web dashboard |
| `server/import_results.py` | backfill existing results into the server |
| `upload.py` | upload a results dir to GitHub |
| `linux_info.py` · `Get-*.ps1` | inventory / sensors / PCIe / health / env |
| `tests/run_all.py` | the test suite |

## Security

The control plane uses a shared join token (operators) and per-runner keys
(runners). Bind it to the LAN/lab; put TLS in front if exposed further. Tokens and
the SQLite db are git-ignored — never commit them. The repo and its ISO releases
are public, so a token baked into an ISO is world-readable; rotate it if a stick
leaves the lab.

## License

Proprietary — see [`LICENSE`](LICENSE).
