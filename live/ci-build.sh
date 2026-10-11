#!/usr/bin/env bash
# CI entry point: build the live ISO on whatever the runner provides.
# Arch runners build natively; any other Linux with docker builds inside an
# archlinux container. All heavy paths (venv, work dir, output ISO, pip cache)
# are routed to the filesystem with the most free space, so small-root runners
# (GitHub-hosted /mnt with 74G, lab guests) work as long as ONE volume has room.
#
# Usage: bash live/ci-build.sh <nvidia-legacy|nvidia|amd>
set -euo pipefail
GPU="${1:?usage: ci-build.sh <nvidia-legacy|nvidia|amd>}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

echo "[ci] host: $(uname -srm)  user: $(id -un)  nproc: $(nproc)  mem: $(free -h | awk '/Mem:/{print $2}')"
command -v docker >/dev/null 2>&1 && echo "[ci] docker: yes ($(docker --version 2>/dev/null))" || echo "[ci] docker: no"

# biggest writable non-system mount wins (e.g. /mnt on GitHub-hosted runners)
BIG=$(df -BG --output=avail,target 2>/dev/null | tail -n +2 \
      | grep -vE "/(dev|proc|sys|run|boot|efi)(/|$)|/dev/shm|/run" \
      | sort -rn | head -1 | awk '{print $2}')
FREE_GB=$(df -BG --output=avail "$BIG" 2>/dev/null | tail -1 | tr -dc '0-9')
echo "[ci] big volume: $BIG (${FREE_GB:-0}G free)"
if [ "${FREE_GB:-0}" -lt 40 ]; then
    echo "[ci] ERROR: the biggest volume has only ${FREE_GB:-0}G free; the build needs ~25-30G." >&2
    echo "[ci] Resize the runner or free space (this is why small lab guests cannot build the ISO)." >&2
    exit 1
fi
WORKROOT="$BIG/bench-ci"
mkdir -p "$WORKROOT/venv" "$WORKROOT/out-$GPU" "$WORKROOT/pip"

# route heavy state to the big volume
rm -rf .venv live/out-"$GPU"
ln -sfn "$WORKROOT/venv" .venv
ln -sfn "$WORKROOT/out-$GPU" live/out-"$GPU"

build_native() {
    echo "[ci] native Arch runner - building directly"
    if [ "$GPU" != "amd" ] && ! .venv/bin/python -c 'import torch' 2>/dev/null; then
        python3 -m venv .venv
        .venv/bin/pip install torch numpy psutil cryptography \
            --index-url https://download.pytorch.org/whl/cu126 \
            --extra-index-url https://pypi.org/simple
    fi
    sudo -n env BENCH_WORK_DIR="$WORKROOT/work" ./live/Build-LiveUSB.sh --gpu "$GPU"
}

build_container() {
    echo "[ci] non-Arch runner - building inside an archlinux container"
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
            pacman -Sy --noconfirm --needed archiso dosfstools python python-pip rsync
            if [ "$GPU" != "amd" ] && ! .venv/bin/python -c "import torch" 2>/dev/null; then
                python3 -m venv .venv
                .venv/bin/pip install torch numpy psutil cryptography \
                    --index-url https://download.pytorch.org/whl/cu126 \
                    --extra-index-url https://pypi.org/simple
            fi
            ./live/Build-LiveUSB.sh --gpu "$GPU"
        '
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
