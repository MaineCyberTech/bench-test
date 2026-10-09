# omarchy — GeForce GTX 970 Linux run (2026-10-09)

First run of the toolkit on Linux, via the new `bench/Run-LinuxBench.sh` runner (Python phases only —
the PowerShell invokers are Windows-only). This folder is the run review for that session.

## Host

| | |
|---|---|
| OS | Omarchy (Arch-based), kernel 7.2.5 |
| CPU | Intel Core i7-3770 (4c/8t) @ 3.40 GHz |
| RAM | 15.6 GiB |
| GPU | NVIDIA GeForce GTX 970 (4 GB, GM204 sm_52), driver **580.178.04** |
| CUDA / torch | CUDA 12.6, torch **2.14.1+cu126** (last cu126 release; Maxwell sm_52 present) |
| GPU fan curve | `gpu-fan-curve.service` — `35:26 45:34 55:44 60:52 68:64 75:76 82:90 88:100` (lowered profile) |

## GPU fan curve (Linux)

The toolkit's Windows fan curve (`Install-GpuFanCurve.ps1` / `nvfancontrol`) was ported to Linux as a
systemd service wrapping `nvidia-settings` (run as root — user-level fan writes are blocked under
Wayland). The curve was then **lowered** to quiet the card while keeping sustained load below 70 °C.

| Profile | Curve (°C : fan %) | Sustained ~152 W plateau | Idle |
|---|---|---|---|
| Stock driver auto | — | 79 °C | ~34–36 % |
| Original custom | 40:45 50:55 60:70 68:80 75:92 82:100 | ~60–61 °C @ ~70 % | 45 % |
| Fan pinned 100 % (floor test) | — | 53–54 °C | — |
| **Final (in service)** | **35:26 45:34 55:44 60:52 68:64 75:76 82:90 88:100** | **66–67 °C @ ~59–62 %** | **28 % / ~1157 RPM @ 26 °C** |

- **The fan cannot be switched off.** The driver reports `GPUTargetFanSpeed` valid range **26–100**;
  the lowest duty is 26 % (~1150 RPM). Stock auto idles at ~34–36 %, so the curve's floor is lower
  than stock at idle and ~10 points lower under load, without exceeding 70 °C.
- Validation: 4-minute full load (8192² matmul, 152 W) sampled every 15 s — full log in
  `fan-curve-validation.log`. Peak 67 °C, never reached 70 °C.
- Service/script copies: `gpu-fan-curve.service`, `gpu-fan-curve.sh`.

## GPU compute

| Phase | Result |
|---|---|
| info | GTX 970 · CUDA 12.6 · 13 SMs · 3.9 GiB |
| matmul fp16 / bf16 | 2.6 / 1.8 TFLOPS |
| matmul tf32 / fp32 | 3.3 / 3.2 TFLOPS |
| membw fp32 add / reduce | 144 / 152 GB/s |
| membw bf16 add / reduce | 148 / 153 GB/s |
| PCIe H2D / D2H | 10.0 / 9.0 GB/s |
| cuDNN conv2d fp16 | 7.0 TFLOPS |
| VRAM integrity (3.0 GiB) | 0 mismatched / 0 bad |
| streams / soak | skipped (pre-Volta) |

## CPU

| Phase | Result |
|---|---|
| matmul fp32 / fp64 | 158.6 / 74.7 GFLOPS |
| membw copy / add / reduce | 16.0 / 12.0 / 13.0 GB/s |
| SHA-256 | 369 MB/s |
| zlib compress/decompress | 41 / 1443 MB/s |
| lzma compress/decompress | 5 / 5 MB/s |
| AES-256-GCM | 1123 MB/s |
| fp32 mul+add | 1.4 GFLOPS |
| 30 s soak | 153.0 GFLOPS |

## RAM

15.6 GiB total (11.6 free) · copy 16.9 / scale 11.4 / add 12.5 / triad 10.2 GB/s ·
latency ~12.9 ns/access · integrity 8.0 GiB, 2 passes, **0 bad** · 30 s soak 8.1 GB/s, 0 errors.

## Disk — `/home` (root LV)

seq write 3302 MB/s · seq read 10630 MB/s (page-cache assisted) · 4K rand read 337 335 IOPS /
write 10 195 / mix 35 259 · 4K latency p50/p99/p99.9 = 2.1 / 4.2 / 11.6 µs ·
5 GiB steady write 2847 → 1956 MB/s (drop 31 %).

_Phases use OS-buffered I/O, so reads and 4K numbers are partly cache; the steady write is the real
flash number._

## Findings / toolkit changes this session

1. **`gpu_bench.py` matmul phase looked like a hang on Maxwell** — the timing loop enqueued CUDA
   kernels far faster than a slow GPU executes them (mini-repro: 1 546 matmuls queued in 3 s, 5.9 s
   just to drain). A 10 s loop queued ~2 h of work, so `matmul` never returned and the first two
   full-battery attempts were killed. Fix: sync every iteration in `timed()` (negligible overhead
   for the multi-millisecond ops these phases measure). Matmul now completes in ~31 s.
2. **VRAM integrity OOM on 4 GB cards** — the phase filled ~90 % of free VRAM as fp32, then tried to
   allocate the same again as int32 without freeing the first pass; the verify also materialised a
   multi-GiB int64 temporary. Fix: free the fp32 pass (`del ts, t`) before the int pass, and verify
   the int chunks in 32 M-element slices. Integrity now passes: 3.0 GiB, 0 mismatched, 0 bad.
3. **Corsair iCUE Commander CORE case-fan control is broken on firmware 2.11.221** — OpenLinkHub /
   liquidctl ack duty writes but the RPM never changes (pump control and RGB do work). Case/radiator
   fans are stuck on the controller's internal curve; only the pump is software-controllable.
4. `streams` and `soak` remain skipped on pre-Volta (existing behaviour; multi-stream matmul hangs).

## Repro

```bash
BENCH_PY=.venv/bin/python bench/Run-LinuxBench.sh \
  --gpu-soak 60 --cpu-soak 30 --ram-soak 30 \
  --disk-path results/disk-tmp --steady-gb 5
```

## Artifacts

- `omarchy-linux-20261009-013222.{json,md}` — full battery run
- `fan-curve-validation.log` — fan-curve load validation samples
- `gpu-fan-curve.sh` / `gpu-fan-curve.service` — the curve as installed
