# bench-test comparison — RTX 4070 Ti SUPER runs

*Generated 2026-10-06. Sources: `examples/example-result.md` (repo baseline) and runs
`20261006-085419` (run 1), `20261006-093337` (run 2), `20261006-101909` (run 3) on
`DESKTOP-PL8A1V6`. Machine-readable twin: `COMPARISON.json`.*

> Extend by appending a column per host. Keep like-for-like: same power limit, same
> resolution/quality, and note ambient. Expect ±5–10 % run-to-run (see `docs/METHODOLOGY.md`).

## Hosts

| id | host | CPU | GPU | driver / CUDA | power limit | notes |
|---|---|---|---|---|---|---|
| example | DESKTOP-7TUKMBJ (HP Z4 G4) | Xeon W-2123 (4c/8t) | RTX 4070 Ti SUPER | 617.14 / 13.4 | 285 W | repo reference; custom fan curve |
| run1 | DESKTOP-PL8A1V6 (ASUS) | i7-6800K (6c/12t) | RTX 4070 Ti SUPER | 616.56 / 12.8 (torch) | 285 W | pre-NVENC |
| run2 | DESKTOP-PL8A1V6 | i7-6800K (6c/12t) | RTX 4070 Ti SUPER | 616.56 / 12.8 | 285 W | + NVENC |
| run3 | DESKTOP-PL8A1V6 | i7-6800K (6c/12t) | RTX 4070 Ti SUPER | 616.56 / 12.8 | 285 W | + Valley; fixes applied |

## Compute (TFLOPS)

| Metric | example | run1 | run2 | run3 |
|---|---|---|---|---|
| matmul fp16 | 89.0 | 89.6 | 89.2 | 89.1 |
| matmul bf16 | 89.3 | 90.3 | 90.2 | 89.9 |
| matmul tf32 | 44.4 | 45.2 | 44.5 | 44.9 |
| matmul fp32 | 25.6 | 27.6 | 27.5 | 27.1 |
| cuDNN conv2d fp16 | 62.5 | 63.1 | 62.8 | 62.8 |
| streams 1 | 84.4 | 90.5 | 90.3 | 90.1 |
| streams 2 | 86.1 | 90.5 | 90.3 | 90.1 |
| streams 4 | 85.8 | 90.2 | 90.1 | 89.8 |
| streams 8 | 85.5 | 90.5 | 90.1 | 89.7 |
| bmm fp16 *(example-only phase)* | 84.1 | — | — | — |
| flash-attn SDPA fp16 *(example-only)* | 52.6 | — | — | — |

## Memory & interconnect

| Metric | example | run1 | run2 | run3 |
|---|---|---|---|---|
| d2d copy (GB/s) *(example-only)* | 590 | — | — | — |
| reduction (GB/s) | 611 | 635 fp32 / 632 bf16 | 634 / 631 | 634 / 632 |
| add fp32 (GB/s) | 619 | 617 | 615 | 617 |
| add bf16 (GB/s) | 613 | 619 | 617 | 617 |
| PCIe H2D (GB/s) | 12.2 | 12.0 | 12.0 | 12.0 |
| PCIe D2H (GB/s) | 12.9 | 13.0 | 13.0 | 13.0 |
| VRAM integrity (GiB) | 13 | 13.0 | 13.0 | 13.0 |
| VRAM mismatched / bad | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

## Endurance (600 s soak @ 285 W)

| Metric | example | run1 | run2 | run3 |
|---|---|---|---|---|
| sustained matmul (TFLOPS) | 86.6 | 81.1 | 81.2 | 80.7 |
| measured duration (s) | 600 | 617.9 | 617.9 | 618.1 |
| temp avg / max (°C) | 81 (max) | 65.9 / 67 | 65.9 / 67 | 71 / 72 |
| power avg / max (W) | 285 | 277.4 / 281 | 277.6 / 280.8 | 281.5 / 284.9 |
| fan avg / max (%) | 93 | 47.4 / 49 | 47.5 / 49 | 63.7 / 65 |
| throttle | SW power cap only | SW power capping (~516 s) | ~664 s | ~814 s |
| TDR / WHEA | none | none | none | none |

## NVENC (1080p60 transcode)

| Codec | example | run1 | run2 | run3 |
|---|---|---|---|---|
| h264_nvenc (fps) | 275 | — | 270 | 273 |
| hevc_nvenc (fps) | 398 | — | 373 | 395 |
| av1_nvenc (fps) | 362 | — | 378 | 344 |

## LLM (Ollama, 128 tokens, tok/s)

| Model | example | run1 | run2 | run3 |
|---|---|---|---|---|
| qwen2.5-coder:7b | — | 120 | 119.2 | 118.2 |
| qwen2.5-coder:14b | — | 64.3 | 63.6 | 63.6 |
| qwen3-coder:30b | — | 61.7 | 61.5 | 58.8 |
| qwen3:8b | 100.6 | — | — | — |
| deepseek-coder-v2:16b | 178–206 | — | — | — |

*Model sets differ; only compare like-for-like models.*

## Game-like render (Unigine Valley, 1080p High, Direct3D11)

| Metric | example | run1* | run2 | run3 |
|---|---|---|---|---|
| Score | 3471 | 4277 | — | 4284 |
| Avg FPS | 83.0 | 102.2 | — | 102.4 |
| Min / Max FPS | — | 5.1 / 213.8 | — | 23.4 / 206.6 |
| GPU util avg / max (%) | ~33 | 29 / 59 | — | — |
| temp max (°C) | — | 58 | — | ~57 |
| power max (W) | — | 89 | — | — |

**Latest run** (hardened Valley driver, 2026-10-06 19:36): Score **4527** · **108.2 FPS**
(min 24.7 / max 207.9). Observed spread on this box across four valid runs: **4277–4527** (±6 %).

\* run1's Valley was run separately right after run1 (screenshot `valley-DESKTOP-PL8A1V6-20261006-100731.png`);
run3's was inline with the battery. run2 had no Valley. Valley is CPU-bound on both boxes.

## Takeaways

- **Compute, memory and PCIe match** across all four within ~1–7 %. The `DESKTOP-PL8A1V6` box is
  slightly *higher* on multi-stream scaling and memory bandwidth.
- **Soak is ~7 % lower** on this box (80.7–81.2 vs 86.6 TFLOPS) but runs **10–14 °C cooler** at far
  lower fan — not thermally limited. Difference is within the documented run-to-run band.
- **Valley +23–30 %** on this box (4277–4527 vs 3471) — a CPU effect: i7-6800K (6c/12t) vs Xeon
  W-2123 (4c/8t). Still engine/CPU-bound (89 W of 285 W, GPU util < 60 %).
- **NVENC and LLM are in line**; AV1 encode has the widest spread (~9 %).
- **Stability: no TDR / `nvlddmkm` / WHEA / BugCheck** on any run.

## Findings recorded against the toolkit (fixed in run3)

1. `Invoke-ValleyBench.ps1` wrote its screenshot to `C:\results` because `$PSScriptRoot` is empty
   while parameter defaults are evaluated when a `[Parameter(Mandatory)]` parameter is present.
2. Failed phases (`ffmpeg`/`ollama` missing) were still listed in the run manifest because the
   phase scripts used non-terminating `Write-Error`, so the runner's `try/catch` never fired.
3. `Invoke-ValleyBench.ps1` clicked an absolute `(47,13)` coordinate, so when the engine launched
   *windowed* (busy desktop) the click missed the Benchmark control and no score was captured. Now
   clicked relative to the engine window's client area (fullscreen or windowed).
