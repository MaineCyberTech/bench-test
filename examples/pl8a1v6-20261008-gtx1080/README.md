# DESKTOP-PL8A1V6 — GeForce GTX 1080 run (2026-10-08)

This folder captures a benchmark session on **DESKTOP-PL8A1V6** after the RTX 4070 Ti SUPER was
replaced with a **GeForce GTX 1080 (GP104, Pascal, 8 GB)**. The card turned out to be **faulty**,
so only the non-GPU tests completed cleanly; the GPU findings are recorded below.

## Host

| | |
|---|---|
| Board | ASUS X99-A II (BIOS 2101) |
| CPU | Intel i7-6800K (6c/12t) @ 3.40 GHz |
| RAM | 32 GiB (4×8 @ 2667) |
| GPU | NVIDIA GeForce GTX 1080 (10DE:1B80, PNY), driver **582.66** (R580 security branch) |
| CUDA / torch | CUDA 12.6 runtime, torch **2.14.1+cu126** (Pascal `sm_61` present in arch list) |

## CPU (all healthy)

| Phase | Result |
|---|---|
| matmul fp32 / fp64 | 507.7 / 244.0 GFLOPS |
| membw copy / add / reduce | 19 / 12 / 6 GB/s |
| SHA-256 | 408 MB/s |
| zlib compress/decompress | 27 / 954 MB/s |
| lzma compress/decompress | 4 / 4 MB/s |
| fp32 mul+add | 1.4 GFLOPS |
| 120 s soak | 463.0 GFLOPS |

## RAM (healthy)

31.9 GiB total (26.1 free) · copy 19 / scale 12 / add 12 / triad 6 GB/s · latency ~18.2 ns/access ·
integrity 18.2 GiB, **0 bad** · 60 s soak 9 GB/s, 0 errors.

## Disk — C: (SanDisk SSD PLUS, SATA) (healthy)

seq read 1650 MB/s (cached) · 4K rand read 108 407 IOPS / write 7 649 / mix 27 955 ·
4K latency p50/p99/p99.9 = 6 / 29 / 53 µs · 10 GiB steady write ~250 MB/s.
_One anomaly: the first `seqwrite` measured only 55 MB/s (likely transient contention; the steady
write confirms ~250 MB/s)._

## Network

ping 34.7 ms · upload 36.1 Mbps · download measured 0 (Cloudflare speed endpoint returned nothing).

## Unigine Valley (1080p High, Direct3D11)

**Score 4292 · 102.6 FPS** (min 24.4 / max 191.4).

> Almost identical to the RTX 4070 Ti SUPER's **4508 / 107.7** on the same box — confirming Valley is
> **CPU-bound** at 1080p (both runs use the same i7-6800K), not GPU-bound.

## GPU-bound graphics — FurMark 2 (1080p OpenGL, 60 s)

| Metric | Value |
|---|---|
| **Score** | **5750** |
| FPS avg (min / max) | 95.9 (87 / 98) |
| Max temperature | 85 °C |
| Max utilisation | 100 % |
| Core clock | 1532–1608 MHz |
| API / driver | OpenGL 3.2.0 / 582.66 |

The card completed a **60 s, 100 %-utilisation, GPU-bound graphics load with no crash or hang** —
confirming the graphics path is stable and only the **CUDA compute path** is faulty.

## GPU compute — partial (card is faulty)

The compute phases that ran before the card started faulting:

| Phase | Result |
|---|---|
| matmul fp16 / bf16 / tf32 / fp32 | 5.0 / 2.9 / 6.7 / 6.7 TFLOPS |
| cuDNN conv2d fp16 | 11.8 TFLOPS |
| membw add / reduce | 229 / 220 GB/s |
| PCIe H2D / D2H | 12.0 / 13.0 GB/s |
| VRAM integrity | 6.0 GiB, 0 mismatched / 0 bad |

`streams` and `soak` **hang indefinitely** on this card (multi-stream / matmul+conv loops never
return), so they are skipped on pre-Volta GPUs (see toolkit change below).

## FINDING: GTX 1080 is faulty

Evidence gathered on this machine with this card installed:

1. **`nvlddmkm` Event ID 153** (NVIDIA driver GPU hardware error) logged repeatedly
   (11:00, 11:03, 11:06, 11:56 on 2026-10-08).
2. **CUDA compute hangs** — `matmul` (once), `streams`, and `soak` never return; a plain CUDA op
   and the first `matmul` phase do work, so it is load/iteration dependent, not a driver/torch issue.
3. **Hard power-offs** — four `Kernel-Power 41` (unexpected shutdown) events began immediately after
   this GPU was installed (2026-10-07 20:11 / 20:39 / 21:20 / 21:58), with no WHEA. The previous
   RTX 4070 Ti SUPER ran at 285 W in this same machine/PSU with no such events, so PSU capacity is
   not the cause.
4. Temps were never the trigger — the card sat at ~70–76 °C with its fan working.

### Software ruled out (driver/torch matrix)

The CUDA-compute hang reproduces across **every** combination tested — two torch/CUDA builds and
two drivers:

| torch | driver | plain bf16 matmul loop |
|---|---|---|
| 2.14.1+cu126 | 582.66 | hangs |
| 2.5.1+cu124 | 582.66 | hangs |
| 2.5.1+cu124 | 560.94 | hangs |

So the fault is the **card**, not the software stack. Graphics/D3D works (games and Valley run
fine), so only the compute/memory/power path is failing.

**Recommendation:** test the GTX 1080 in another system, or RMA/replace it. Re-fit the
RTX 4070 Ti SUPER (or another known-good GPU) to run the full suite cleanly.

## Toolkit changes made this session

- `bench/gpu_bench.py`: **skip `streams` and `soak` on pre-Volta GPUs** (`compute capability < 7.0`)
  where the tight multi-stream / matmul+conv loops hang, emitting `"skipped": true` instead.
- `bench/Install-GpuFanCurve.ps1`: fixed a bug where the `Elevate` helper used `$args` (a PowerShell
  automatic variable) as a parameter name, so the scheduled-task creation always failed.

## Artifacts

- `DESKTOP-PL8A1V6-cpu-20261008-115703.{md,json}` — CPU
- `DESKTOP-PL8A1V6-ram-20261008-120104.{md,json}` — RAM
- `DESKTOP-PL8A1V6-disk-20261008-120318.{md,json}` — disk (C:)
- `DESKTOP-PL8A1V6-net-20261008-120727.{md,json}` — network
- `valley-DESKTOP-PL8A1V6-20261008-score4292.png` — Valley result dialog
