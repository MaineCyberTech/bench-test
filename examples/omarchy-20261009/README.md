# omarchy — GeForce GTX 970 Linux run (2026-10-09)

First full Linux run of the toolkit via `bench/Run-LinuxBench.sh` (no PowerShell).
The final battery covers GPU compute + 600 s soak, CPU, RAM, disk, combined
GPU+CPU stress, a full-system soak (GPU+CPU+RAM+disk), network, NVENC (skipped),
a FurMark graphics benchmark and a fan-response check — all with a 1 Hz telemetry
time series and raw per-phase logs.

## Host

| | |
|---|---|
| OS | Omarchy (Arch-based), kernel 7.2.5 |
| CPU | Intel Core i7-3770 (4c/8t) @ 3.40 GHz |
| RAM | 15.6 GiB |
| GPU | NVIDIA GeForce GTX 970 (4 GB, GM204 sm_52), driver **580.178.04** |
| CUDA / torch | CUDA 12.6, torch **2.14.1+cu126** (last cu126 release; Maxwell sm_52 present) |
| Storage | SanDisk SSD PLUS 1000GB (btrfs, `compress=zstd:3`) |
| GPU fan curve | `35:26 45:34 55:44 60:52 68:64 75:76 82:90 88:100` (noted in the report header) |

## GPU fan curve

The toolkit's Windows fan curve (`Install-GpuFanCurve.ps1` / `nvfancontrol`) has a
portable Linux equivalent — `bench/fan_curve.py` (NVIDIA via nvidia-settings, AMD
via amdgpu sysfs) with `set` / `run` / `off` / `status` / `check`. This machine
runs a persistent systemd curve; every run report now records the active curve.

| Profile | Curve (°C : fan %) | Sustained 152 W plateau | Idle |
|---|---|---|---|
| Stock driver auto | — | 79 °C | ~34–36 % |
| Original custom | 40:45 50:55 60:70 68:80 75:92 82:100 | ~60–61 °C @ ~70 % | 45 % |
| Fan pinned 100 % (floor test) | — | 53–54 °C | — |
| **Final (in service)** | **35:26 45:34 55:44 60:52 68:64 75:76 82:90 88:100** | **66–67 °C @ ~59–62 %** | **28 % / ~1157 RPM @ 26 °C** |

- **The fan cannot be switched off**: `GPUTargetFanSpeed` range is 26–100 % on
  this card (driver-enforced floor).
- Final run's **fan response check: `ok`** — 90 s ramp: 52 → 64 °C, fan 47 → 56 %.
- Curve files + load-validation log: `gpu-fan-curve.*`, `fan-curve-validation.log`.

## GPU compute

| Phase | Result |
|---|---|
| matmul fp16 / bf16 | 2.6 / 1.9 TFLOPS |
| matmul tf32 / fp32 | 3.2 / 3.2 TFLOPS |
| membw fp32 add / reduce | 148 / 155 GB/s |
| membw bf16 add / reduce | 148 / 154 GB/s |
| PCIe H2D / D2H | 10.0 / 9.0 GB/s |
| cuDNN conv2d fp16 | 7.1 TFLOPS |
| VRAM integrity (3.0 GiB) | 0 mismatched / 0 bad |
| streams | skipped (pre-Volta) |
| **600 s soak** | **1.8 TFLOPS · 60/65/66 °C · fan 45/60/61 % · 151 W** |

## CPU

| Phase | Result |
|---|---|
| matmul fp32 / fp64 | 157.7 / 75.6 GFLOPS |
| membw copy / add / reduce | 16 / 12 / 13 GB/s |
| SHA-256 | 372 MB/s |
| zlib compress/decompress | 42 / 1452 MB/s |
| lzma compress/decompress | 5 / 5 MB/s |
| AES-256-GCM | 1121 MB/s |
| fp32 mul+add | 1.4 GFLOPS |
| 120 s soak | 152.6 GFLOPS |

## RAM

15.6 GiB total (11.7 free) · copy 17.2 / scale 11.5 / add 12.3 / triad 10.0 GB/s ·
latency ~12.9 ns/access · integrity 8.0 GiB, 2 passes, **0 bad** ·
120 s soak 8.5 GB/s, 0 errors.

## Disk — `/home` (btrfs + zstd on SATA SSD)

| Phase | Result |
|---|---|
| seq write (incompressible) | 281 MB/s |
| seq read | 9148 MB/s (page cache) |
| 4K rand read / write / mix | 315 497 / 13 551 / 35 232 IOPS |
| 4K latency p50 / p99 / p99.9 | 2.2 / 5.1 / 12.1 µs |
| **10 GiB steady write** | **192 → 197 MB/s (flat)** |

_Test data is incompressible and `steadywrite` is fsync-bounded per 64 MiB, so
these are real storage numbers (btrfs `compress=zstd:3` cannot inflate them)._

## Combined stress (GPU+CPU, 300 s)

GPU 1.8 TFLOPS · CPU 97.1 GFLOPS · 63/65/67 °C · fan 48/59/62 % · 151 W.

## Full-system soak (GPU+CPU+RAM+disk, 300 s)

GPU 1.8 TFLOPS · CPU 80.9 GFLOPS · RAM 4.5 GB/s (0 errors) · disk 278 MB/s ·
63/66/66 °C · **verdict: clean** (exit codes [0,0,0,0], 0 Xid lines).

## Network

ping 31.4/35.1/37.3 ms · download 276.3 Mbps · upload 42.1 Mbps.

## NVENC

Skipped on this machine: ffmpeg requires NVENC API 13.1 (driver ≥ 610) but the
GTX 970 is on the 580 legacy branch (API 13.0). Recorded as a skip, not a failure.

## Graphics (FurMark 2.10.2, 1080p OpenGL, 60 s)

**Score 3017 · 50/50/52 fps · max 58 °C** — consistent across three runs
(3019 / 3019 / 3017), thermal-limited by design at ~60 fps with the quiet curve.

## Fan response check

**`ok`** — temp 52 → 64 °C with fan 47 → 56 % over the 90 s ramp.

## System

Omarchy · kernel 7.2.5 · i7-3770 (8 threads) · 15.6 GiB · GTX 970 driver 580.178.04 ·
SanDisk SSD PLUS 1000GB + zram · 197 boot-time error lines (pre-existing).

## Telemetry

1 Hz sampler (2 025 samples over 2 098 s): GPU temp 30/55.7/67 °C ·
fan 27/49.2/62 % · power avg 112 W / max 160 W. Full CSV in the artifacts.

## Findings / toolkit changes this session

1. **`gpu_bench.py` matmul looked like a hang on Maxwell** — the timing loop
   enqueued kernels far faster than a slow GPU drains them. Fixed with a
   per-iteration sync; matmul now completes in ~31 s.
2. **VRAM integrity OOM on 4 GB cards** — the fp32 pass was not freed before the
   int32 pass and the verify materialised a multi-GiB temporary. Fixed; 3.0 GiB
   with 0 mismatches.
3. **Disk measurements were inflated** — constant fill bytes were compressed by
   btrfs-zstd, and `steadywrite` measured page cache. Fixed (incompressible data,
   fsync-bounded windows); the runner warns when the disk path is tmpfs. The
   earlier runs in this folder that predate the fix are superseded.
4. **GPU soak re-enabled on pre-Volta** with per-2 s telemetry (temp/fan/power).
5. **New runner groups**: combined stress, full-system soak, network, NVENC,
   inventory, FurMark graphics, fan-response; plus a 1 Hz telemetry CSV,
   per-phase logs, `nvidia-smi`/`sensors`/journal snapshots and an append-only
   `runs-index.jsonl` run index (datalake).
6. **Dashboards**: `bench/tui.py` (htop/nvtop-style, auto-starts on the live USB)
   and `bench/gui.py` (flat dark Tk) — live phases, CPU/GPU/RAM temps + fan
   RPM, results summary, collect-to-USB.
7. **Live USB**: `live/Build-LiveUSB.sh` builds bootable test sticks
   (`nvidia` / `nvidia-legacy` / `amd`) with the toolkit and dashboard baked in.
8. **iCUE Commander CORE case-fan control is broken on fw 2.11.221** (pump and
   RGB work; fan duty writes are ignored) — from the earlier session on this box.

## Earlier artifacts (superseded runs)

- `omarchy-linux-20261009-013222.*` — first battery (no GPU soak, no graphics).
- `omarchy-linux-20261009-012249.*` — VRAM-integrity OOM (pre-fix).
- The **final** run is `omarchy-linux-20261009-031453.*`.

## Artifacts

- `omarchy-linux-20261009-031453.{json,md}` — final full battery report
- `omarchy-linux-20261009-031453.telemetry.csv` — 1 Hz telemetry (2 025 samples)
- `omarchy-linux-20261009-031453-logs/` — raw per-phase stdout/stderr, FurMark
  logs + GPU CSV, nvidia-smi/sensors snapshots, kernel messages
- `runs-index.jsonl` — the run index for this machine (one line per run)
- `fan-curve-validation.log`, `gpu-fan-curve.sh`, `gpu-fan-curve.service` — fan curve

_Linux run via Run-LinuxBench.sh. See `bench/tui.py` / `bench/gui.py` for the live
dashboards and `live/` for the bootable test USB._
