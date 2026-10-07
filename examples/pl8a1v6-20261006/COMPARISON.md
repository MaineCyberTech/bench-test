# bench-test comparison — RTX 4070 Ti SUPER runs

*Generated 2026-10-07. Sources: `examples/example-result.md` (repo baseline) and runs on
`DESKTOP-PL8A1V6` (20261006-085419/093337/101909) and `DESKTOP-7TUKMBJ`
(`examples/7tukmbj-20261007/`). Machine-readable twin: `COMPARISON.json`.*

> Extend by appending a column per host. Keep like-for-like: same power limit, same
> resolution/quality, and note ambient. Expect +/-5-10 % run-to-run (see `docs/METHODOLOGY.md`).

## Hosts

| id | host | CPU | GPU | driver / CUDA | RAM | power limit | notes |
|---|---|---|---|---|---|---|---|
| example | DESKTOP-7TUKMBJ (HP Z4 G4) | Xeon W-2123 (4c/8t) | RTX 4070 Ti SUPER | 617.14 / 13.4 | 64 GB | 285 W | repo reference; custom fan curve |
| run4 | DESKTOP-7TUKMBJ (HP Z4 G4) | Xeon W-2123 (4c/8t) | RTX 4070 Ti SUPER | 617.14 / 12.8 (torch) | 63.7 GB | 285 W | first run with CPU + combined phases |
| run1 | DESKTOP-PL8A1V6 (ASUS) | i7-6800K (6c/12t) | RTX 4070 Ti SUPER | 616.56 / 12.8 | 31.9 GB | 285 W | pre-NVENC |
| run2 | DESKTOP-PL8A1V6 | i7-6800K (6c/12t) | RTX 4070 Ti SUPER | 616.56 / 12.8 | 31.9 GB | 285 W | + NVENC |
| run3 | DESKTOP-PL8A1V6 | i7-6800K (6c/12t) | RTX 4070 Ti SUPER | 616.56 / 12.8 | 31.9 GB | 285 W | + Valley; fixes applied |

## Compute (TFLOPS)

| Metric | example | run4 (7tukmbj) | run1 | run2 | run3 |
|---|---|---|---|---|---|
| matmul fp16 | 89.0 | 90.2 | 89.6 | 89.2 | 89.1 |
| matmul bf16 | 89.3 | 90.3 | 90.3 | 90.2 | 89.9 |
| matmul tf32 | 44.4 | 44.9 | 45.2 | 44.5 | 44.9 |
| matmul fp32 | 25.6 | 26.1 | 27.6 | 27.5 | 27.1 |
| cuDNN conv2d fp16 | 62.5 | 62.8 | 63.1 | 62.8 | 62.8 |
| streams 1 / 2 / 4 / 8 | 84.4 / 86.1 / 85.8 / 85.5 | 89.9 / 89.3 / 88.2 / 87.7 | 90.5 / 90.5 / 90.2 / 90.5 | 90.3 / 90.3 / 90.1 / 90.1 | 90.1 / 90.1 / 89.8 / 89.7 |
| bmm fp16 *(example-only)* | 84.1 | - | - | - | - |
| flash-attn SDPA fp16 *(example-only)* | 52.6 | - | - | - | - |

## Memory & interconnect

| Metric | example | run4 (7tukmbj) | run1 | run2 | run3 |
|---|---|---|---|---|---|
| d2d copy (GB/s) *(example-only)* | 590 | - | - | - | - |
| reduction fp32 / bf16 (GB/s) | 611 | 636 / 633 | 635 / 632 | 634 / 631 | 634 / 632 |
| add fp32 / bf16 (GB/s) | 619 / 613 | 617 / 619 | 617 / 619 | 615 / 617 | 617 / 617 |
| PCIe H2D / D2H (GB/s) | 12.2 / 12.9 | 12.0 / 13.0 | 12.0 / 13.0 | 12.0 / 13.0 | 12.0 / 13.0 |
| VRAM integrity (GiB) | 13 | 13.0 | 13.0 | 13.0 | 13.0 |
| VRAM mismatched / bad | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

## CPU compute (added after run1-3; first measured on run4)

| Metric | run4 (DESKTOP-7TUKMBJ, Xeon W-2123 4c/8t) |
|---|---|
| matmul fp32 (GFLOPS) | 405.5 |
| matmul fp64 (GFLOPS) | 202.3 |
| mem copy / add / reduce (GB/s) | 18 / 14 / 6 |
| soak 120 s (GFLOPS) | 291.7 |

> The i7-6800K (6c/12t) machine has not yet been run through the CPU phase - add a column when it is.

## Combined CPU + GPU stress (added after run1-3)

| Metric | run4 (DESKTOP-7TUKMBJ) |
|---|---|
| Duration | 318.5 s |
| GPU soak | 79.6 TFLOPS |
| CPU soak | 282.8 GFLOPS |
| GPU temp avg / max (°C) | 77.1 / 84 |
| GPU power avg / max (W) | 275.5 / 285.2 |
| GPU fan avg / max (%) | 91.3 / 100 |
| CPU util avg / max (%) | 95.8 / 100 |
| TDR / WHEA | none |

## Endurance (600 s soak @ 285 W)

| Metric | example | run4 (7tukmbj) | run1 | run2 | run3 |
|---|---|---|---|---|---|
| sustained matmul (TFLOPS) | 86.6 | 78.4 | 81.1 | 81.2 | 80.7 |
| measured duration (s) | 600 | 618.6 | 617.9 | 617.9 | 618.1 |
| temp avg / max (°C) | 81 (max) | **85.7 / 87** | 65.9 / 67 | 65.9 / 67 | 71 / 72 |
| power avg / max (W) | 285 | 282.1 / 285 | 277.4 / 281 | 277.6 / 280.8 | 281.5 / 284.9 |
| fan avg / max (%) | 93 | **99.8 / 100** | 47.4 / 49 | 47.5 / 49 | 63.7 / 65 |
| throttle | SW power cap only | SW power capping | ~516 s | ~664 s | ~814 s |
| TDR / WHEA | none | none | none | none | none |

## NVENC (1080p60 transcode)

| Codec | example | run4 (7tukmbj) | run1 | run2 | run3 |
|---|---|---|---|---|---|
| h264_nvenc (fps) | 275 | 270 | - | 270 | 273 |
| hevc_nvenc (fps) | 398 | 398 | - | 373 | 395 |
| av1_nvenc (fps) | 362 | 363 | - | 378 | 344 |

## LLM (Ollama, 128 tokens, tok/s)

| Model | example | run4 (7tukmbj) | run1 | run2 | run3 |
|---|---|---|---|---|---|
| qwen3:8b | 100.6 | 98.0 | - | - | - |
| deepseek-coder-v2-tools:latest | - | 215.9 | - | - | - |
| deepseek-coder-v2:16b | 178-206 | 220.7 | - | - | - |
| qwen2.5-coder:7b | - | - | 120 | 119.2 | 118.2 |
| qwen2.5-coder:14b | - | - | 64.3 | 63.6 | 63.6 |
| qwen3-coder:30b | - | - | 61.7 | 61.5 | 58.8 |

*Model sets differ; only compare like-for-like models.*

## Game-like render (Unigine Valley, 1080p High, Direct3D11)

| Metric | example | run4 (7tukmbj) | run1* | run2 | run3 |
|---|---|---|---|---|---|
| Score | 3471 | 3831 | 4277 | - | 4284 |
| Avg FPS | 83.0 | 91.6 | 102.2 | - | 102.4 |
| Min / Max FPS | - | 27.7 / 152.3 | 5.1 / 213.8 | - | 23.4 / 206.6 |
| GPU util avg / max (%) | ~33 | - | 29 / 59 | - | - |
| temp max (°C) | - | 49 | 58 | - | ~57 |

**Latest on DESKTOP-PL8A1V6** (hardened Valley driver, 2026-10-06 19:36): Score **4527** /
**108.2 FPS** (min 24.7 / max 207.9); spread across four valid runs **4277-4527** (+/-6 %).

run4's Valley score is a **launcher-driven fullscreen re-capture** (2026-10-07,
`screenshot valley-DESKTOP-7TUKMBJ-20261007-score3831.png`): 1920x1080 fullscreen High, Direct3D11.
Both boxes are CPU-bound in this legacy 32-bit engine (GPU util < 70 %, ~90-150 W of 285 W); the
Z4 G4 trails the ASUS box by ~10-16 % (4c/8t vs 6c/12t).
\* run1's Valley ran separately right after run1; run2 had no Valley.

## Takeaways

- **GPU compute, memory and PCIe match across all boxes within ~1-7 %.** Both are RTX 4070 Ti SUPERs.
- **Soak thermals split the two boxes clearly.** `DESKTOP-PL8A1V6` (ASUS) runs **66-72 °C at 47-65 % fan**;
  `DESKTOP-7TUKMBJ` (HP Z4 G4) runs **85.7/87 °C with the fan at 100 %** - i.e. **case-airflow limited**
  at 285 W (the Z4 G4 chassis fans don't ramp). Same silicon, different chassis.
- **CPU: 4c/8t vs 6c/12t.** The new CPU phase quantifies the Xeon W-2123 (405 GFLOPS fp32, 292 GFLOPS soak);
  the i7-6800K column is pending. The CPU gap shows in **Valley: ASUS box 4277-4527 vs Z4 G4 3831**
  (~11-18 % higher on the 6c/12t machine) and drives the combined-stress headroom (this box's CPU is ~96-100 % busy alongside the GPU).
- **NVENC and LLM are in line; AV1 encode has the widest spread (~9 %).**
- **Stability: no TDR / `nvlddmkm` / WHEA / BugCheck on any box or phase.**

## Findings recorded against the toolkit (fixed)

1. `Invoke-ValleyBench.ps1` wrote screenshots to `C:\results` because `$PSScriptRoot` is empty while
   parameter defaults are evaluated when a `[Parameter(Mandatory)]` parameter is present. -> `$OutDir`
   now resolves at runtime.
2. Failed phases (`ffmpeg`/`ollama` missing) were still listed in the run manifest because phase scripts
   used non-terminating `Write-Error`. -> they `throw`, so the runner's `try/catch` fires.
3. `Invoke-ValleyBench.ps1` clicked an absolute `(47,13)`, missing the control when the engine launched
   windowed. -> click is now relative to the engine window client area, **and double-clicked** (a single
   click only focuses the window). Capture is now a **series of timed frames** so the result panel is
   never missed when the engine runs windowed.
