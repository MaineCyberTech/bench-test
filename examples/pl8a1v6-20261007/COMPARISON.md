# Workstation comparison — DESKTOP-PL8A1V6 vs DESKTOP-7TUKMBJ

*Generated 2026-10-07. Both are RTX 4070 Ti SUPER at a 285 W power limit; the difference
is CPU, RAM and cooling. Sources: this folder (`examples/pl8a1v6-20261007/`, full 11-phase
run) and `examples/7tukmbj-20261007/`.*

## Hosts

| | DESKTOP-PL8A1V6 | DESKTOP-7TUKMBJ |
|---|---|---|
| Board | ASUS X99-A II | HP Z4 G4 Workstation |
| CPU | Intel i7-6800K (6c/12t) @ 3.40 GHz | Intel Xeon W-2123 (4c/8t) @ 3.60 GHz |
| RAM | 32 GiB (4×8 @ 2667) | 64 GiB |
| OS | Win 11 Pro build 26100 | Win 11 Pro build 26200 |
| GPU driver | 616.56 (32.0.16.1656) | 617.14 (32.0.16.1714) |

## GPU compute

| Metric | PL8A1V6 | 7TUKMBJ |
|---|---|---|
| matmul fp16 / bf16 (TFLOPS) | 88.1 / 88.5 | 90.2 / 90.3 |
| matmul tf32 / fp32 (TFLOPS) | 45.1 / 27.2 | 44.9 / 26.1 |
| cuDNN conv2d fp16 (TFLOPS) | 62.8 | 62.8 |
| streams 1/2/4/8 (TFLOPS) | 90.1 / 90.1 / 90.1 / 90.1 | 89.9 / 89.3 / 88.2 / 87.7 |
| membw add fp32 / bf16 (GB/s) | 616 / 618 | 617 / 619 |
| membw reduce fp32 / bf16 (GB/s) | 635 / 632 | 636 / 633 |
| PCIe H2D / D2H (GB/s) | 12.0 / 13.0 | 12.0 / 13.0 |
| VRAM integrity (13 GiB) | 0 mismatched / 0 bad | 0 / 0 |

## Endurance (600 s soak @ 285 W)

| Metric | PL8A1V6 | 7TUKMBJ |
|---|---|---|
| sustained TFLOPS | **80.7** | 78.4 |
| temp avg / max (°C) | **70.1 / 71** | 85.7 / **87** |
| power avg / max (W) | 281.2 / 284.6 | 282.1 / 285.0 |
| fan avg / max (%) | **60.4 / 62** | 99.8 / **100** |
| SW thermal slowdown | **0** | ~185 s |
| stability | no TDR/WHEA | no TDR/WHEA |

## CPU

| Metric | PL8A1V6 | 7TUKMBJ |
|---|---|---|
| logical CPUs | 12 | 8 |
| matmul fp32 / fp64 (GFLOPS) | **575.7 / 269.8** | 405.5 / 202.3 |
| membw copy / add / reduce (GB/s) | 22 / 14 / 7 | 18 / 14 / 6 |
| soak GFLOPS (120 s) | **506.2** | 291.7 |
| sha256 (MB/s) | 515 | — |
| zlib c/d (MB/s) | 35 / 1126 | — |
| lzma c/d (MB/s) | 7 / 6 | — |
| fp32 mul+add (GFLOPS) | 1.6 | — |

## Combined CPU+GPU soak (300 s)

| Metric | PL8A1V6 | 7TUKMBJ |
|---|---|---|
| GPU TFLOPS | **80.8** | 79.6 |
| CPU GFLOPS | **504.1** | 282.8 |
| GPU temp avg / max (°C) | **69.6 / 72** | 77.1 / **84** |
| GPU fan max (%) | **63** | **100** |
| SW thermal slowdown | **0** | ~185 s |

## NVENC (1080p60 transcode, fps)

| Codec | PL8A1V6 | 7TUKMBJ |
|---|---|---|
| h264_nvenc | 271 | 270 |
| hevc_nvenc | 370 | **398** |
| av1_nvenc | 354 | **363** |

## LLM (Ollama, 128 tokens, tok/s)

*Model sets differ; not directly comparable.*

| Model | PL8A1V6 | 7TUKMBJ |
|---|---|---|
| qwen2.5-coder:7b | 101.2 | — |
| qwen2.5-coder:14b | 64.1 | — |
| qwen3-coder:30b | 64.6 | — |
| qwen3:8b | — | 98.0 |
| deepseek-coder-v2-tools | — | 215.9 |
| deepseek-coder-v2:16b | — | 220.7 |

## Game-like render (Unigine Valley, 1080p High, Direct3D11)

| Metric | PL8A1V6 | 7TUKMBJ |
|---|---|---|
| Score | **4508** | 3831 |
| Avg FPS | 107.7 | 91.6 |
| Min / Max FPS | 25.8 / 224.6 | 27.7 / 152.3 |

## Extra phases (PL8A1V6 only — new toolkit modules)

**RAM:** copy 21.7 / scale 12.9 / add 13.8 / triad 7.7 GB/s · latency ~15.7 ns/access ·
integrity 5.5 GiB passes 2, 0 bad · 120 s soak 9.3 GB/s, 0 errors.

**Disk — C: (SanDisk SSD PLUS, SATA):** seq 258 / 2016 MB/s; 4K rand r/w/mix
139 297 / 7 042 / 30 744 IOPS; 4K latency p50/p99/p99.9 = 5.4 / 16.7 / 28.9 µs;
10 GiB steady **254 → 124 MB/s (−51 %)**.

**Disk — D: (WD_BLACK SN850X, NVMe — reformatted as one 931 GB NTFS volume):** seq
write/read 1315 / 1654 MB/s; 4K rand read/write/mix 139 113 / **98 765** / **104 890** IOPS;
4K latency p50/p99/p99.9 = 5.0 / 16.1 / 26.7 µs; 10 GiB steady **1251 → 1288 MB/s (−2.9 %)**.
*NVMe is ~5× the SATA SSD's sequential write and ~14× its random 4K write, and it holds SLC
speed across a 10 GiB steady write where the DRAM-less SanDisk drops 51 %.*

**Network:** ping 34 ms; upload 34.5 Mbps; download measured 0 (endpoint returned nothing).

**Full-system soak (GPU+CPU+RAM+disk, 300 s):** GPU 80.4 TFLOPS · CPU 272.4 GFLOPS ·
RAM 6.5 GB/s · disk 154.6 MB/s seq / 59 720 IOPS; GPU 66/71 °C, no TDR/WHEA.

## Read-out

- **GPU compute is a near tie**; PL8A1V6 scales better with CUDA streams, 7TUKMBJ has a
  slight fp16/bf16 edge (clock).
- **PL8A1V6's CPU is 42–78 % faster** (6c/12t vs 4c/8t).
- **Cooling is the real differentiator:** PL8A1V6 holds the soak at ~71 °C, fan ~62 %,
  and **never throttles**; 7TUKMBJ reaches 87 °C, pins the fan at 100 %, and logs
  **~185 s of SW thermal slowdown** in both the soak and the combined soak.

## Findings fixed this session

1. **Valley timing** (`fix/valley-timing-and-ram-info`): the rewritten driver clicked
   "Benchmark" during the engine loading screen, so no score was captured; waiting
   `-EngineSettleSeconds` (default 12) before the click fixes it — **4508** here.
2. **RAM `info` reported `0.0 GiB`** without `psutil`; added a Windows
   `GlobalMemoryStatusEx` fallback (now 31.9 GiB / 26.9 GiB).
