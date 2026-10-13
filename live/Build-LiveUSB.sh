#!/usr/bin/env bash
# Build a bench-test live ISO with archiso and optionally write it to a USB device.
#
# Usage: sudo ./live/Build-LiveUSB.sh --gpu nvidia|nvidia-legacy|amd|hybrid [--out DIR] [--comp xz|zstd] [--write /dev/sdX]
#
#   --gpu nvidia         Turing+ (RTX 20/30/40/50) with nvidia-open (610)
#   --gpu nvidia-legacy  Maxwell/Pascal/Volta (and up) with the 580 branch (omarchy repo)
#   --gpu amd            AMD with Mesa/RADV graphics + ROCm PyTorch
#   --gpu hybrid         AMD + modern NVIDIA on one stick: Mesa/RADV + ROCm torch,
#                        nvidia-open + baked CUDA torch selected at run time
#   --out DIR            ISO output directory (default live/out)
#   --write /dev/sdX     after building, dd the ISO to the device and create a
#                        BENCHDATA partition in the free space (DESTRUCTIVE)
#
# The ISO boots to a console with the toolkit at /opt/bench-test; run `bench-live-run`.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE="$REPO/live"
OUT=""
GPU=""
COMP="xz"
ISO_ONLY=""
WRITE_DEV=""

while [ $# -gt 0 ]; do
    case "$1" in
        --gpu) GPU="${2:-}"; shift 2 ;;
        --out) OUT="${2:-}"; shift 2 ;;
        --comp) COMP="${2:-}"; shift 2 ;;
        --iso) ISO_ONLY="${2:-}"; shift 2 ;;
        --write) WRITE_DEV="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

if [ -z "$ISO_ONLY" ]; then
    case "$GPU" in
        nvidia|nvidia-legacy|amd|hybrid) ;;
        *) echo "error: --gpu nvidia|nvidia-legacy|amd|hybrid is required" >&2; exit 1 ;;
    esac
fi
[ -n "$OUT" ] || OUT="$REPO/live/out-$GPU"

if [ "$(id -u)" -ne 0 ]; then
    echo "error: run as root (mkarchiso requires root)" >&2
    exit 1
fi

if [ -n "$ISO_ONLY" ]; then
    [ -f "$ISO_ONLY" ] || { echo "error: --iso not found: $ISO_ONLY" >&2; exit 1; }
    [ -n "$WRITE_DEV" ] || { echo "error: --iso only makes sense together with --write /dev/sdX" >&2; exit 1; }
    ISO="$ISO_ONLY"
    echo "[build] skipping build; using prebuilt ISO $ISO ($(du -h "$ISO" | cut -f1))"
else

pacman -S --needed --noconfirm archiso dosfstools >/dev/null || true
command -v mkarchiso >/dev/null 2>&1 || { echo "mkarchiso missing"; exit 1; }

WORK_BASE="${BENCH_WORK_DIR:-/var/tmp}"
mkdir -p "$WORK_BASE"
FREE_GB="$(df -BG --output=avail "$WORK_BASE" | tail -1 | tr -dc '0-9')"
if [ -z "$FREE_GB" ] || [ "$FREE_GB" -lt 25 ]; then
    echo "error: need >= 25 GiB free at $WORK_BASE (found ${FREE_GB:-0} GiB)" >&2
    echo "       (do not build in /tmp - it is often tmpfs)" >&2
    exit 1
fi
WORK="$(mktemp -d "$WORK_BASE/bench-live.XXXXXX")"
PROFILE="$WORK/profile"
echo "[build] gpu=$GPU profile=$PROFILE"
cp -r /usr/share/archiso/configs/releng/. "$PROFILE/"
sed -i 's/^iso_name=.*/iso_name="bench-live"/; s/^iso_label=.*/iso_label="BENCH_LIVE"/; s/^iso_publisher=.*/iso_publisher="bench-test"/; s/^iso_application=.*/iso_application="bench-test live environment"/' "$PROFILE/profiledef.sh"

case "$COMP" in
    xz) ;;
    zstd)
        sed -i "s|^airootfs_image_tool_options=.*|airootfs_image_tool_options=('-comp' 'zstd' '-Xcompression-level' '15' '-b' '1M')|" "$PROFILE/profiledef.sh"
        echo "[build] squashfs compression: zstd level 15 (faster build, larger ISO)"
        ;;
    *) echo "error: --comp must be xz or zstd" >&2; exit 1 ;;
esac

cat "$LIVE/packages.x86_64" >> "$PROFILE/packages.x86_64"
cat "$LIVE/packages-$GPU.x86_64" >> "$PROFILE/packages.x86_64"

# NVIDIA variants: bake the toolkit venv's Maxwell/Pascal-safe PyTorch (cu126).
# Arch's python-pytorch-cuda is built for CUDA 13, which has no kernels for
# pre-Turing GPUs (GTX 9xx/10xx) - GPU phases would error instantly there.
if [ "$GPU" != "amd" ] && "$REPO/.venv/bin/python" -c 'import torch' >/dev/null 2>&1; then
    echo "[build] baking venv PyTorch: $("$REPO/.venv/bin/python" -c 'import torch; print(torch.__version__)')"
    # keep packages OUTSIDE pacman-owned site-packages (overlay is copied before
    # pacstrap, and shadowed files abort the install) and outside the toolkit dir:
    # the toolkit is re-pointed at a fetched copy on live sticks, which would hide
    # a baked py-packages and silently remove torch from every GPU phase.
    PP="$PROFILE/airootfs/opt/bench-pypackages"
    mkdir -p "$PP" "$PROFILE/airootfs/usr/lib/python3.14/site-packages"
    rsync -a --exclude '__pycache__' --exclude 'pip*' --exclude 'setuptools*' \
        "$REPO/.venv/lib/python3.14/site-packages/" "$PP/"
    if [ "$GPU" = "hybrid" ]; then
        # hybrid: the CUDA build shares the stick with the system ROCm PyTorch, so
        # no static .pth - the launcher (Run-LinuxBench.sh) puts this tree on
        # PYTHONPATH only on NVIDIA, leaving ROCm for AMD.
        echo "[build] hybrid: CUDA PyTorch baked to /opt/bench-pypackages (launcher-selected)"
    else
        printf '%s\n' "/opt/bench-pypackages" \
            > "$PROFILE/airootfs/usr/lib/python3.14/site-packages/zz-bench-torch.pth"
    fi
elif [ "$GPU" != "amd" ]; then
    echo "[build] WARNING: no toolkit venv with torch - falling back to python-pytorch-cuda (NOT Maxwell-compatible)"
    echo "python-pytorch-cuda" >> "$PROFILE/packages.x86_64"
fi

if [ "$GPU" = "nvidia-legacy" ]; then
    printf '\n[omarchy]\nServer = https://pkgs.omarchy.org/stable/$arch\n' >> "$PROFILE/pacman.conf"
fi

cp -r "$LIVE/airootfs/." "$PROFILE/airootfs/"

# the headless-X config pins the NVIDIA driver; drop it for non-NVIDIA images so
# gfx/fan (which start X with that configdir) still work on the amd build
case "$GPU" in
    nvidia*) : ;;
    *) rm -f "$PROFILE/airootfs/etc/X11/bench-headless.d/10-headless.conf" ;;
esac

# optional private upload credentials (gitignored, never in the repo):
# live/private/bench-upload.conf is baked to /etc/bench-upload.conf so uploads
# work even when the stick's own copy cannot be reached (mount trouble)
if [ -f "$LIVE/private/bench-upload.conf" ]; then
    install -m 600 "$LIVE/private/bench-upload.conf" "$PROFILE/airootfs/etc/bench-upload.conf"
    echo "[build] baked private bench-upload.conf -> /etc/bench-upload.conf"
fi

# optional control-plane config (server URL + token): the live dashboards check
# in automatically when /etc/bench-agent.conf is present
if [ -f "$LIVE/private/bench-agent.conf" ]; then
    install -m 600 "$LIVE/private/bench-agent.conf" "$PROFILE/airootfs/etc/bench-agent.conf"
    echo "[build] baked private bench-agent.conf -> /etc/bench-agent.conf"
fi

# NVIDIA console: fbdev gives the VT a framebuffer once nvidia-drm loads.
# Without it the text console goes dark on real hardware (a live system has no
# compositor to take over the display) - the dashboard would run invisibly.
#
# copytoram=y: the initramfs copies the squashfs to RAM and unmounts/detaches
# the whole USB disk. Modern kernels (6.5+ block-device claim rules) make a
# disk's partitions unclaimable while the whole disk is mounted - without this,
# BENCHDATA can be read (blkid) but never mounted or written (the "Can't open
# blockdev" error). With the whole disk released, BENCHDATA mounts normally.
mkdir -p "$PROFILE/airootfs/etc/modprobe.d"
printf 'options nvidia_drm modeset=1 fbdev=1\n' > "$PROFILE/airootfs/etc/modprobe.d/zz-bench-nvidia.conf"
for f in "$PROFILE"/efiboot/loader/entries/0[12]-archiso-*.conf; do
    [ -f "$f" ] && sed -i -E 's/^(options .*)$/\1 nvidia_drm.modeset=1 nvidia_drm.fbdev=1 copytoram=y/' "$f"
done
[ -f "$PROFILE/syslinux/archiso_sys-linux.cfg" ] && \
    sed -i -E 's/^(APPEND .*archisobasedir.*)$/\1 nvidia_drm.modeset=1 nvidia_drm.fbdev=1 copytoram=y/' \
        "$PROFILE/syslinux/archiso_sys-linux.cfg"
for f in "$PROFILE/grub/grub.cfg" "$PROFILE/grub/loopback.cfg"; do
    [ -f "$f" ] && sed -i -E 's/^(linux .*)$/\1 nvidia_drm.modeset=1 nvidia_drm.fbdev=1 copytoram=y/' "$f"
done

# toolkit into the image (no git/venv/results/ISOs/dist bundles/build leftovers/PACKAGED SECRETS)
mkdir -p "$PROFILE/airootfs/opt/bench-test"
rsync -a \
    --exclude '.git' --exclude '.venv' --exclude 'results' --exclude 'live/out*' \
    --exclude 'live/private' --exclude 'server/data' --exclude 'server/benchd.conf' \
    --exclude 'dist' --exclude '__pycache__' --exclude '*.log' \
    "$REPO/" "$PROFILE/airootfs/opt/bench-test/"

# the image must never carry private credentials or build leftovers — fail the build if it does
for bad in "$PROFILE/airootfs/opt/bench-test/live/private" \
           "$PROFILE/airootfs/opt/bench-test/server/benchd.conf" \
           "$PROFILE/airootfs/opt/bench-test/.git" \
           "$PROFILE/airootfs/opt/bench-test/.venv" \
           "$PROFILE/airootfs/opt/bench-test/results" \
           "$PROFILE/airootfs/opt/bench-test/dist" \
           "$PROFILE/airootfs/opt/bench-test/server/data"; do
    if [ -e "$bad" ]; then
        echo "[build] ERROR: excluded path leaked into the image: $bad" >&2
        exit 1
    fi
done
# logs and bytecode caches are excluded from the rsync above; verify none slipped in
if find "$PROFILE/airootfs/opt/bench-test" \( -name '*.log' -o -name '__pycache__' -o -name '*.pyc' \) \
        -print -quit | grep -q .; then
    echo "[build] ERROR: logs or bytecode leaked into the image" >&2
    exit 1
fi

# stamp the toolkit identity so a flashed stick can report which build it runs
# (the fleet view then shows "update available" against the latest main)
if git -C "$REPO" rev-parse --short HEAD >/dev/null 2>&1; then
    _c="$(git -C "$REPO" rev-parse --short HEAD)"
    _b="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || echo detached)"
    printf '{"mode":"iso","ref":"%s","commit":"%s","built":"%s","sha256":null}\n' \
        "$_b" "$_c" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        > "$PROFILE/airootfs/opt/bench-test/.bench-version"
    echo "[build] stamped toolkit $_b@$_c"
fi

# FurMark 2 linux64 into tools/: repo copy -> tools release asset -> geeks3d
FM_SRC="$REPO/tools/furmark/FurMark_linux64"
if [ ! -x "$FM_SRC/furmark" ]; then
    echo "[build] fetching FurMark from the tools release asset"
    mkdir -p "$WORK/furmark"
    if curl -fsSL -o "$WORK/furmark.tar.xz" \
        "https://github.com/MaineCyberTech/bench-test/releases/download/tools-v1/furmark-linux64.tar.xz"; then
        tar -xJf "$WORK/furmark.tar.xz" -C "$WORK/furmark"
    fi
    FM_SRC="$(find "$WORK/furmark" -maxdepth 2 -type f -name furmark -printf '%h\n' 2>/dev/null | head -1)"
fi
if [ -z "${FM_SRC:-}" ] || [ ! -x "$FM_SRC/furmark" ]; then
    echo "[build] tools asset unavailable - trying geeks3d"
    mkdir -p "$WORK/furmark"
    if curl -fsSL -A "Mozilla/5.0" -o "$WORK/furmark.7z" "https://geeks3d.com/dl/get/833"; then
        bsdtar -xf "$WORK/furmark.7z" -C "$WORK/furmark"
        FM_SRC="$(find "$WORK/furmark" -maxdepth 2 -type f -name furmark -printf '%h\n' 2>/dev/null | head -1)"
    fi
fi
if [ -n "${FM_SRC:-}" ] && [ -x "$FM_SRC/furmark" ]; then
    mkdir -p "$PROFILE/airootfs/opt/bench-test/tools/furmark"
    cp -r "$FM_SRC" "$PROFILE/airootfs/opt/bench-test/tools/furmark/"
    # keep a copy OUTSIDE the toolkit dir too: a live stick re-points /opt/bench-test
    # at a fetched copy, which hides a baked tools/ (FurMark) - the same reason the
    # PyTorch packages live in /opt/bench-pypackages
    mkdir -p "$PROFILE/airootfs/opt/bench-tools/furmark"
    cp -r "$FM_SRC" "$PROFILE/airootfs/opt/bench-tools/furmark/"
    # some copy paths lose the exec bit (seen in CI); force it
    chmod 755 "$PROFILE/airootfs/opt/bench-test/tools/furmark/FurMark_linux64/furmark" \
              "$PROFILE/airootfs/opt/bench-tools/furmark/FurMark_linux64/furmark" 2>/dev/null || true
else
    echo "[build] WARNING: FurMark not available - gfx phase will skip" >&2
fi

# enable the dkms build unit (legacy NVIDIA) and the TUI dashboards
mkdir -p "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants"
ln -sf ../bench-dkms.service \
    "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants/bench-dkms.service"
ln -sf ../bench-tui@.service \
    "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants/bench-tui@tty1.service"
ln -sf ../bench-tui@.service \
    "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants/bench-tui@ttyS0.service"
# fan-curve daemon: starts at boot only when a curve conf exists (off by
# default); the dashboard's 'f' key starts it explicitly
ln -sf ../bench-fan-curve.service \
    "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants/bench-fan-curve.service"
# fan curve auto-enable: applies the curve at boot when the hardware supports
# it (the dashboard's 'f' still switches it off for the session)
ln -sf ../bench-fan-auto.service \
    "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants/bench-fan-auto.service"
# A/B slot bookkeeping (no-op on non-A/B sticks)
ln -sf ../bench-boot-attempt.service \
    "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants/bench-boot-attempt.service"
ln -sf ../bench-boot-confirm.service \
    "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants/bench-boot-confirm.service"

# exec permissions for our helpers. The optional private configs are installed
# mode 600, but mkarchiso re-copies airootfs with --no-preserve=ownership,mode, so
# the modes must be re-applied here; for a file that was not baked mkarchiso warns
# and continues (the build never carries an empty placeholder config).
cat >> "$PROFILE/profiledef.sh" <<'EOF'

file_permissions+=(
  ["/usr/local/bin/bench-tui"]="0:0:755"
  ["/usr/local/bin/bench-gui"]="0:0:755"
  ["/usr/local/bin/bench-agent-env"]="0:0:755"
  ["/usr/local/bin/bench-fan-curve"]="0:0:755"
  ["/usr/local/bin/bench-fan-auto"]="0:0:755"
  ["/usr/local/bin/bench-slot"]="0:0:755"
  ["/usr/local/bin/bench-live-run"]="0:0:755"
  ["/usr/local/bin/bench-collect"]="0:0:755"
  ["/usr/local/bin/bench-upload"]="0:0:755"
  ["/opt/bench-test/bench/Run-LinuxBench.sh"]="0:0:755"
  ["/opt/bench-test/tools/furmark/FurMark_linux64/furmark"]="0:0:755"
  ["/etc/bench-agent.conf"]="0:0:600"
  ["/etc/bench-upload.conf"]="0:0:600"
)
EOF

mkdir -p "$OUT"
mkarchiso -v -w "$WORK/work" -o "$OUT" "$PROFILE"

ISO="$(ls -t "$OUT"/*.iso | head -1)"
echo "[build] ISO ready: $ISO  ($(du -h "$ISO" | cut -f1))"

# --- provenance / inventory (SBOM-P3-001, SC-P3-001) --------------------------
# The package set is name-only and resolved from the rolling Arch mirrors (and,
# for nvidia-legacy, the third-party omarchy repo) at build time, so an image is
# not reproducible from the repo alone. Record what was actually built next to
# the ISO: the resolved package inventory from the installed rootfs (falling back
# to the declared names) plus a SHA256SUMS for the image. See docs/DEPENDENCIES.md.
PROV_PKGS="$OUT/packages.txt"
ROOTFS="$(find "$WORK/work" -maxdepth 6 -type d -path '*/airootfs/var/lib/pacman' 2>/dev/null | head -1 || true)"
ROOTFS="${ROOTFS%/var/lib/pacman}"
{
    echo "# bench-test live ISO package inventory"
    echo "# variant: $GPU"
    echo "# built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if [ -n "$ROOTFS" ] && pacman -Q --dbpath "$ROOTFS/var/lib/pacman" >/dev/null 2>&1; then
        echo "# source: installed package db (pacman -Q) from the built rootfs"
        pacman -Q --dbpath "$ROOTFS/var/lib/pacman" 2>/dev/null || true
    else
        echo "# source: declared names (installed db unavailable)"
        grep -hv '^#' "$LIVE/packages.x86_64" "$LIVE/packages-$GPU.x86_64" 2>/dev/null \
            | sed '/^[[:space:]]*$/d' || true
    fi
} > "$PROV_PKGS"
if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$OUT" && sha256sum "$(basename "$ISO")" > SHA256SUMS ) || true
else
    echo "[build] WARNING: sha256sum not found - no SHA256SUMS written" >&2
fi
echo "[build] provenance: $PROV_PKGS + $OUT/SHA256SUMS"
fi

if [ -n "$WRITE_DEV" ]; then
    echo "[write] DESTRUCTIVE: writing $ISO to $WRITE_DEV"
    DEVNAME="$(basename "$WRITE_DEV")"
    RULE=/run/udev/rules.d/99-bench-flash.rules
    cleanup_rule() {
        rm -f "$RULE"
        udevadm control --reload 2>/dev/null || true
        udevadm trigger --subsystem-match=block --sysname-match="${DEVNAME}*" 2>/dev/null || true
    }
    # keep automounters (udisks) from grabbing partitions mid-flash: an open
    # partition stops the kernel from re-reading the table
    mkdir -p /run/udev/rules.d
    echo "SUBSYSTEM==\"block\", KERNEL==\"${DEVNAME}*\", ENV{UDISKS_IGNORE}=\"1\"" > "$RULE"
    udevadm control --reload 2>/dev/null || true
    udevadm trigger --subsystem-match=block --sysname-match="${DEVNAME}*" 2>/dev/null || true
    trap cleanup_rule EXIT

    for i in 1 2 3 4 5; do
        for part in "$WRITE_DEV"*; do
            [ -b "$part" ] && umount "$part" 2>/dev/null || true
        done
        mount | grep -q "$WRITE_DEV" || break
        sleep 1
    done

    # preserve the GitHub upload config across reflashes when present
    CONF_SAVE=""
    for part in "$WRITE_DEV"*; do
        [ -b "$part" ] || continue
        if mount "$part" /mnt 2>/dev/null; then
            if [ -f /mnt/bench-upload.conf ]; then
                CONF_SAVE="$(mktemp)"
                cp /mnt/bench-upload.conf "$CONF_SAVE"
                echo "[write] preserved bench-upload.conf from $part"
            fi
            umount /mnt 2>/dev/null || true
        fi
    done

    dd if="$ISO" of="$WRITE_DEV" bs=4M status=progress conv=fsync
    sync
    udevadm settle || true

    ISO_BYTES=$(stat -c %s "$ISO")
    START_MIB=$(( ISO_BYTES / 1048576 + 16 ))
    echo "[write] creating BENCHDATA partition at ${START_MIB}MiB"
    blockdev --rereadpt "$WRITE_DEV" 2>/dev/null || true
    sleep 1
    parted -s "$WRITE_DEV" mkpart primary fat32 "${START_MIB}MiB" 100% || true
    sleep 1
    blockdev --rereadpt "$WRITE_DEV" 2>/dev/null || true
    partx -a "$WRITE_DEV" 2>/dev/null || true
    sleep 2

    # the new partition starts at/after the end of the ISO image
    MIN_SECTOR=$(( ISO_BYTES / 512 ))
    PART=$(lsblk -lbno NAME,START "$WRITE_DEV" | tail -n +2 | awk -v m="$MIN_SECTOR" '$2 >= m {print $1; exit}')
    if [ -z "$PART" ]; then
        echo "[write] BENCHDATA partition not visible to the kernel; finish manually:"
        echo "        sudo partx -a $WRITE_DEV && sudo mkfs.vfat -n BENCHDATA ${WRITE_DEV}3"
    else
        SIZE_MB=$(( $(lsblk -bdno SIZE "/dev/$PART") / 1000000 ))
        if [ "$SIZE_MB" -lt 1000 ]; then
            echo "[write] refusing to format /dev/$PART (${SIZE_MB}MB - boot partition?)"
        else
            mkfs.vfat -n BENCHDATA "/dev/$PART"
            echo "[write] BENCHDATA on /dev/$PART (${SIZE_MB}MB)"
            if [ -n "$CONF_SAVE" ]; then
                if mount "/dev/$PART" /mnt 2>/dev/null; then
                    cp "$CONF_SAVE" /mnt/bench-upload.conf && echo "[write] restored bench-upload.conf"
                    umount /mnt 2>/dev/null || true
                fi
                rm -f "$CONF_SAVE"
            fi
        fi
    fi
    cleanup_rule
    echo "[write] done - boot the stick; the dashboard auto-starts"
fi
