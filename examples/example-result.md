# Example result — DESKTOP-7TUKMBJ (RTX 4070 Ti SUPER / Xeon W-2123)

*Illustrative output of the toolkit on a real machine. Your numbers will differ.*

**Host:** HP Z4 G4 Workstation · Xeon W-2123 (4c/8t) · 64 GB RAM · Windows 11 Pro (build 26200)
**GPU:** NVIDIA GeForce RTX 4070 Ti SUPER (AD103) · driver 617.14 · CUDA 13.4 · 16 376 MiB · 66 SMs
**Power limit:** 285 W · **GPU fan:** custom curve (nvfancontrol)

## Compute
| Phase | Metric |
|---|---|
| matmul fp16 / bf16 | **89.0 / 89.3 TFLOPS** |
| matmul tf32 / fp32 | 44.4 / 25.6 TFLOPS |
| cuDNN conv2d fp16 | 62.5 TFLOPS |
| bmm fp16 | 84.1 TFLOPS |
| flash-attention (SDPA) fp16 | 52.6 TFLOPS |
| streams 1 / 2 / 4 / 8 | 84.4 / 86.1 / 85.8 / 85.5 TFLOPS |

## Memory / interconnect
| Phase | Metric |
|---|---|
| d2d copy / reduction | 590 / 611 GB/s |
| add (fp32/bf16) | 619 / 613 GB/s |
| PCIe H2D / D2H | 12.2 / 12.9 GB/s |
| VRAM integrity (~13 GiB) | 0 mismatches · 0 bad elements |

## Endurance (see docs/METHODOLOGY.md for drift interpretation)
| Run | Throughput | Temp | Power | Fan | Throttle |
|---|---|---|---|---|---|
| 600 s soak @285 W | 86.6 TFLOPS | 81 °C | 285 W | 93 % | SW power cap only |
| 30 min soak @220 W | 72.7 TFLOPS | 82 °C | 220 W | 85 % | none |
| 30 min soak @285 W | 74.8 TFLOPS | **90 °C** | 285 W | 89 % | **SW thermal slowdown ~26 min** |

> At 285 W the mixed soak thermally throttled; +14 °C drift with flat power ⇒ **case airflow**
> was the limit (Z4 G4 front/PCIe fans don't ramp). 220 W was the sweet spot (~97 % throughput, cooler).

## LLM (Ollama)
| Model | tok/s |
|---|---|
| qwen3:8b | 100.6 |
| deepseek-coder-v2:16b | 178–206 |

## NVENC (1080p60 transcode)
| Codec | fps |
|---|---|
| h264_nvenc | 275 |
| hevc_nvenc | 398 |
| av1_nvenc | 362 |
| 4× concurrent h264 | encoder util 100 % |

## Game-like render (Unigine Valley, Direct3D11)
| Preset | Score | Avg FPS | GPU util |
|---|---|---|---|
| High (1080p) | 3471 | 83.0 | ~33 % |
| Extreme HD (1080p 8×AA) | 3533 | 84.4 | ~39 % |

> CPU-bound (Xeon W-2123); GPU not the limit at 1080p in this legacy 32-bit engine.

## Stability
No TDR (`4101`), no `nvlddmkm`, no WHEA, no BugCheck across ~35 min of load.
