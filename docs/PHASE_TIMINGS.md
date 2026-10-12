# Phase timings — measured

Per-phase wall-clock durations measured on a reference workstation, to estimate
how long a run/preset takes. Numbers are from a **single full run with short
soaks** (gpu 30 s · combined 30 s · fullsoak 30 s · gfx 30 s), all 12 groups.

**Reference machine:** `DESKTOP-PL8A1V6` — Intel Core i7-6800K, 32 GiB, NVIDIA
RTX 4070 Ti SUPER (16 GB, driver 617.42), Windows 11. Total: **1803 s (~30 min)**.

## Measured per-phase durations

| Group | Phase | s |
|---|---|---|
| `sys` | system + GPU inventory + sensors | **71** |
| `gpu` | info | 3 |
| `gpu` | matmul (fp16/bf16/tf32/fp32) | 45 |
| `gpu` | membw | 36 |
| `gpu` | pcie | 20 |
| `gpu` | conv | 18 |
| `gpu` | integrity (fill/verify ~13 GiB VRAM) | 4 |
| `gpu` | **streams (1/2/4/8)** | **357** |
| `gpu` | soak (30 s) | 53 |
| `cpu` | CPU compute battery (matmul/membw/hash/compress/aes/flops + 120 s soak) | **254** |
| `combined` | GPU + CPU concurrent (30 s soak) | 157 |
| `nvenc` | ffmpeg h264/hevc/av1 | 98 |
| `gfx` | Unigine Valley | 91 |
| `ram` | bandwidth / latency / integrity + 120 s soak | **195** |
| `disk` | seq/rand + latency + 10 GiB steady write | **295** |
| `net` | ping + download/upload | 28 |
| `fullsoak` | GPU+CPU+RAM+disk concurrent (30 s soak) | 70 |

Long poles (fixed cost regardless of soak): **gpu streams ≈ 6 min**, **disk ≈ 5 min**,
**cpu ≈ 4 min**, **ram ≈ 3 min**, **sys ≈ 1 min**.

## Model

```
gpu_group    = 486 + gpu_soak + 23        # compute is fixed; soak adds ~1:1
combined     = combined_seconds + 127
fullsoak     = fullsoak_seconds + 40
gfx          = 91                          # Valley, fixed
cpu, ram, disk, nvenc, net, sys, llm, fan  # fixed (see above)
```

## Estimated preset runtimes

| Preset | Estimate |
|---|---|
| System info + 60s soak (all) | ~3 min |
| Fan response | ~2.5 min (Linux) |
| LLM inference (Ollama) | ~2–3 min |
| Video / graphics | ~4.5 min |
| Storage + network | ~6.5 min |
| GPU compute (gpu 60) | ~11 min |
| CPU + RAM + disk | ~14 min |
| Quick check (short soaks) | **~31 min** |
| Full run (defaults) | **~48 min** |
| Full-system soak (long) | ~32 min |
| Soak only (long) | ~70 min |

> **Quick check is ~31 min** despite "short soaks" — the fixed GPU streams, disk,
> CPU and RAM phases dominate, not the soaks. For a genuinely quick check use
> *System info + 30s soak* or *GPU compute*.

## Caveats

- One machine, one run; treat as indicative. Faster/slower disks, GPUs and CPUs
  shift the fixed phases (disk especially).
- `gpu streams` (~6 min here) and `disk steadywrite` scale with the hardware and
  the configured size (10 GiB steady write, 2 GiB seq write).
- CPU/RAM soak lengths are fixed in the runner (`-CpuSoakSeconds 120`,
  `-RamSoakSeconds 120`); the gpu/combined/fullsoak/gfx lengths follow the preset.
- On the live USB the `disk` group is skipped unless a real internal volume is
  mounted (see `docs/CONTROL_PLANE.md` / `Run-LinuxBench.sh`), and `fan` replaces
  the Windows `llm`/`nvenc` cost.

Regenerate after a hardware/driver change by running the battery with short soaks
and reading `/api/v1/runs/<id>` (phase `t0`/`t1`).
