# Methodology

## Goal
Produce **comparable numbers** across workstations so an upgrade (GPU, driver, cooling) can be
measured, and so a machine can be stress-tested for stability and thermals.

## Phases and why
| Phase | Load type | What it reveals |
|---|---|---|
| matmul sweep (fp16/bf16/tf32/fp32) | tensor-core saturated | peak compute; matches spec sheet |
| membw (add/reduce/copy) | memory-bandwidth saturated | HBM throughput (~spec) |
| pcie (pinned H2D/D2H) | interconnect | host↔GPU transfer |
| conv2d (cuDNN) | convolution/tensor | DL inference path |
| integrity (VRAM fill/verify) | memory | bit errors on a large allocation |
| streams (1/2/4/8) | concurrency | scales to the compute ceiling |
| soak (N seconds, 4 streams) | sustained | thermal steady-state + drift + stability |
| LLM (Ollama) | real inference | tokens/s per model; VRAM residency |
| NVENC (ffmpeg) | video engine | encode fps + concurrent sessions |
| Valley (Unigine) | game-like render | score/FPS (note: often CPU-bound at 1080p) |

## Telemetry
`nvidia-smi` sampled once/second: `utilization.gpu`, `temperature.gpu`, `power.draw`,
`clocks.sm`, `memory.used`, `fan.speed`. Throttle reasons are read after the soak
(`nvidia-smi -q -d PERFORMANCE`). Stability is checked against System events
(4101 TDR, `nvlddmkm`, WHEA, BugCheck).

## Reference points (RTX 4070 Ti SUPER, 285 W, driver 617.14 / CUDA 13.4, Xeon W-2123)
| Metric | Observed |
|---|---|
| fp16 / bf16 matmul | ~89 TFLOPS |
| tf32 / fp32 matmul | ~44 / 26 TFLOPS |
| D2D copy / reduction | ~590 / 611 GB/s |
| PCIe H2D / D2H | ~12 GB/s (sync-per-iter, latency bound) |
| cuDNN conv2d fp16 | ~62 TFLOPS |
| bmm fp16 / SDPA fp16 | ~84 / ~53 TFLOPS |
| 600 s mixed soak | ~86 TFLOPS sustained, 81 °C, 285 W, no throttle |
| VRAM integrity (13 GiB) | 0 mismatches, 0 bad elements |
| qwen3:8b / deepseek-coder-v2:16b | ~100 / ~180–206 tok/s |
| NVENC h264/hevc/av1 (1080p) | ~275 / ~398 / ~362 fps |
| Unigine Valley High 1080p | score ~3471, 83 FPS (CPU-bound) |

Expect ±5–10 % run-to-run variance. Compare like-for-like (same resolution, same power limit).

## RAM & disk phases
| Phase | Load | What it reveals |
|---|---|---|
| `ram_bench.py bandwidth` | numpy STREAM copy/scale/add/triad | system RAM bandwidth (GB/s) |
| `ram_bench.py latency` | large random gather | ~ns/access + gather GB/s |
| `ram_bench.py integrity` | fill + verify a large fraction of free RAM | memory bit errors |
| `ram_bench.py soak` | sustained bandwidth + alloc churn | stability / error count |
| `disk_bench.py seqwrite/seqread` | 1 MiB sequential I/O | drive sequential MB/s |
| `disk_bench.py randread/randwrite/randmix` | random 4 KiB | IOPS + MB/s |
| `Invoke-DiskBench.ps1` | + `Get-StorageReliabilityCounter` | SMART temp / wear / error counts |

Caveats:
- **RAM bandwidth is single-threaded** (numpy), so it under-reports plain multi-channel peak;
  it is a like-for-like comparison. Quad-channel DDR4-2666 typically shows ~15-25 GB/s here.
- **Disk reads may be cache-served** (buffered I/O): use a file larger than RAM for truer reads.
  Write numbers and random IOPS are representative; write to a **non-system** drive when possible.

## Gotchas learned in the field
- **Old benchmarks are CPU-bound.** Unigine Valley/Heaven at 1080p won't load a modern GPU
  (GPU util < 50 %). For a GPU-bound gaming number use 4K/DSR or a modern engine (3DMark).
- **Power limit changes everything.** `nvidia-smi -pl <W>` caps heat/power; 200 W gave ~93 % of
  285 W throughput but ran ~10–15 °C cooler with much less coil whine. Record the power limit in results.
- **Drift = airflow.** If soak temperature climbs steadily with flat power, the *case* airflow is
  the limit (ramp chassis fans in BIOS: HP Z-series → Power → Thermal → "Increase (PCIe) Idle Fan Speed").
- **Sleep kills long runs.** Disable sleep (`powercfg -change -standby-timeout-ac 0`) before a soak.
- **VRAM fill is destructive to other GPU work** — don't run while a training job/LLM is active.
- **32-bit benchmarks misreport VRAM** (e.g. 4095 MB for a 16 GB card).

## Reproducing
1. `git clone` this repo on the target.
2. Ensure the NVIDIA driver is current (`Get-GpuInfo.ps1` → driver version).
3. Run `Run-WorkstationBench.ps1` (it installs the matching PyTorch CUDA wheel if needed).
4. Compare `results/<host>-*.md` across machines.
