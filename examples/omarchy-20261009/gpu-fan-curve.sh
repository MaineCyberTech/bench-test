#!/usr/bin/env bash
# GPU fan curve daemon (Linux equivalent of the repo's nvfancontrol setup).
# Runs as root: user-level nvidia-settings writes are blocked by the driver on Wayland.
set -u

NS="${NS:-/opt/nvidia-settings/usr/bin/nvidia-settings}"
export LD_LIBRARY_PATH="${NS%/usr/bin/nvidia-settings}/usr/lib"
export DISPLAY="${DISPLAY:-:0}"
CURVE="${FAN_CURVE:-35:26 45:34 55:44 60:52 68:64 75:76 82:90 88:100}"
INTERVAL="${INTERVAL:-5}"
XUSER="${XUSER:-$(stat -c %U /run/user/1000 2>/dev/null || echo user)}"

grant_x_access() {
    runuser -u "$XUSER" -- env DISPLAY="$DISPLAY" xhost +SI:localuser:root >/dev/null 2>&1
}

target_for() {
    awk -v t="$1" -v c="$CURVE" 'BEGIN{
        n = split(c, p, " ")
        for (i = 1; i <= n; i++) { split(p[i], q, ":"); x[i] = q[1]; y[i] = q[2] }
        if (t <= x[1]) { print y[1]; exit }
        if (t >= x[n]) { print y[n]; exit }
        for (i = 1; i < n; i++) {
            if (t >= x[i] && t <= x[i+1]) {
                printf "%d", y[i] + (y[i+1] - y[i]) * (t - x[i]) / (x[i+1] - x[i])
                exit
            }
        }
    }'
}

last=-1
while true; do
    temp="$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1)"
    if [ -n "$temp" ]; then
        tgt="$(target_for "$temp")"
        delta=$(( tgt > last ? tgt - last : last - tgt ))
        if [ "$last" -lt 0 ] || [ "$delta" -ge 3 ]; then
            if ! "$NS" -a GPUFanControlState=1 -a GPUTargetFanSpeed="$tgt" >/dev/null 2>&1; then
                grant_x_access
                "$NS" -a GPUFanControlState=1 -a GPUTargetFanSpeed="$tgt" >/dev/null 2>&1 && last=$tgt
            else
                last=$tgt
            fi
        fi
    fi
    sleep "$INTERVAL"
done
