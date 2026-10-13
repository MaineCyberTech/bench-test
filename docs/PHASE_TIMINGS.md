# Phase timings — measured

Per-phase wall-clock durations, to estimate how long a run/preset takes and to see
which phases dominate. Numbers come from real runs of the **Stabilized full soak
(15 min)** preset (gpu 180 s ceiling · combined 45 s · fullsoak 90 s · gfx 30 s ·
cpu soak 45 s · ram soak 30 s · disk 30 s / 512 MB · gpu matrix 4096 · cpu 2048 ·
ram 512 MiB · 1 pass · 2 GB steady write · soak-until-stable).

## Two reference machines, same preset

| Machine | OS | Total | Notes |
|---|---|---|---|
| `bench-live` (live USB) — i7-3770, 16 GiB, GTX 1080 | Arch live | **~14.1 min** (845 s) | `disk` skipped (no internal volume mounted) |
| `DESKTOP-PL8A1V6` — i7-6800K, 32 GiB, RTX 4070 Ti SUPER | Windows 11 | **~26 min** (1561 s) | slow system disk dominates |

The same preset is comfortably inside the 15-minute target on the live stick and
**over it on this desktop** — the fixed phases, not the soaks, are the difference.
Trim with the preset's `tune` knobs (below) or drop groups.

> **Live-stick total.** The measured stabilized-preset total on `bench-live` is
> **845 s (~14.1 min)**, consistent with the 843 s (before the disk-size trim) and
> 853 s (after) recorded below — the live stick runs no disk phases, so the trim
> changes nothing there. An earlier **~11.5 min** figure for the same preset was an
> estimate that disagrees with these measurements; the measured total supersedes
> it.

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
| **Stabilized full soak (15 min)** | **~14.1 min** (845 s, live stick) · ~26 min (old desktop, slow disk) |
| Quick check (short soaks) | ~14 min — it carries lean knobs (cpu 60, ram 30, disk 15 s / 256 MB, matrix 4096); without them it inherits the runner's long defaults and took ~31 min |
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

Without a `tune` a run falls back to the runner's long defaults (cpu 120 s, ram
120 s, gpu matrix 12288, 10 GiB steady write, disk 2048 MB). "Quick check" and
"Stabilized full soak (15 min)" do carry the lean knobs above — it is the presets
without a `tune` that inherit the long defaults.

### Disk size is the disk lever, not disk seconds

The `randread` / `randwrite` / `randmix` phases are **size-bound**: at the runner's
2048 MB default they took 41 + 75 + 110 = **226 s** of the desktop's run, and
raising `disk_seconds` does not change that. The presets now carry `disk_size_mb`
512 (256 for Quick check), which does the same job in ~57 s. This was the single
biggest safe saving on the slow desktop.

**Where this does and does not show up.** The live USB runs **no disk phases** (no
real volume mounted at `/mnt/internal`), so the trim changes nothing there: its
stabilized run measured **843 s before the trim and 853 s after** — noise, with the
disk group absent from the phase list entirely. The ~170 s saving applies to
machines that actually run the disk group; the 226 s figure above is measured, the
~57 s is the arithmetic on the same phase set at 512 MB, and a desktop re-measure
is still outstanding (that machine was offline). Do not quote the desktop's post-trim
total as measured until it is.

**Shared test file (new).** `Invoke-DiskBench.ps1` now forwards `--keep`, so the
`seqread` / `latency` (and random) phases read the file `seqwrite` created instead
of each re-creating 2 GiB first. Wall-clock disk figures measured before this
(e.g. the desktop's 289 s disk group) include that untimed re-creation and are
upper bounds now; the per-phase `seconds` fields were always measured work only.

## Telemetry cadence (per stat, measured)

A row used to call every stat, including three subprocesses, so an iteration cost
~0.3 s and asking for 4 Hz delivered **2.9 rows/s** — the `sleep` was never the
limit. Costs measured on a Windows box (nvidia-smi is the same command on Linux):

| Stat | How | Cost | Cadence |
|---|---|---|---|
| GPU util/temp/fan/power/clocks/VRAM/pstate | `nvidia-smi` | **66 ms/call** | 1 Hz (`--gpu-interval 1.0`) |
| CPU package/core temps | `sensors -j` | subprocess | 1 Hz (`--slow-interval 1.0`) |
| GPU fan RPM | `nvidia-settings` | subprocess | 1 Hz |
| CPU %/RAM/swap | `psutil`, `GetSystemTimes`, `/proc` | 0.1–3.6 ms | the row rate |
| disk/net counters | `psutil`, `/proc/diskstats`, `/proc/net/dev` | 0.1 ms | the row rate |
| core clock | cpufreq sysfs, `/proc/cpuinfo` | 0.1 ms | the row rate |
| RAPL package power, PSI I/O | `/proc` | ~0.1 ms | the row rate |

Temps have seconds of thermal mass and the GPU query carries util+temp+power in one
call, so 1 Hz keeps the traces smooth. The A/B (same box, same sampler):

| row interval | before | after |
|---|---|---|
| 0.25 s (4 Hz) | 2.9 rows/s | **4.0 rows/s** |
| 0.10 s | 3.4 rows/s | 9.1 rows/s |
| 0.05 s | 3.3 rows/s | 15.6 rows/s |

`elapsed_s` is now written with 2 decimals so rows stay distinct above 10 Hz.

### The soak's TFLOPS depends on the matrix size

`gpu_soak_tflops` is `2·N³·matmuls / seconds`, so it is **only comparable between
runs that used the same `gpu_size`**. The same RTX 4070 Ti SUPER reported **81.1**
at the runner's default (N=10240) and **32.7** with the stabilized preset's
`gpu_size: 4096` — not a regression:

| run | N | matmuls/s | `gpu_soak_tflops` | `gpu_soak_total_tflops` |
|---|---|---|---|---|
| default size | 10240 | 37.7 | 81.1 | **86.9** (conv = 7% of the work) |
| stabilized preset | 4096 | **237.9** | 32.7 | **69.5** (conv = 53%) |

The card did **6× more matmuls per second** at the smaller size; it simply reaches a
lower fraction of peak. The soak loop also runs a conv2d per matmul, and the
matmul-only figure omits it — which matters far more at 4096, where the conv is
about half the work. So the summary now carries `gpu_soak_size` and
`gpu_soak_total_tflops` alongside `gpu_soak_tflops`: read the size before comparing
two runs, and prefer the total when the sizes differ.

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
- Telemetry rows are written at 4 Hz (`--interval 0.25`), with the GPU query and
  the `sensors`/`nvidia-settings` stats at 1 Hz; a 12-minute run is
  ~2900 rows. Each row is stored once — the end-of-run bulk upload skips what the
  live stream already sent.

Regenerate after a hardware/driver change by running the preset and reading
`/api/v1/runs/<id>` (per-phase `t0`/`t1`).
