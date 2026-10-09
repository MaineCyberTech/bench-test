# DESKTOP-PL8A1V6 — RTX 4070 Ti SUPER full run (2026-10-08)

The RTX 4070 Ti SUPER was re-installed (replacing the faulty GTX 1080), the driver was updated to
**617.42**, and the custom GPU fan curve (`nvfancontrol`) was left active. Full battery run.

## Host

| | |
|---|---|
| Board | ASUS X99-A II (BIOS 2101) |
| CPU | Intel i7-6800K (6c/12t) @ 3.40 GHz |
| RAM | 32 GiB (4×8 @ 2667) |
| GPU | NVIDIA GeForce RTX 4070 Ti SUPER (16 GB, sm_89), driver **617.42** |
| CUDA / torch | CUDA 12.8, torch **2.11.0+cu128** |
| Fan curve | `nvfancontrol` `40:55 → 70:100` (persistent task) |

## GPU compute

| Phase | Result |
|---|---|
| matmul fp16 / bf16 | 87.7 / 88.3 TFLOPS |
| matmul tf32 / fp32 | 44.4 / 27.2 TFLOPS |
| cuDNN conv2d fp16 | 61.7 TFLOPS |
| membw add / reduce (fp32) | 604 / 619 GB/s |
| membw add / reduce (bf16) | 605 / 618 GB/s |
| PCIe H2D / D2H | 12.0 / 13.0 GB/s |
| streams 1/2/4/8 | 88.7 / 88.6 / 88.6 / 88.6 TFLOPS |
| VRAM integrity (13 GiB) | 0 mismatched / 0 bad |
| **600 s soak** | **79.9 TFLOPS · 58/60 °C · 274/277 W · fan 81/83 % · no throttle** |

## CPU / combined / NVENC / LLM / Valley

| Test | Result |
|---|---|
| CPU matmul fp32 / fp64 | 436.3 / 206.9 GFLOPS |
| CPU soak (120 s) | 367.8 GFLOPS |
| Combined GPU+CPU (300 s) | 80.2 TFLOPS + 361.7 GFLOPS · 58/60 °C |
| NVENC h264 / hevc / av1 | 273 / 401 / 380 fps |
| LLM qwen2.5-coder 7b / 14b · qwen3-coder 30b | 78.1 / 52.4 / 32.3 tok/s |
| Unigine Valley (1080p High) | **4319 · 103.2 FPS** |

## Full-system soak (300 s — GPU+CPU+RAM+disk concurrently)

GPU 80.0 TFLOPS · CPU 221.8 GFLOPS · RAM 6.0 GB/s (0 errors) · disk seq 168.8 MB/s / 50 769 IOPS;
GPU 59/60 °C, fan 80/84 %, no TDR/WHEA/BugCheck.

## Notes

- **Thermals:** the custom fan curve keeps the 600 s soak at a chilly **58–60 °C** (vs ~71 °C on the
  stock curve in earlier sessions) with no throttling.
- **Phase 11 (full-system soak) died in the battery run** (no crash/power event/reboot — the job just
  ended). It was **re-run separately and completed** — see `DESKTOP-PL8A1V6-fullsoak-20261008-165336.*`.
- **CPU and LLM figures came out lower than earlier sessions** (CPU soak 367.8 vs 463–506 GFLOPS;
  LLM 7b 78.1 vs ~119 tok/s) — likely background load / the newer driver. Worth a clean re-check.
- No `manifest.json`: the battery died before it writes the summary manifest.

## Artifacts

- `DESKTOP-PL8A1V6-20261008-150242.{md,json}` — GPU compute + soak
- `DESKTOP-PL8A1V6-cpu-20261008-152857.{md,json}` — CPU
- `DESKTOP-PL8A1V6-combined-20261008-153258.{md,json}` — combined CPU+GPU
- `DESKTOP-PL8A1V6-ram-20261008-154845.{md,json}` — RAM
- `DESKTOP-PL8A1V6-disk-20261008-155203.{md,json}` — disk (C:)
- `DESKTOP-PL8A1V6-net-20261008-155726.{md,json}` — network
- `DESKTOP-PL8A1V6-20261008-150139.{info,llm,nvenc}.json` — inventory / LLM / NVENC
- `valley-DESKTOP-PL8A1V6-20261008-4070-score4319.png` — Valley result dialog
- `DESKTOP-PL8A1V6-fullsoak-20261008-165336.{md,json}` — full-system soak (re-run)
