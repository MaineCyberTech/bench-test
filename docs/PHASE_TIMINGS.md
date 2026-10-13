# Phase timings — measured

Per-phase wall-clock durations, to estimate how long a run/preset takes and to see
which phases dominate. Numbers come from real runs of the **Stabilized full soak
(15 min)** preset (gpu 180 s ceiling · combined 45 s · fullsoak 90 s · gfx 30 s ·
cpu soak 45 s · ram soak 30 s · disk 30 s · gpu matrix 4096 · cpu 2048 · ram
512 MiB · 1 pass · 2 GB steady write · soak-until-stable).

## Two reference machines, same preset

| Machine | OS | Total | Notes |
|---|---|---|---|
| `bench-live` (live USB) — i7-3770, 16 GiB, GTX 1080 | Arch live | **~11.5 min** (691 s) | `disk` skipped (no internal volume mounted) |
| `DESKTOP-PL8A1V6` — i7-6800K, 32 GiB, RTX 4070 Ti SUPER | Windows 11 | **~26 min** (1561 s) | slow system disk dominates |

The same preset is comfortably inside the 15-minute target on the live stick and
**over it on this desktop** — the fixed phases, not the soaks, are the difference.
Trim with the preset's `tune` knobs (below) or drop groups.

## Measured per-phase durations

### Live stick (GTX 1080, Linux)

| Group | Phase | s |
|---|---|---|
| `sys` | inventory + sensors | ~60 |
| `gpu` | info / matmul / membw / pcie / conv / integrity / streams | 4 / 55 / 36 / 21 / 19 / 4 / 4 |
| `gpu` | soak (plateau + 60 s hold) | 54 |
| `cpu` | matmul / membw / hash / compress / aes / flops | 21 / 25 / 8 / 36 / 8 / 9 |
| `cpu` | soak (45 s) | 45 |
| `ram` | bandwidth / latency / integrity | 41 / 1 / 3 |
| `ram` | soak (30 s) | 30 |
| `net` | bench | 11 |
| `gfx` | FurMark / glmark2 | 8 (FurMark failed) |
| `fan` | fan-response check (90 s) | 96 |
| `combined` | GPU+CPU (45 s) | 53 |
| `fullsoak` | GPU+CPU+RAM+disk (90 s) | 95 |

### Desktop (RTX 4070 Ti SUPER, Windows)

| Group | Phase | s |
|---|---|---|
| `sys` | inventory + sensors | 65 |
| `gpu` | whole group at matrix 4096 (matmul 64 · membw 51 · pcie 29 · conv 16 · streams 76 · integrity 4 · info 3) | **379** |
| `cpu` | whole group at size 2048 (compress 62 · membw 26 · matmul 17 · flops 10 · hash 9 · aes 8 · soak 45) | 176 |
| `combined` | GPU+CPU (45 s) | 68 |
| `nvenc` | h264/hevc/av1 | 95 |
| `gfx` | Unigine Valley (fixed preset) | 91 |
| `ram` | 512 MiB / 1 pass / 30 s soak | 95 |
| `disk` | 2 GiB seq + rand + 2 GB steady | **289** |
| `net` | ping + download/upload | 27 |
| `fullsoak` | GPU+CPU+RAM+disk (90 s) | 159 |

Long poles (fixed cost, regardless of soak): **disk ≈ 5 min** (desktop),
**gpu sub-phases ≈ 4 min**, **cpu ≈ 2 min**, **fullsoak ≈ 1–2 min**.

## Model

```
gpu group  = ~240 (fixed sub-phases at matrix 4096) + soak
combined   = combined_seconds + ~25
fullsoak   = fullsoak_seconds + ~10..70   (depends on the disk phase inside it)
gfx        = 25 + gfx_seconds             (FurMark 2; Valley is ~90s, fixed)
cpu, ram, disk, nvenc, net, sys, fan      # fixed-ish, see tables above
```

`soak-until-stable` stops after `stable_hold` seconds of plateau (default 60) and
never before 45 s, so the *ceiling* (`gpu soak`) is what bounds it — 180 s in this
preset. Add the hold to the plateau time when budgeting.

## Estimated preset runtimes

| Preset | Estimate |
|---|---|
| System info + 60s soak (all) | ~3 min |
| Fan response | ~2.5 min (Linux) |
| LLM inference (Ollama) | ~2–3 min |
| **Graphics only (FurMark/Valley)** | **~1.5 min** (FurMark) / ~3 min (Valley) |
| Video / graphics | ~4.5 min |
| Storage + network | ~6.5 min |
| GPU compute (gpu 60) | ~11 min |
| CPU + RAM + disk | ~14 min |
| **Stabilized full soak (15 min)** | **~11.5 min** (live stick) · ~26 min (old desktop, slow disk) |
| Quick check (short soaks) | ~31 min (uses the runner's long defaults for cpu/ram) |
| Full run (defaults) | ~48 min |
| Full-system soak (long) | ~32 min |
| Soak only (long) | ~70 min |

## Trimming a run

The preset carries these in its `tune` field; the web start dialog exposes them,
and both runners accept the matching flags.

| Knob | Flag | Effect |
|---|---|---|
| `gpu_size` | `--gpu-size` | matrix N for every GPU sub-phase (biggest lever; cubic) |
| `cpu_size` | `--cpu-size` | CPU matrix size |
| `ram_size_mb` / `passes` | `--ram-size-mb` / `--passes` | RAM phases |
| `cpu_soak` / `ram_soak` | `--cpu-soak` / `--ram-soak` | the CPU/RAM soaks |
| `disk_seconds` / `disk_size_mb` / `steady_gb` | `--disk-seconds` / `--disk-size-mb` / `--steady-gb` | the disk phases |
| `stable` / `stable_hold` | `--soak-until-stable` / `--stable-hold` | plateau soak and its hold |
| `gfx_engine` | `--gfx-engine` | `furmark` (default) or `valley` |

Without these a run falls back to the runner's long defaults (cpu 120 s, ram
120 s, gpu matrix 12288, 10 GiB steady write) — that is why "Quick check" is not
quick.

## Caveats

- Two machines, one run each; treat as indicative. Faster/slower disks, GPUs and
  CPUs shift the fixed phases (the desktop's disk phase alone is ~5 min).
- `disk` on the **live USB** is skipped unless a real internal volume is mounted at
  `/mnt/internal` (never the USB stick or RAM); `fan` replaces the Windows
  `llm`/`nvenc` cost there.
- `gfx`: FurMark 2 is the default engine. On a display with **no monitor**
  (`Monitors: 0`, e.g. a headless live box) FurMark's X probe segfaults and the
  phase falls back to a bounded glmark2 scene subset; on Windows it falls back to
  Valley only if FurMark is not installed.
- Telemetry is sampled at 2 Hz; a 12-minute run is ~1450 rows.

Regenerate after a hardware/driver change by running the preset and reading
`/api/v1/runs/<id>` (per-phase `t0`/`t1`).
