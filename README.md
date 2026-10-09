# bench-test — workstation benchmark & stress toolkit

A reusable, host-agnostic toolkit to **baseline and stress a Windows workstation** the same way
every time: GPU compute, HBM bandwidth, PCIe, VRAM integrity, tensor-core soak, LLM inference,
video encode (NVENC), and a game-like render — all with live telemetry and a JSON + Markdown report.

Built to compare machines (e.g. before/after a GPU install or a driver update) with reproducible numbers.

## What it measures

| Phase | Script | Metric |
|---|---|---|
| System/GPU inventory | `bench/Get-GpuInfo.ps1` | GPU model, driver version, VRAM, CPU, RAM |
| Compute (matmul sweep) | `bench/gpu_bench.py matmul` | TFLOPS fp16 / bf16 / tf32 / fp32 |
| Memory bandwidth | `bench/gpu_bench.py membw` | GB/s (copy, reduction, add) |
| PCIe host↔device | `bench/gpu_bench.py pcie` | H2D / D2H GB/s |
| cuDNN convolutions | `bench/gpu_bench.py conv` | TFLOPS |
| VRAM integrity | `bench/gpu_bench.py integrity` | bit-exact fill/verify of ~90 % VRAM |
| Stream scaling | `bench/gpu_bench.py streams` | TFLOPS vs 1/2/4/8 CUDA streams |
| Endurance soak | `bench/gpu_bench.py soak` | sustained TFLOPS + thermals over N seconds |
| **CPU** | `bench/Invoke-CpuBench.ps1` | matmul fp32/fp64, mem bw, **SHA-256**, **zlib/lzma compression**, **AES-256-GCM**, fp32 FLOPS, soak |
| LLM inference | `bench/Invoke-LlmBench.ps1` | tokens/s per model (Ollama) |
| Video engine (NVENC) | `bench/Invoke-NvencBench.ps1` | fps h264/hevc/av1 + concurrent sessions |
| Game-like render | `bench/Invoke-ValleyBench.ps1` | Unigine Valley score/FPS (optional) |
| **RAM** | `bench/Invoke-RamBench.ps1` | bandwidth (copy/scale/add/triad GB/s), random-access latency, **integrity** (fill+verify a large fraction of RAM), soak |
| **Disk (HDD/SSD/NVMe)** | `bench/Invoke-DiskBench.ps1` | sequential + random 4 KiB IOPS, **4 KiB latency percentiles**, **steady-state write**, plus SMART |
| **Full-system soak** | `bench/Invoke-FullSystemSoak.ps1` | GPU + CPU + RAM + disk **concurrently** + stability verdict (opt-in) |
| **System inventory (deep)** | `bench/Get-SystemInfo.ps1` | CPU, DIMMs (part/speed), GPU + PCIe link gen/width, volumes, NICs, monitors, USB, boot time |
| **Hardware sensors** | `bench/Get-Sensors.ps1` | temps/fans/voltages/power/loads/clocks via LibreHardwareMonitor (elevated = full) |
| **Network** | `bench/Invoke-NetBench.ps1` | ping latency + download/upload Mbps (Cloudflare) |
| **Device topology** | `bench/Get-PcieInfo.ps1` | all PCI/PCIe devices + vendor/device IDs + driver versions |
| **Health history** | `bench/Get-EventHealth.ps1` | WHEA / TDR / disk / Kernel-Power-41 event history + disk SMART |
| **Environment** | `bench/Get-EnvInfo.ps1` | OS update level, drivers, installed apps, Defender/firewall/TPM/SecureBoot/BitLocker, battery/Bluetooth |

Telemetry (`bench/Sample-GpuTelemetry.ps1`) samples `util`, `temp`, `power`, `SM clock`, `VRAM`, `fan` once/second.

## Quick start (on the target workstation)

Requirements: Windows 10/11, an NVIDIA GPU (works CPU-only but pointless), PowerShell 5.1+,
Python 3.10+ (`python` on PATH), and `git`.

```powershell
git clone https://github.com/MaineCyberTech/bench-test.git
cd bench-test
# one-command battery (installs PyTorch CUDA build if missing, writes results\<host>-<date>.{json,md})
powershell -ExecutionPolicy Bypass -File .\bench\Run-WorkstationBench.ps1
```

### Long endurance soak
```powershell
# disable sleep first, then soak (detached so it survives an RDP drop)
powercfg -change -standby-timeout-ac 0
powershell -ExecutionPolicy Bypass -File .\bench\Invoke-LongSoak.ps1 -Hours 4
```
`Invoke-LongSoak.ps1` runs the 4-stream soak for `-Hours`, samples telemetry every
`-IntervalSeconds`, and writes `<host>-longsoak-<stamp>.{md,json}` with min/avg/max thermals,
loaded-sample drift, throttle reasons and any TDR/WHEA events.

Options:
```powershell
.\bench\Run-WorkstationBench.ps1 -SoakSeconds 600 -SkipLlm -SkipNvenc -SkipValley
```
| Flag | Effect |
|---|---|
| `-SoakSeconds N` | endurance soak length (default 600 s; use 1800 for a full soak) |
| `-SkipLlm` | skip Ollama LLM tests |
| `-SkipNvenc` | skip NVENC video tests |
| `-SkipValley` | skip the Unigine Valley game-like test |
| `-TorchIndex <url>` | PyTorch wheel index (default `cu128`; match your driver era) |
| `-NoInstall` | never install anything; only run what's present |
| `-OutDir <path>` | results directory (default `.\results`) |

### Linux (GPU/CPU/RAM/disk + soaks, network, NVENC, graphics)

The Python phases also run on Linux via `bench/Run-LinuxBench.sh` (no PowerShell needed):

```bash
python3 -m venv .venv
.venv/bin/pip install numpy psutil cryptography        # core deps
# GPU phases additionally need CUDA PyTorch; Maxwell/Pascal (pre-Turing) use cu126 wheels:
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu126

BENCH_PY=.venv/bin/python ./bench/Run-LinuxBench.sh    # full run (all groups)
BENCH_PY=.venv/bin/python ./bench/Run-LinuxBench.sh --skip-gfx --gpu-soak 60
.venv/bin/python ./bench/gui.py                        # graphical dashboard (Tk)
```

Groups: `gpu` · `cpu` · `ram` · `disk` · `combined` (GPU+CPU) · `fullsoak`
(GPU+CPU+RAM+disk) · `net` · `nvenc` (ffmpeg) · `sys` (inventory) · `gfx` (FurMark 2,
optional — drop the linux64 build in `tools/furmark/` or set `$FURMARK`).
Useful flags: `--gpu-soak S`, `--combined-seconds S`, `--fullsoak-seconds S`,
`--gfx-seconds S`, `--skip-<group>`.

Every run writes `results/<host>-linux-<stamp>.{json,md}` plus a **1 Hz telemetry time
series** (`*.telemetry.csv`: GPU util/temp/fan/power/clocks + CPU package temp + RAM),
raw per-phase stdout/stderr under `*-logs/` (with `nvidia-smi -q`, `sensors`, and
kernel/error journal snapshots), and appends a row to `results/runs-index.jsonl` — the
flat run index for trend analysis / datalake queries.

## Driver check / update

`bench/Get-GpuInfo.ps1` reports the installed NVIDIA driver version and the GPU's PCI device id.
To update, install the **latest NVIDIA driver** for that GPU (Game Ready / Studio). A helper to
resolve + download the current driver by device id is planned; for now use
<https://www.nvidia.com/en-us/drivers/>.

Power-limit control (needs admin):
```powershell
.\bench\Set-GpuPowerLimit.ps1 -Info        # min/default/max/current
.\bench\Set-GpuPowerLimit.ps1 -Watts 220   # cap heat/coil-whine (one UAC prompt)
.\bench\Set-GpuPowerLimit.ps1 -Watts 285   # restore full
```

Custom GPU fan curve (more aggressive than the quiet stock curve; persistent at logon):
```powershell
.\bench\Install-GpuFanCurve.ps1                         # default curve + logon task
.\bench\Install-GpuFanCurve.ps1 -Curve '40:40,55:55,70:75,80:95'
.\bench\Install-GpuFanCurve.ps1 -Uninstall              # remove task / restore auto fan
```

## Interpreting results

- **Compute** should approach the GPU's spec sheet (see `docs/METHODOLOGY.md` for reference numbers).
- **Soak drift**: compare the first vs last telemetry window. A steady temperature climb with flat
  power usually means *case airflow* is the limit (ramp chassis fans in BIOS), not the GPU cooler.
- **Throttle reasons**: `nvidia-smi -q -d PERFORMANCE` — `SW Power Cap` is expected at the power
  limit; `HW Thermal Slowdown`/`HW Power Braking` indicate a real problem.
- **Stability**: the report flags any TDR (`Event ID 4101`) / `nvlddmkm` / WHEA / BugCheck events.

## Layout

```
bench/     PowerShell runners + Python phases + telemetry sampler (+ Linux runner and collectors)
docs/      METHODOLOGY.md (how it works, reference numbers, gotchas) · FUTURE_WORK.md (not-yet-collected data)
examples/  real results (Windows + Linux runs) for templates
results/   output (git-ignored) — reports, telemetry CSVs, per-phase logs, runs-index.jsonl
```

## Scripts

| Script | Purpose |
|---|---|
| `Run-WorkstationBench.ps1` | one-command battery (info + compute + optional LLM/NVENC/Valley) |
| `Run-LinuxBench.sh` | Linux one-command battery (GPU/CPU/RAM/disk + combined + full-system soak + net/NVENC/sys/gfx) |
| `Invoke-LongSoak.ps1` | multi-hour endurance soak with telemetry + report |
| `Invoke-GpuBench.ps1` | compute phases with telemetry → JSON + Markdown report |
| `gpu_bench.py` | matmul · membw · pcie · conv · integrity · streams · soak |
| `Get-GpuInfo.ps1` | system/CPU/RAM/GPU + driver version + PCI id |
| `Sample-GpuTelemetry.ps1` | standalone telemetry sampler |
| `Invoke-CpuBench.ps1` / `cpu_bench.py` | CPU matmul fp32/fp64 + mem bandwidth + soak |
| `Invoke-CombinedStress.ps1` | CPU+GPU concurrent stress |
| `Invoke-RamBench.ps1` / `ram_bench.py` | RAM bandwidth/latency/integrity/soak |
| `Invoke-DiskBench.ps1` / `disk_bench.py` | disk seq/random throughput + latency + steady-state + SMART |
| `Invoke-FullSystemSoak.ps1` | GPU+CPU+RAM+disk concurrent soak |
| `Get-SystemInfo.ps1` | deep inventory: CPU/DIMM/GPU/PCIe/volumes/NICs/monitors/USB/boot |
| `Get-Sensors.ps1` | all hardware sensors (LibreHardwareMonitor) |
| `Invoke-NetBench.ps1` | network latency + download/upload Mbps |
| `Get-PcieInfo.ps1` | PCI/PCIe device topology + driver versions |
| `Get-EventHealth.ps1` | WHEA/TDR/disk/Kernel-Power history + disk SMART |
| `Get-EnvInfo.ps1` | OS update level, drivers, software, security, battery/Bluetooth |
| `Invoke-LlmBench.ps1` | Ollama tokens/s |
| `Invoke-NvencBench.ps1` | NVENC h264/hevc/av1 fps |
| `Invoke-ValleyBench.ps1` | Unigine Valley game-like run |
| `Set-GpuPowerLimit.ps1` | query/set power limit |
| `Install-GpuFanCurve.ps1` | custom GPU fan curve + logon task |
| `telemetry_log.py` | 1 Hz Linux telemetry CSV (GPU/CPU temps, fan, power, RAM, load) |
| `tui.py` | htop-style console dashboard (live phases, telemetry bars, log, collect) |
| `gui.py` | Tkinter dashboard (presets, live progress + telemetry graph, results, collect) |
| `fan_curve.py` | portable GPU fan curve + fan-response test (NVIDIA nvidia-settings / AMD sysfs) |
| `net_bench.py` | Linux network latency + Cloudflare download/upload Mbps |
| `nvenc_bench.py` | Linux NVENC encode fps via ffmpeg |
| `linux_info.py` | Linux inventory: OS/CPU/mem/GPU/PCIe/storage/NICs/boot/errors |

## License

Proprietary — see `LICENSE`.
