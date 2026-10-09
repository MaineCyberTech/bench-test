# bench-test live USB

A bootable Arch Linux ISO that carries the toolkit, so any machine can be tested
**without installing anything**. It boots to a console; the toolkit lives at
`/opt/bench-test`, and results are collected to the **BENCHDATA** partition on the
same USB stick.

## Build

```bash
sudo ./live/Build-LiveUSB.sh --gpu nvidia            # Turing+ (nvidia-open 610)
sudo ./live/Build-LiveUSB.sh --gpu nvidia-legacy     # Maxwell/Pascal/Volta (+up), 580 branch
sudo ./live/Build-LiveUSB.sh --gpu amd               # Mesa/RADV + ROCm PyTorch
```

Then write it (destructive) — or build with `--write /dev/sdX` directly:

```bash
sudo dd if=live/out/bench-live-*.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

`Build-LiveUSB.sh --write` also creates a `BENCHDATA` exFAT/FAT partition in the
remaining space for results.

Requirements: 16 GB+ USB (the nvidia/amd images are ~7 GB, the base system alone
~2.5 GB), network during build, ~25 GiB free disk for the work dir (the build
refuses tmpfs paths - /tmp on many systems is RAM).

## On the target machine

Boot the stick — the **TUI dashboard** auto-starts on the console (`bench-tui`,
htop/nvtop-style: live bars + sparklines, phase table, log, one-key start):

```
bench-tui                      # htop-style console dashboard (auto-starts on tty1)
bench-gui                      # graphical dashboard (X + mouse), same features
bench-live-run                 # plain console run, no dashboard
bench-live-run --with-gfx      # + FurMark / vkmark / glmark2 under X
```

TUI keys: `s` start · `x` stop · `c` collect · `f` fan-curve · `p` cycle profile ·
`1-0` toggle groups · `r` report viewer · `G` graphical dashboard · `t` shell ·
`PgUp/PgDn` log · `q` quit.

### GPU fan curves

`bench-fan-curve` sets a curve or tests fan response:

```
bench-fan-curve status                    # vendor, temp, fan %/RPM, saved curve
bench-fan-curve set --curve "40:40 55:55 70:75 80:95"
bench-fan-curve check --seconds 90        # ramp test: verdict ok / no-fan-response / thermal-limit
bench-fan-curve off                       # restore automatic control
```

NVIDIA uses `nvidia-settings` (needs the X session — the dashboard can start it),
AMD uses the amdgpu sysfs `pwm1` interface. The active curve is saved in
`/etc/bench-fan-curve.conf` so run reports record which cooling profile was in
effect (mirrors the repo's Windows `nvfancontrol` curve runs, e.g. the GTX 1080
fan-profile comparison in `examples/`).

Both dashboards show the detected hardware, run presets (quick / full /
graphics-only / CPU-RAM-disk), per-group toggles, live phase progress and 1 Hz
telemetry (temp / fan / power / util), display the final report, and collect
results to `BENCHDATA` automatically when the run ends.

`bench-live-run` passes any `Run-LinuxBench.sh` flags through, runs the battery in
RAM, and copies `results/` (report, 1 Hz telemetry CSV, per-phase logs, run index)
to `BENCHDATA/<host>-<timestamp>/` when it finishes. Offline machines work fine —
the collection is local; upload/sync the `BENCHDATA` contents later.

## Variants / vendor notes

| Variant | GPU support | Compute | Notes |
|---|---|---|---|
| `nvidia` | Turing → RTX 50 (`nvidia-open` 610) | PyTorch CUDA | default for modern NVIDIA |
| `nvidia-legacy` | Maxwell/Pascal/Volta (+ Turing..Ada) via 580 branch | PyTorch CUDA | uses the omarchy repo; the DKMS module is compiled into the ISO at build time (a boot service rebuilds it if needed) |
| `amd` | GCN+ for graphics (Mesa/RADV) | PyTorch ROCm | ROCm compute only on supported cards (RDNA+); older cards still run graphics/telemetry and skip compute |

The GPU phases call `nvidia-smi` or `rocm-smi`/amdgpu sysfs through
`bench/gpu_util.py`; on unsupported hardware they record a skip instead of failing.

## Layout

```
live/Build-LiveUSB.sh         build + write tool
live/packages.x86_64          common packages (appended to the archiso releng list)
live/packages-{nvidia,nvidia-legacy,amd}.x86_64
live/airootfs/                overlay: bench-tui, bench-gui, bench-live-run, bench-collect, motd, dkms unit
```
