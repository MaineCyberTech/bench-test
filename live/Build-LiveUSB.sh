#!/usr/bin/env bash
# Build a bench-test live ISO with archiso and optionally write it to a USB device.
#
# Usage: sudo ./live/Build-LiveUSB.sh --gpu nvidia|nvidia-legacy|amd [--out DIR] [--write /dev/sdX]
#
#   --gpu nvidia         Turing+ (RTX 20/30/40/50) with nvidia-open (610)
#   --gpu nvidia-legacy  Maxwell/Pascal/Volta (and up) with the 580 branch (omarchy repo)
#   --gpu amd            AMD with Mesa/RADV graphics + ROCm PyTorch
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
WRITE_DEV=""

while [ $# -gt 0 ]; do
    case "$1" in
        --gpu) GPU="${2:-}"; shift 2 ;;
        --out) OUT="${2:-}"; shift 2 ;;
        --write) WRITE_DEV="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

case "$GPU" in
    nvidia|nvidia-legacy|amd) ;;
    *) echo "error: --gpu nvidia|nvidia-legacy|amd is required" >&2; exit 1 ;;
esac
[ -n "$OUT" ] || OUT="$REPO/live/out-$GPU"

if [ "$(id -u)" -ne 0 ]; then
    echo "error: run as root (mkarchiso requires root)" >&2
    exit 1
fi

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

cat "$LIVE/packages.x86_64" >> "$PROFILE/packages.x86_64"
cat "$LIVE/packages-$GPU.x86_64" >> "$PROFILE/packages.x86_64"

if [ "$GPU" = "nvidia-legacy" ]; then
    printf '\n[omarchy]\nServer = https://pkgs.omarchy.org/stable/$arch\n' >> "$PROFILE/pacman.conf"
fi

cp -r "$LIVE/airootfs/." "$PROFILE/airootfs/"

# toolkit into the image (no git/venv/results)
mkdir -p "$PROFILE/airootfs/opt/bench-test"
rsync -a \
    --exclude '.git' --exclude '.venv' --exclude 'results' --exclude 'live/out' \
    "$REPO/" "$PROFILE/airootfs/opt/bench-test/"

# FurMark 2 linux64 into tools/
if [ ! -x "$REPO/tools/furmark/FurMark_linux64/furmark" ]; then
    echo "[build] downloading FurMark 2 (linux64)"
    mkdir -p "$WORK/furmark"
    curl -fsSL -A "Mozilla/5.0" -o "$WORK/furmark.7z" "https://geeks3d.com/dl/get/833"
    bsdtar -xf "$WORK/furmark.7z" -C "$WORK/furmark"
fi
mkdir -p "$PROFILE/airootfs/opt/bench-test/tools/furmark"
cp -r "$REPO/tools/furmark/FurMark_linux64" "$PROFILE/airootfs/opt/bench-test/tools/furmark/"

# enable the dkms build unit (legacy NVIDIA) and the TUI dashboards
mkdir -p "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants"
ln -sf ../bench-dkms.service \
    "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants/bench-dkms.service"
ln -sf ../bench-tui@.service \
    "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants/bench-tui@tty1.service"
ln -sf ../bench-tui@.service \
    "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants/bench-tui@ttyS0.service"

# exec permissions for our helpers
cat >> "$PROFILE/profiledef.sh" <<'EOF'

file_permissions+=(
  ["/usr/local/bin/bench-tui"]="0:0:755"
  ["/usr/local/bin/bench-gui"]="0:0:755"
  ["/usr/local/bin/bench-fan-curve"]="0:0:755"
  ["/usr/local/bin/bench-live-run"]="0:0:755"
  ["/usr/local/bin/bench-collect"]="0:0:755"
  ["/opt/bench-test/bench/Run-LinuxBench.sh"]="0:0:755"
)
EOF

mkdir -p "$OUT"
mkarchiso -v -w "$WORK/work" -o "$OUT" "$PROFILE"

ISO="$(ls -t "$OUT"/*.iso | head -1)"
echo "[build] ISO ready: $ISO  ($(du -h "$ISO" | cut -f1))"

if [ -n "$WRITE_DEV" ]; then
    echo "[write] DESTRUCTIVE: writing $ISO to $WRITE_DEV"
    dd if="$ISO" of="$WRITE_DEV" bs=4M status=progress conv=fsync
    sync
    if command -v sgdisk >/dev/null 2>&1; then
        sgdisk -n 0:0:0 -t 0:0700 -c 0:BENCHDATA "$WRITE_DEV" || true
        partprobe "$WRITE_DEV" || true
        sleep 2
        PART="$(lsblk -lno NAME,PARTLABEL "$WRITE_DEV" | awk '$2=="BENCHDATA" {print "/dev/"$1; exit}')"
        [ -n "$PART" ] && mkfs.vfat -n BENCHDATA "$PART" || true
    fi
    echo "[write] done - boot the stick and run: bench-live-run --with-gfx"
fi
