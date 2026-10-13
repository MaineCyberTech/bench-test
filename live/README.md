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
sudo dd if=live/out-nvidia-legacy/bench-live-*.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

Note: the `nvidia-legacy` image covers **every NVIDIA card from Maxwell
through Ada** (GTX 9xx/10xx and RTX 20/30/40 series) - the 580 driver's GPU
table includes e.g. the RTX 4070 Ti SUPER. One stick for all of those. The
`nvidia` variant (open kernel modules, 610) is only needed for Blackwell
(RTX 50).

Each variant builds into its own `live/out-<variant>/` directory so they can
coexist. `Build-LiveUSB.sh --write` also creates a `BENCHDATA` FAT partition in
the remaining space for results.

Requirements: 16 GB+ USB (the nvidia/amd images are ~7 GB, the base system alone
~2.5 GB), network during build, ~25 GiB free disk for the work dir (the build
refuses tmpfs paths - /tmp on many systems is RAM).

### Provenance

Each build writes `packages.txt` (the resolved package inventory, `pacman -Q`
against the built rootfs, falling back to the declared names) and `SHA256SUMS`
(the ISO digest) next to the image in `live/out-<variant>/`, so a shipped ISO can
be inventoried and verified after the fact. The package set is still resolved
from the rolling Arch mirrors (and, for `nvidia-legacy`, the third-party omarchy
repo) at build time, so the image is not reproducible from the repo alone - see
[`../docs/DEPENDENCIES.md`](../docs/DEPENDENCIES.md).

## On the target machine

Boot the stick — the **TUI dashboard** auto-starts on the console (`bench-tui`,
htop/nvtop-style: live bars + sparklines, phase table, log, one-key start):

```
bench-tui                      # htop-style console dashboard (auto-starts on tty1)
bench-gui                      # graphical dashboard (X + mouse), same features
bench-live-run                 # plain console run, no dashboard
bench-live-run --with-gfx      # + FurMark (glmark2 fallback) under X
```

TUI keys: `s` start · `x` stop · `c` collect · `u` upload · `f` fan-curve ·
`p` cycle profile · `1-0` toggle groups · `r` report viewer · `G` graphical
dashboard · `t` shell · `PgUp/PgDn` log · `q` quit.

The boot dashboard runs as a systemd template, `bench-tui@.service`, which exports
its instance (`tty1` / `ttyS0`) as `BENCH_AGENT_INSTANCE` — that becomes the
runner's instance label on the control plane, so a stick that also serves a
serial console shows up as `bench-live (tty1)` / `bench-live (ttyS0)` instead of
two identical rows. The **ttyS0** instance only starts when the kernel is booted
with a `console=` on the cmdline **and** a serial device present (the drop-in
checks `ConditionKernelCommandLine=console` - which also matches
`console=ttyS0,115200` - plus `ConditionPathExists=/dev/ttyS0`),
so a normal boot runs a single dashboard.

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

## Self-update (no reflash)

Tick the toolkit's **update** action from the control plane (fleet card → *update…*,
or the `update_toolkit` command) and the stick fetches the toolkit at a ref onto
BENCHDATA, verifies it, and re-points `/opt/bench-test` — at runtime and again at
boot (via `bench-agent-env`). Kernel/driver changes still need a new image:
`iso_fetch` downloads + verifies a release ISO, and `iso_write` can flash it to a
*different* device attached to the same machine. Locally:

```bash
python bench/live_update.py toolkit --ref main
python bench/live_update.py iso-fetch iso-nvidia-legacy-38 && \
python bench/live_update.py iso-write /mnt/benchdata/iso/bench-live-nvidia-legacy.iso \
       --device /dev/sdX --yes
```

## Variants / vendor notes

| Variant | GPU support | Compute | Notes |
|---|---|---|---|
| `nvidia` | Turing → RTX 50 (`nvidia-open` 610) | PyTorch CUDA | default for modern NVIDIA |
| `nvidia-legacy` | Maxwell/Pascal/Volta (+ Turing..Ada) via 580 branch | PyTorch CUDA | uses the omarchy repo; the DKMS module is compiled into the ISO at build time (a boot service rebuilds it if needed) |
| `amd` | GCN+ for graphics (Mesa/RADV) | PyTorch ROCm | ROCm compute only on supported cards (RDNA+); older cards still run graphics/telemetry and skip compute |
| `hybrid` | AMD (Mesa/RADV) **and** modern NVIDIA (Turing → RTX 50, `nvidia-open`) | ROCm **or** CUDA, picked at run time | one stick for both; `Run-LinuxBench.sh` puts the baked CUDA torch on `PYTHONPATH` only when `nvidia-smi` is present, else the system ROCm torch. Legacy NVIDIA (Maxwell..Volta) still needs its own variant |

APU iGPUs (Phoenix `gfx1103` e.g. Radeon 780M/880M, Strix `gfx1150/1151`) are not
in ROCm's supported list; `Run-LinuxBench.sh` detects them via `rocminfo` and sets
`HSA_OVERRIDE_GFX_VERSION` (11.0.0 / 11.5.0) so the compute phases run. Set it
yourself to override. Graphics, telemetry and fan control need no ROCm.

Boot note: the image is unsigned — disable **Secure Boot** to boot it (Arch
installs are not signed). The GPU phases call `nvidia-smi` or
`rocm-smi`/amdgpu sysfs through `bench/gpu_util.py`; on unsupported hardware they
record a skip instead of failing.

## Layout

```
live/Build-LiveUSB.sh         build + write tool
live/packages.x86_64          common packages (appended to the archiso releng list)
live/packages-{nvidia,nvidia-legacy,amd}.x86_64
live/airootfs/                overlay: bench-tui, bench-gui, bench-live-run, bench-collect,
                              bench-agent-env, bench-fan-curve, bench-fan-auto, bench-upload,
                              motd, bench-tui@.service, dkms/fan units, X config
```

## Results & collection

Results live in RAM while the run happens and are collected to the stick's
**BENCHDATA** partition automatically when the run finishes, on `c`, and as a
final attempt when you quit the dashboard. The collector does not trust udev
labels: it re-reads the partition table (`partx`) and probes filesystems
directly (`blkid -p`), falling back through *by-label -> probed label -> the
big vFAT partition on the boot stick -> the ISO's small EFI partition*. Every
save includes a `collect-diag.txt` with the full device picture, and the
dashboard reports the true outcome ("saved to USB: ..." or a red
"USB save FAILED - ...").

If no writable stick partition can be found, the collector falls back to a raw
(unformatted) log slot — a partition labelled `BENCHLOG1` is written directly
with `bench/rawlog.py` (exits 5) — and if that is unavailable it keeps results in
`/root/bench-results` (**RAM**), says so loudly, and exits 4. Copy them off
before rebooting.

### Disk tests on the internal SSD (live session)

By default the disk phases target BENCHDATA (the USB stick - real but modest
throughput). To measure the machine's own disk instead, unlock and mount it
before starting the run:

```
press t (shell)
cryptsetup open /dev/sda2 cryptroot        # your LUKS passphrase
mkdir -p /mnt/internal
mount /dev/mapper/cryptroot /mnt/internal
exit                                        # back to the dashboard
```

The dashboard detects `/mnt/internal` automatically and points the disk
phases at `/mnt/internal/bench-scratch` (test files are cleaned up by the
phase scripts; peak usage ~12 GiB while the steady write runs). Results still
collect to the stick / upload as usual.

### Uploading to GitHub

`u` in the dashboard (or `bench-upload` in a shell) sends the newest results
into your repository via the GitHub API. Put the token once on the stick:

```
/mnt/benchdata/bench-upload.conf
--------------------------------
GITHUB_TOKEN=github_pat_xxx        # fine-grained PAT: Contents Read/Write
GITHUB_REPO=MaineCyberTech/bench-test
# GITHUB_BRANCH=main               # defaults to main
# UPLOAD_PATH=results
# AUTO_UPLOAD=1                    # upload right after a successful collect
--------------------------------
```

The config survives reflashing: `Build-LiveUSB.sh --write` preserves
`bench-upload.conf` across the wipe. On the desktop you can run
`bench/upload.py` directly against any results folder.
