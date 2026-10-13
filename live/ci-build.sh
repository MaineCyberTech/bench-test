#!/usr/bin/env bash
# CI entry point: build the live ISO on whatever the runner provides.
# Arch runners build natively; any other Linux with docker builds inside an
# archlinux container. All heavy paths (venv, work dir, output ISO, pip cache)
# are routed to the filesystem with the most free space, so small-root runners
# (GitHub-hosted /mnt with 74G, lab guests) work as long as ONE volume has room.
#
# Usage: bash live/ci-build.sh <nvidia-legacy|nvidia|amd>
set -euo pipefail
GPU="${1:?usage: ci-build.sh <nvidia-legacy|nvidia|amd> [xz|zstd]}"
COMP="${2:-xz}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

echo "[ci] host: $(uname -srm)  user: $(id -un)  nproc: $(nproc)  mem: $(free -h | awk '/Mem:/{print $2}')"
command -v docker >/dev/null 2>&1 && echo "[ci] docker: yes ($(docker --version 2>/dev/null))" || echo "[ci] docker: no"

# pick the roomiest writable directory for heavy state
BEST=""; BEST_GB=0
for d in /var/tmp /mnt "$HOME" /tmp; do
    [ -d "$d" ] && [ -w "$d" ] || continue
    fg=$(df -BG --output=avail "$d" 2>/dev/null | tail -1 | tr -dc '0-9'); fg=${fg:-0}
    echo "[ci] candidate $d: ${fg}G free"
    [ "$fg" -gt "$BEST_GB" ] && { BEST="$d"; BEST_GB="$fg"; }
done
echo "[ci] big volume: $BEST (${BEST_GB}G free)"
if [ "$BEST_GB" -lt 40 ]; then
    echo "[ci] ERROR: the biggest writable volume has only ${BEST_GB}G free; the build needs ~25-30G." >&2
    echo "[ci] Resize the runner or free space (this is why small lab guests cannot build the ISO)." >&2
    exit 1
fi
WORKROOT="$BEST/bench-ci"
mkdir -p "$WORKROOT/venv" "$WORKROOT/out-$GPU" "$WORKROOT/pip"

# route heavy state to the big volume
rm -rf .venv live/out-"$GPU"
ln -sfn "$WORKROOT/venv" .venv
ln -sfn "$WORKROOT/out-$GPU" live/out-"$GPU"

build_native() {
    echo "[ci] native Arch runner - building directly"
    if [ -f live/keys/omarchy.gpg ]; then
        sudo -n pacman-key --add live/keys/omarchy.gpg >/dev/null 2>&1 || true
        sudo -n pacman-key --lsign-key 40DFB630FF42BCFFB047046CF0134EE680CAC571 >/dev/null 2>&1 || true
    fi
    [ -x "$WORKROOT/venv/bin/python" ] || python3 -m venv --clear "$WORKROOT/venv"
    if [ "$GPU" != "amd" ] && ! .venv/bin/python -c 'import torch' 2>/dev/null; then
        .venv/bin/pip install torch numpy psutil cryptography \
            --index-url https://download.pytorch.org/whl/cu126 \
            --extra-index-url https://pypi.org/simple
    fi
    sudo -n env BENCH_WORK_DIR="$WORKROOT/work" ./live/Build-LiveUSB.sh --gpu "$GPU" --comp "$COMP"
}

build_container() {
    echo "[ci] non-Arch runner - building inside an archlinux container"
    # NOTE (audit, not implemented): `archlinux:latest` is a mutable tag, so the
    # build toolchain can change between runs. Pin it by digest
    # (archlinux@sha256:<digest>) when the build image is settled.
    docker run --rm --privileged \
        -v "$REPO:/work" -w /work \
        -v "$WORKROOT:/bigwork" \
        -v "$WORKROOT/pip:/root/.cache/pip" \
        -e GPU="$GPU" -e BENCH_WORK_DIR=/bigwork/work \
        archlinux:latest \
        bash -o pipefail -c '
            set -euo pipefail
            pacman-key --init 2>/dev/null || true
            pacman-key --populate archlinux 2>/dev/null || true
            if [ -f /work/live/keys/omarchy.gpg ]; then
                pacman-key --add /work/live/keys/omarchy.gpg >/dev/null 2>&1 || true
                pacman-key --lsign-key 40DFB630FF42BCFFB047046CF0134EE680CAC571 >/dev/null 2>&1 || true
            fi
            pacman -Sy --noconfirm --needed archiso dosfstools python python-pip rsync
            [ -x /bigwork/venv/bin/python ] || python3 -m venv --clear /bigwork/venv
            # host-side absolute symlinks do not resolve in here; re-point them
            ln -sfn /bigwork/venv .venv
            ln -sfn /bigwork/out-'"$GPU"' live/out-'"$GPU"'
            if [ "$GPU" != "amd" ] && ! .venv/bin/python -c "import torch" 2>/dev/null; then
                .venv/bin/pip install torch numpy psutil cryptography \
                    --index-url https://download.pytorch.org/whl/cu126 \
                    --extra-index-url https://pypi.org/simple
            fi
            ./live/Build-LiveUSB.sh --gpu "$GPU" --comp '"$COMP"'
        '
    # the container re-pointed the symlinks; restore them for host-side use
    ln -sfn "$WORKROOT/venv" .venv
    ln -sfn "$WORKROOT/out-$GPU" live/out-"$GPU"
}

if [ -f /etc/arch-release ] && command -v pacman >/dev/null 2>&1; then
    build_native
elif command -v docker >/dev/null 2>&1; then
    build_container
else
    echo "[ci] ERROR: neither Arch nor docker available on this runner" >&2
    exit 1
fi

ISO=$(ls -t live/out-"$GPU"/*.iso | head -1)
echo "[ci] built: $ISO"
echo "ISO_PATH=$ISO" >> "${GITHUB_ENV:-/dev/null}"
