#!/usr/bin/env bash
# Lay out a USB stick as an A/B bench-test stick:
#   p1 BENCH_ESP  (FAT32, 512 MiB): GRUB (BIOS + UEFI) + slot state (grubenv)
#   p2 slot A     (raw ISO9660)
#   p3 slot B     (raw ISO9660)
#   p4 BENCHDATA  (FAT32, rest): results + configs - never touched by updates
#
# Usage: sudo ./live/ab-layout.sh --iso PATH /dev/sdX
#
# Both slots start with the same ISO. Updates write the INACTIVE slot and flip
# the grub env; grub auto-reverts after 3 unconfirmed boots (the live system
# increments attempts early in boot and confirms at multi-user).
set -euo pipefail

ISO=""; DEV=""
while [ $# -gt 0 ]; do
    case "$1" in
        --iso) ISO="$2"; shift 2 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        /dev/*) DEV="$1"; shift ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done
[ -n "$ISO" ] && [ -f "$ISO" ] || { echo "error: --iso PATH required" >&2; exit 1; }
[ -n "$DEV" ] && [ -b "$DEV" ] || { echo "error: block device required (e.g. /dev/sdb)" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "error: run as root" >&2; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ESP_MNT=/mnt/bench-esp
DATA_MNT=/mnt/benchdata
ISO_BYTES=$(stat -c %s "$ISO")
SLOT_MIB=$(( ISO_BYTES / 1048576 + 32 ))
ESP_MIB=512
START_A=$(( 1 + ESP_MIB ))
START_B=$(( START_A + SLOT_MIB ))
START_DATA=$(( START_B + SLOT_MIB ))

command -v grub-install >/dev/null 2>&1 || { echo "[ab] installing grub"; pacman -S --needed --noconfirm grub >/dev/null; }
command -v mkfs.vfat >/dev/null 2>&1 || pacman -S --needed --noconfirm dosfstools >/dev/null

echo "[ab] $DEV: ESP ${ESP_MIB}M | A ${SLOT_MIB}M @${START_A}M | B @${START_B}M | data @${START_DATA}M"

# stash configs from an existing BENCHDATA (or the repo's private/)
STASH="$(mktemp -d)"
for part in "$DEV"*; do
    [ -b "$part" ] || continue
    if mount -o ro "$part" "$DATA_MNT" 2>/dev/null; then
        for n in bench-upload.conf bench-agent.conf; do
            [ -f "$DATA_MNT/$n" ] && cp "$DATA_MNT/$n" "$STASH/$n"
        done
        umount "$DATA_MNT" 2>/dev/null || true
    fi
done
for n in bench-upload.conf bench-agent.conf; do
    if [ ! -f "$STASH/$n" ] && [ -f "$REPO/live/private/$n" ]; then
        cp "$REPO/live/private/$n" "$STASH/$n"
    fi
done

umount "$DEV"* 2>/dev/null || true
wipefs -a "$DEV" >/dev/null 2>&1 || true
parted -s "$DEV" mklabel msdos \
    mkpart primary fat32 1MiB "${START_A}MiB" \
    mkpart primary "${START_A}MiB" "${START_B}MiB" \
    mkpart primary "${START_B}MiB" "${START_DATA}MiB" \
    mkpart primary fat32 "${START_DATA}MiB" 100%
parted -s "$DEV" set 1 esp on
parted -s "$DEV" set 1 boot on
sleep 1
partprobe "$DEV" 2>/dev/null || blockdev --rereadpt "$DEV" 2>/dev/null || true
sleep 2

P1="${DEV}1"; P2="${DEV}2"; P3="${DEV}3"; P4="${DEV}4"

# ESP + grub (BIOS + UEFI)
mkfs.vfat -n BENCH_ESP "$P1" >/dev/null
mkdir -p "$ESP_MNT"
mount "$P1" "$ESP_MNT"
mkdir -p "$ESP_MNT/boot/grub"
grub-install --target=i386-pc --boot-directory="$ESP_MNT/boot" --recheck "$DEV" >/dev/null
grub-install --target=x86_64-efi --efi-directory="$ESP_MNT" --boot-directory="$ESP_MNT/boot" \
    --removable --no-nvram >/dev/null
umount "$ESP_MNT"

# both slots start with the same image
echo "[ab] writing slot A"
dd if="$ISO" of="$P2" bs=4M conv=fsync status=none
echo "[ab] writing slot B"
dd if="$ISO" of="$P3" bs=4M conv=fsync status=none
sync
U_A=$(blkid -p -o value -s UUID "$P2" 2>/dev/null || true)
U_B=$(blkid -p -o value -s UUID "$P3" 2>/dev/null || true)
[ -n "$U_A" ] && [ -n "$U_B" ] || { echo "error: slot UUIDs not readable" >&2; exit 1; }

# grub.cfg + env
mount "$P1" "$ESP_MNT"
cat > "$ESP_MNT/boot/grub/grub.cfg" <<EOF
set timeout=5
set timeout_style=menu
load_env
if [ "\${attempts}" -ge 3 ]; then
    if [ "\${active}" = "A" ]; then set active=B; else set active=A; fi
    set attempts=0
    save_env active attempts
fi
if [ "\${active}" = "B" ]; then set default=1; else set default=0; fi
menuentry "bench-test (slot A)" {
    search --no-floppy --set=root --fs-uuid ${U_A}
    linux /arch/boot/x86_64/vmlinuz-linux archisobasedir=arch archisosearchuuid=${U_A} bench_slot=A copytoram=y nvidia_drm.modeset=1 nvidia_drm.fbdev=1
    initrd /arch/boot/x86_64/initramfs-linux.img
}
menuentry "bench-test (slot B)" {
    search --no-floppy --set=root --fs-uuid ${U_B}
    linux /arch/boot/x86_64/vmlinuz-linux archisobasedir=arch archisosearchuuid=${U_B} bench_slot=B copytoram=y nvidia_drm.modeset=1 nvidia_drm.fbdev=1
    initrd /arch/boot/x86_64/initramfs-linux.img
}
EOF
python3 - "$ESP_MNT/boot/grub/grubenv" <<'PYEOF'
import sys
body = "# GRUB Environment Block\nactive=A\nattempts=0\n"
body += "#" * (1024 - len(body))
open(sys.argv[1], "wb").write(body[:1024].encode())
PYEOF
umount "$ESP_MNT"

# BENCHDATA + configs
mkfs.vfat -n BENCHDATA "$P4" >/dev/null
mkdir -p "$DATA_MNT"
mount "$P4" "$DATA_MNT"
for n in "$STASH"/*; do
    [ -f "$n" ] && cp "$n" "$DATA_MNT/" && echo "[ab] restored $(basename "$n")"
done
umount "$DATA_MNT"
rm -rf "$STASH"
sync

echo "[ab] done:"
lsblk -o NAME,SIZE,FSTYPE,LABEL "$DEV"
