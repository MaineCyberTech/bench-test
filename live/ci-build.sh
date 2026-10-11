#!/usr/bin/env bash
# CI entry point: build the live ISO on whatever the runner provides.
# Arch runners build natively; any other Linux with docker builds inside an
# archlinux container (the lab runners are Debian/Ubuntu containers).
#
# Usage: bash live/ci-build.sh <nvidia-legacy|nvidia|amd>
set -euo pipefail
GPU="${1:?usage: ci-build.sh <nvidia-legacy|nvidia|amd>}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

build_native() {
    echo "[ci] native Arch runner - building directly"
    if [ "$GPU" != "amd" ] && ! .venv/bin/python -c 'import torch' 2>/dev/null; then
        python3 -m venv .venv
        .venv/bin/pip install torch numpy psutil cryptography \
            --index-url https://download.pytorch.org/whl/cu126 \
            --extra-index-url https://pypi.org/simple
    fi
    sudo -n env BENCH_WORK_DIR=/var/tmp ./live/Build-LiveUSB.sh --gpu "$GPU"
}

build_container() {
    echo "[ci] non-Arch runner - building inside an archlinux container"
    mkdir -p "$HOME/.cache/bench-pip"
    docker run --rm --privileged \
        -v "$REPO:/work" -w /work \
        -v "$HOME/.cache/bench-pip:/root/.cache/pip" \
        -e GPU="$GPU" \
        archlinux:latest \
        bash -o pipefail -c '
            set -euo pipefail
            pacman-key --init 2>/dev/null || true
            pacman-key --populate archlinux 2>/dev/null || true
            pacman -Sy --noconfirm --needed archiso dosfstools python python-pip rsync
            if [ "$GPU" != "amd" ] && ! .venv/bin/python -c "import torch" 2>/dev/null; then
                python -m venv .venv
                .venv/bin/pip install torch numpy psutil cryptography \
                    --index-url https://download.pytorch.org/whl/cu126 \
                    --extra-index-url https://pypi.org/simple
            fi
            ./live/Build-LiveUSB.sh --gpu "$GPU"
        '
}

echo "[ci] host: $(uname -srm)  user: $(id -un)  home free: $(df -h "$HOME" | tail -1 | awk '{print $4}')"
command -v docker >/dev/null 2>&1 && echo "[ci] docker: yes ($(docker --version 2>/dev/null))" || echo "[ci] docker: no"

if [ -f /etc/arch-release ] && command -v pacman >/dev/null 2>&1; then
    build_native
elif command -v docker >/dev/null 2>&1; then
    build_container
else
    echo "[ci] ERROR: neither Arch nor docker available on this runner" >&2
    exit 1
fi
