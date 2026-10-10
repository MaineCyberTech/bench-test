#!/usr/bin/env bash
# Run-LinuxBench.sh - Linux runner for the bench-test Python phases
# (GPU/CPU/RAM/disk + combined, full-system soak, network, NVENC, inventory).
# Writes results/<host>-linux-<stamp>.json and .md, mirroring the PowerShell invokers' metrics.
#
# Usage: ./Run-LinuxBench.sh [-o OutDir] [--gpu-soak S] [--cpu-soak S] [--ram-soak S]
#   [--combined-seconds S] [--fullsoak-seconds S] [--gfx-seconds S] [--fan-seconds S]
#   [--gpu-size N] [--cpu-size N] [--ram-size-mb N] [--fill-pct N] [--passes N]
#   [--disk-path DIR] [--disk-size-mb N] [--disk-seconds S] [--steady-gb G]
#   [--skip-gpu] [--skip-cpu] [--skip-ram] [--skip-disk]
#   [--skip-combined] [--skip-fullsoak] [--skip-net] [--skip-nvenc] [--skip-sys]
#   [--skip-gfx] [--skip-fan]
#
# Env: BENCH_PY overrides the Python interpreter (default: .venv/bin/python, else python3).
#      FURMARK overrides the FurMark 2 binary path (default: tools/furmark/FurMark_linux64/furmark).
#      GPU phases need PyTorch CUDA; on pre-Turing NVIDIA use torch cu126 wheels.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH="$ROOT/bench"
PY="${BENCH_PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

OUT="$ROOT/results"
GPU_SOAK=600; CPU_SOAK=120; RAM_SOAK=120
COMBINED_SECONDS=300; FULLSOAK_SECONDS=300; GFX_SECONDS=60; FAN_SECONDS=90
GPU_SIZE=12288; CPU_SIZE=4096; RAM_SIZE_MB=1024; FILL_PCT=70; PASSES=2
DISK_PATH="${TMPDIR:-/tmp}"; DISK_SIZE_MB=2048; DISK_SECONDS=15; STEADY_GB=10
DO_GPU=1; DO_CPU=1; DO_RAM=1; DO_DISK=1
DO_COMBINED=1; DO_FULLSOAK=1; DO_NET=1; DO_NVENC=1; DO_SYS=1; DO_GFX=1; DO_FAN=1

while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out-dir)      OUT="$2"; shift 2 ;;
        --gpu-soak)        GPU_SOAK="$2"; shift 2 ;;
        --cpu-soak)        CPU_SOAK="$2"; shift 2 ;;
        --ram-soak)        RAM_SOAK="$2"; shift 2 ;;
        --combined-seconds) COMBINED_SECONDS="$2"; shift 2 ;;
        --fullsoak-seconds) FULLSOAK_SECONDS="$2"; shift 2 ;;
        --gfx-seconds)     GFX_SECONDS="$2"; shift 2 ;;
        --fan-seconds)     FAN_SECONDS="$2"; shift 2 ;;
        --gpu-size)        GPU_SIZE="$2"; shift 2 ;;
        --cpu-size)        CPU_SIZE="$2"; shift 2 ;;
        --ram-size-mb)     RAM_SIZE_MB="$2"; shift 2 ;;
        --fill-pct)        FILL_PCT="$2"; shift 2 ;;
        --passes)          PASSES="$2"; shift 2 ;;
        --disk-path)       DISK_PATH="$2"; shift 2 ;;
        --disk-size-mb)    DISK_SIZE_MB="$2"; shift 2 ;;
        --disk-seconds)    DISK_SECONDS="$2"; shift 2 ;;
        --steady-gb)       STEADY_GB="$2"; shift 2 ;;
        --skip-gpu)        DO_GPU=0; shift ;;
        --skip-cpu)        DO_CPU=0; shift ;;
        --skip-ram)        DO_RAM=0; shift ;;
        --skip-disk)       DO_DISK=0; shift ;;
        --skip-combined)   DO_COMBINED=0; shift ;;
        --skip-fullsoak)   DO_FULLSOAK=0; shift ;;
        --skip-net)        DO_NET=0; shift ;;
        --skip-nvenc)      DO_NVENC=0; shift ;;
        --skip-sys)        DO_SYS=0; shift ;;
        --skip-gfx)        DO_GFX=0; shift ;;
        --skip-fan)        DO_FAN=0; shift ;;
        -h|--help)         sed -n '2,19p' "$0"; exit 0 ;;
        *)                 echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

HOST="$(hostname -s)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ISO="$(date -Iseconds)"
mkdir -p "$OUT" "$DISK_PATH"
BASE="$OUT/$HOST-linux-$STAMP"
LOGDIR="$BASE-logs"
TELEMETRY="$BASE.telemetry.csv"
JSONL="$(mktemp)"
mkdir -p "$LOGDIR"
TELE_PID=""
trap 'rm -f "$JSONL"; [ -n "$TELE_PID" ] && kill "$TELE_PID" 2>/dev/null' EXIT

echo "[env] python: $($PY -c 'import sys; print(sys.version.split()[0])')  out: $BASE.json|.md"
echo "[telemetry] $PY $BENCH/telemetry_log.py --out $TELEMETRY --interval 1"
"$PY" "$BENCH/telemetry_log.py" --out "$TELEMETRY" --interval 1 &
TELE_PID=$!
nvidia-smi -q > "$LOGDIR/nvidia-smi-q.start.txt" 2>&1 || true
sensors -j > "$LOGDIR/sensors.start.json" 2>&1 || true

run_phase() {
    local group="$1" script="$2" phase="$3"; shift 3
    echo "[$group] $phase"
    local out rc line
    out="$("$PY" "$BENCH/$script" "$phase" "$@" 2>&1)"; rc=$?
    printf '%s\n' "$out" | grep -v '^RESULT_JSON:' | grep -v '^$' | sed 's/^/    /'
    printf '%s\n' "$out" > "$LOGDIR/$group-$phase.log"
    line="$(printf '%s\n' "$out" | grep '^RESULT_JSON:' | tail -1 | sed 's/^RESULT_JSON://')"
    if [ -n "$line" ]; then
        printf '{"group":"%s","phase":"%s","result":%s}\n' "$group" "$phase" "$line" >> "$JSONL"
    else
        printf '{"group":"%s","phase":"%s","result":null,"exit":%d}\n' "$group" "$phase" "$rc" >> "$JSONL"
    fi
    [ "$rc" -ne 0 ] && echo "    [warn] $group/$phase exited $rc"
    return 0
}

if [ "$DO_GPU" = 1 ]; then
    run_phase gpu gpu_bench.py info
    run_phase gpu gpu_bench.py matmul --size "$GPU_SIZE" --seconds 10 --force
    run_phase gpu gpu_bench.py membw  --size "$GPU_SIZE" --seconds 8  --force
    run_phase gpu gpu_bench.py pcie   --size "$GPU_SIZE" --seconds 8  --force
    run_phase gpu gpu_bench.py conv   --size "$GPU_SIZE" --seconds 15 --force
    run_phase gpu gpu_bench.py integrity --size "$GPU_SIZE" --force
    run_phase gpu gpu_bench.py streams --size "$GPU_SIZE" --seconds 8 --force
    run_phase gpu gpu_bench.py soak   --size "$GPU_SIZE" --seconds "$GPU_SOAK" --force
fi

if [ "$DO_CPU" = 1 ]; then
    run_phase cpu cpu_bench.py info --size "$CPU_SIZE"
    run_phase cpu cpu_bench.py matmul --size "$CPU_SIZE" --seconds 10
    run_phase cpu cpu_bench.py membw  --size "$CPU_SIZE" --seconds 8
    run_phase cpu cpu_bench.py hash   --size "$CPU_SIZE" --seconds 8
    run_phase cpu cpu_bench.py compress --size "$CPU_SIZE" --seconds 6
    run_phase cpu cpu_bench.py aes    --size "$CPU_SIZE" --seconds 8
    run_phase cpu cpu_bench.py flops  --size "$CPU_SIZE" --seconds 8
    run_phase cpu cpu_bench.py soak   --size "$CPU_SIZE" --seconds "$CPU_SOAK"
fi

if [ "$DO_RAM" = 1 ]; then
    RAM_ARGS=(--size-mb "$RAM_SIZE_MB" --fill-pct "$FILL_PCT" --passes "$PASSES" --force)
    run_phase ram ram_bench.py info "${RAM_ARGS[@]}"
    run_phase ram ram_bench.py bandwidth "${RAM_ARGS[@]}" --seconds 10
    run_phase ram ram_bench.py latency "${RAM_ARGS[@]}"
    run_phase ram ram_bench.py integrity "${RAM_ARGS[@]}"
    run_phase ram ram_bench.py soak "${RAM_ARGS[@]}" --seconds "$RAM_SOAK"
fi

if [ "$DO_DISK" = 1 ]; then
    FSTYPE="$(df -T "$DISK_PATH" 2>/dev/null | awk 'NR==2 {print $2}')"
    if [ "$FSTYPE" = "tmpfs" ]; then
        # Live systems keep everything in RAM; a real-volume target keeps disk
        # tests (and the 10 GiB steady write) from filling memory. Prefer the
        # BENCHDATA partition of the live USB when one is present. Probe
        # directly (partx + blkid -p) so a missing udev label cannot hide it.
        BDEV=""
        for d in /dev/sd? /dev/nvme?n? /dev/mmcblk?; do
            [ -b "$d" ] && partx -a "$d" >/dev/null 2>&1 || true
        done
        udevadm settle --timeout=10 2>/dev/null || true
        [ -e "/dev/disk/by-label/${BENCHDATA_LABEL:-BENCHDATA}" ] && \
            BDEV="$(readlink -f "/dev/disk/by-label/${BENCHDATA_LABEL:-BENCHDATA}")"
        if [ -z "$BDEV" ]; then
            for p in /dev/sd*[0-9]; do
                [ -b "$p" ] || continue
                if [ "$(blkid -p -o value -s LABEL "$p" 2>/dev/null)" = "${BENCHDATA_LABEL:-BENCHDATA}" ]; then
                    BDEV="$p"; break
                fi
            done
        fi
        if [ -z "$BDEV" ]; then
            ROOTDEV="$(findmnt -no SOURCE /run/archiso/bootmnt 2>/dev/null || true)"
            for p in /dev/sd*[0-9]; do
                [ -b "$p" ] || continue
                sz="$(lsblk -bdno SIZE "$p" 2>/dev/null || echo 0)"
                [ "$(blkid -p -o value -s TYPE "$p" 2>/dev/null)" = "vfat" ] || continue
                [ "${sz:-0}" -gt 1000000000 ] || continue
                if udevadm info -q path -n "$p" 2>/dev/null | grep -q usb; then BDEV="$p"; break; fi
                PD="$(lsblk -no PKNAME "$p" 2>/dev/null | head -1)"
                if [ -n "$ROOTDEV" ] && [ -n "$PD" ]; then
                    RD="$(lsblk -no PKNAME "$ROOTDEV" 2>/dev/null | head -1)"; [ -z "$RD" ] && RD="${ROOTDEV#/dev/}"
                    [ "$PD" = "$RD" ] && { BDEV="$p"; break; }
                fi
            done
        fi
        if [ -n "$BDEV" ]; then
            modprobe vfat 2>/dev/null || true
            mkdir -p /mnt/benchdata
            mountpoint -q /mnt/benchdata || mount -t vfat -o rw,umask=0022 "$BDEV" /mnt/benchdata 2>/dev/null || true
            if mountpoint -q /mnt/benchdata; then
                DISK_PATH=/mnt/benchdata
                echo "    [info] disk path -> BENCHDATA ($BDEV) - not RAM-backed"
            else
                echo "    [warn] disk path $DISK_PATH is tmpfs and BENCHDATA could not be mounted"
            fi
        else
            echo "    [warn] disk path $DISK_PATH is tmpfs (RAM) -- pass --disk-path for a real volume"
        fi
    fi
    DISK_ARGS=(--path "$DISK_PATH" --size-mb "$DISK_SIZE_MB" --seconds "$DISK_SECONDS" --force)
    run_phase disk disk_bench.py info "${DISK_ARGS[@]}"
    run_phase disk disk_bench.py seqwrite "${DISK_ARGS[@]}"
    run_phase disk disk_bench.py seqread "${DISK_ARGS[@]}"
    run_phase disk disk_bench.py randread "${DISK_ARGS[@]}"
    run_phase disk disk_bench.py randwrite "${DISK_ARGS[@]}"
    run_phase disk disk_bench.py randmix "${DISK_ARGS[@]}"
    run_phase disk disk_bench.py latency "${DISK_ARGS[@]}"
    run_phase disk disk_bench.py steadywrite "${DISK_ARGS[@]}" --steady-gb "$STEADY_GB"
fi

if [ "$DO_NET" = 1 ]; then
    run_phase net net_bench.py bench
fi

if [ "$DO_NVENC" = 1 ]; then
    run_phase nvenc nvenc_bench.py bench
fi

if [ "$DO_SYS" = 1 ]; then
    run_phase sys linux_info.py info
fi

if [ "$DO_GFX" = 1 ]; then
    echo "[gfx] FurMark 2 (furmark-gl, 1920x1080, ${GFX_SECONDS}s)"
    FM="${FURMARK:-$ROOT/tools/furmark/FurMark_linux64/furmark}"
    if [ ! -x "$FM" ]; then
        echo "    furmark not found at $FM -- download FurMark 2 (linux64) from geeks3d.com into tools/, or set \$FURMARK"
        printf '{"group":"gfx","phase":"furmark","result":{"skipped":true,"reason":"furmark not installed"}}\n' >> "$JSONL"
    else
        # FurMark's X11 DPI probe segfaults when RESOURCE_MANAGER is missing (Xwayland here).
        xprop -root RESOURCE_MANAGER 2>/dev/null | grep -q RESOURCE_MANAGER || \
            xprop -root -f RESOURCE_MANAGER 8s -set RESOURCE_MANAGER '*dpi: 96' 2>/dev/null || true
        mkdir -p "$LOGDIR/furmark"
        GFX_OUT="$(cd "$(dirname "$FM")" && timeout $((GFX_SECONDS * 2 + 120)) ./furmark --demo furmark-gl \
            --width 1920 --height 1080 --benchmark --duration-ms "$((GFX_SECONDS * 1000))" \
            --no-score-box --vsync 0 --log-gpu-data --gpu-monitor-print \
            --export-dir "$LOGDIR/furmark" --hw-polling-interval 1000 2>&1)"; GFRC=$?
        printf '%s\n' "$GFX_OUT" > "$LOGDIR/gfx-furmark.log"
        printf '%s\n' "$GFX_OUT" | grep -a -v '^$' | sed 's/^/    /'
        cp "$(dirname "$FM")/_furmark_log.txt" "$LOGDIR/" 2>/dev/null || true
        cp "$(dirname "$FM")/_geexlab_log.txt" "$LOGDIR/" 2>/dev/null || true
        "$PY" - "$LOGDIR" "$JSONL" "$GFX_SECONDS" "$GFRC" <<'PYEOF'
import json, os, re, sys
logdir, jsonl, secs, rc = sys.argv[1:5]
text = ""
try:
    with open(os.path.join(logdir, "gfx-furmark.log"), errors="replace") as fh:
        text = fh.read()
except OSError:
    pass

def num(pattern, cast=float):
    m = re.search(pattern, text)
    return cast(m.group(1)) if m else None

csv = None
fdir = os.path.join(logdir, "furmark")
try:
    for name in sorted(os.listdir(fdir)):
        if name.lower().endswith(".csv"):
            csv = name
except OSError:
    pass
api = re.search(r"3D API\s*:\s*(.+)", text)
res = {"phase": "furmark", "demo": "furmark-gl", "width": 1920, "height": 1080,
       "seconds": int(secs), "exit": int(rc),
       "score": num(r"SCORE\s*:\s*(\d+)", int),
       "fps_min": num(r"FPS \(min/avg/max\)\s*:\s*([\d.]+)"),
       "fps_avg": num(r"FPS \(min/avg/max\)\s*:\s*[\d.]+\s*/\s*([\d.]+)"),
       "fps_max": num(r"FPS \(min/avg/max\)\s*:\s*[\d.]+\s*/\s*[\d.]+\s*/\s*([\d.]+)"),
       "temp_max_c": num(r"max temperature:\s*(\d+)", int),
       "api": api.group(1).strip() if api else None,
       "gpu_csv": csv}
if res["score"] is None and int(rc) != 0:
    res["skipped"] = True
    res["reason"] = "furmark exited %s" % rc
rec = {"group": "gfx", "phase": "furmark", "result": res}
with open(jsonl, "a") as fh:
    fh.write(json.dumps(rec) + "\n")
PYEOF
    fi
fi

if [ "$DO_FAN" = 1 ]; then
    echo "[fan] fan-response check (${FAN_SECONDS}s)"
    if [ -f /etc/bench-fan-curve.conf ]; then
        echo "    curve: $(grep '^CURVE=' /etc/bench-fan-curve.conf | head -1 | cut -d= -f2-)"
    else
        echo "    curve: automatic"
    fi
    run_phase fan fan_curve.py check --seconds "$FAN_SECONDS" --size 8192
fi

if [ "$DO_COMBINED" = 1 ]; then
    echo "[combined] gpu soak + cpu soak (${COMBINED_SECONDS}s)"
    D="$(mktemp -d)"
    "$PY" "$BENCH/gpu_bench.py" soak --size "$GPU_SIZE" --seconds "$COMBINED_SECONDS" --force >"$D/gpu.out" 2>&1 &
    GPID=$!
    "$PY" "$BENCH/cpu_bench.py" soak --size "$CPU_SIZE" --seconds "$COMBINED_SECONDS" >"$D/cpu.out" 2>&1 &
    CPID=$!
    wait "$GPID"; GRC=$?
    wait "$CPID"; CRC=$?
    sed 's/^/    /' "$D/gpu.out" | grep -v 'RESULT_JSON' | grep -v '^    $' || true
    sed 's/^/    /' "$D/cpu.out" | grep -v 'RESULT_JSON' | grep -v '^    $' || true
    "$PY" - "$D" "$JSONL" "$COMBINED_SECONDS" "$GRC" "$CRC" <<'PYEOF'
import json, os, sys
d, jsonl, secs, grc, crc = sys.argv[1:6]

def res(name):
    try:
        with open(os.path.join(d, name)) as fh:
            for line in fh:
                if line.startswith("RESULT_JSON:"):
                    return json.loads(line.split(":", 1)[1])
    except OSError:
        pass
    return None

gpu, cpu = res("gpu.out"), res("cpu.out")
rec = {"group": "combined", "phase": "stress",
       "result": {"seconds": int(secs), "gpu_exit": int(grc), "cpu_exit": int(crc),
                  "gpu": gpu, "cpu": cpu,
                  "telemetry": (gpu or {}).get("telemetry")}}
with open(jsonl, "a") as fh:
    fh.write(json.dumps(rec) + "\n")
PYEOF
    cp "$D/gpu.out" "$LOGDIR/combined-gpu.log" 2>/dev/null || true
    cp "$D/cpu.out" "$LOGDIR/combined-cpu.log" 2>/dev/null || true
    rm -rf "$D"
fi

if [ "$DO_FULLSOAK" = 1 ]; then
    echo "[fullsoak] gpu + cpu + ram + disk concurrently (${FULLSOAK_SECONDS}s)"
    D="$(mktemp -d)"
    "$PY" "$BENCH/gpu_bench.py" soak --size "$GPU_SIZE" --seconds "$FULLSOAK_SECONDS" --force >"$D/gpu.out" 2>&1 &
    GPID=$!
    "$PY" "$BENCH/cpu_bench.py" soak --size "$CPU_SIZE" --seconds "$FULLSOAK_SECONDS" >"$D/cpu.out" 2>&1 &
    CPID=$!
    "$PY" "$BENCH/ram_bench.py" soak --size-mb "$RAM_SIZE_MB" --fill-pct "$FILL_PCT" --passes "$PASSES" --force --seconds "$FULLSOAK_SECONDS" >"$D/ram.out" 2>&1 &
    RPID=$!
    "$PY" "$BENCH/disk_bench.py" seqwrite --path "$DISK_PATH" --size-mb "$DISK_SIZE_MB" --seconds "$FULLSOAK_SECONDS" --force >"$D/disk.out" 2>&1 &
    DPID=$!
    wait "$GPID"; GRC=$?
    wait "$CPID"; CRC=$?
    wait "$RPID"; RRC=$?
    wait "$DPID"; DRC=$?
    for f in gpu cpu ram disk; do
        sed 's/^/    /' "$D/$f.out" | grep -v 'RESULT_JSON' | grep -v '^    $' || true
    done
    XID="$(journalctl -k --since "@$(( $(date +%s) - FULLSOAK_SECONDS - 120 ))" --no-pager 2>/dev/null | grep -ciE 'xid' || true)"
    "$PY" - "$D" "$JSONL" "$FULLSOAK_SECONDS" "$GRC" "$CRC" "$RRC" "$DRC" "$XID" <<'PYEOF'
import json, os, sys
d, jsonl, secs, grc, crc, rrc, drc, xid = sys.argv[1:9]

def res(name):
    try:
        with open(os.path.join(d, name)) as fh:
            for line in fh:
                if line.startswith("RESULT_JSON:"):
                    return json.loads(line.split(":", 1)[1])
    except OSError:
        pass
    return None

gpu, cpu, ram, disk = res("gpu.out"), res("cpu.out"), res("ram.out"), res("disk.out")
exits = [int(grc), int(crc), int(rrc), int(drc)]
rec = {"group": "fullsoak", "phase": "stress",
       "result": {"seconds": int(secs), "gpu": gpu, "cpu": cpu, "ram": ram, "disk": disk,
                  "telemetry": (gpu or {}).get("telemetry"),
                  "verdict": {"clean": all(e == 0 for e in exits) and int(xid) == 0,
                              "exits": exits, "xid_lines": int(xid)}}}
with open(jsonl, "a") as fh:
    fh.write(json.dumps(rec) + "\n")
PYEOF
    cp "$D/gpu.out" "$LOGDIR/fullsoak-gpu.log" 2>/dev/null || true
    cp "$D/cpu.out" "$LOGDIR/fullsoak-cpu.log" 2>/dev/null || true
    cp "$D/ram.out" "$LOGDIR/fullsoak-ram.log" 2>/dev/null || true
    cp "$D/disk.out" "$LOGDIR/fullsoak-disk.log" 2>/dev/null || true
    rm -rf "$D"
fi

kill "$TELE_PID" 2>/dev/null; wait "$TELE_PID" 2>/dev/null; TELE_PID=""
nvidia-smi -q > "$LOGDIR/nvidia-smi-q.end.txt" 2>&1 || true
sensors -j > "$LOGDIR/sensors.end.json" 2>&1 || true
journalctl -k --since "$ISO" --no-pager > "$LOGDIR/kernel-since-start.log" 2>&1 || true
journalctl -p err --since "$ISO" --no-pager > "$LOGDIR/errors-since-start.log" 2>&1 || true

"$PY" - "$JSONL" "$BASE.json" "$BASE.md" "$HOST" "$ISO" "$PY" "$ROOT" "$TELEMETRY" "$LOGDIR" <<'PYEOF'
import json, os, subprocess, sys, platform
jsonl, json_out, md_out, host, iso, py, root, telemetry_csv, logdir = sys.argv[1:10]

recs = []
with open(jsonl) as fh:
    for line in fh:
        line = line.strip()
        if line:
            recs.append(json.loads(line))

env = {"python": py}
try:
    import torch
    env["torch"] = torch.__version__
    env["torch_cuda"] = torch.version.cuda
    env["cuda_available"] = torch.cuda.is_available()
except Exception:
    pass
try:
    env["git_commit"] = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                                       capture_output=True, text=True, timeout=10).stdout.strip()
    env["git_dirty"] = bool(subprocess.run(["git", "-C", root, "status", "--porcelain"],
                                           capture_output=True, text=True, timeout=10).stdout.strip())
except Exception:
    pass

telemetry = None
try:
    with open(telemetry_csv) as fh:
        tlines = [l.strip() for l in fh if l.strip()]
    if len(tlines) > 1:
        thdr = tlines[0].split(",")
        tcol, fcol, pcol, ecol = (thdr.index("temp_c"), thdr.index("fan_pct"),
                                  thdr.index("power_w"), thdr.index("elapsed_s"))
        temps, fans, pw, last_el = [], [], [], 0.0
        for line in tlines[1:]:
            v = line.split(",")
            try:
                temps.append(float(v[tcol])); fans.append(float(v[fcol])); pw.append(float(v[pcol]))
                last_el = float(v[ecol])
            except (ValueError, IndexError):
                pass
        if temps:
            telemetry = {"file": os.path.basename(telemetry_csv), "samples": len(temps),
                         "duration_s": round(last_el, 1),
                         "temp_c": [min(temps), round(sum(temps) / len(temps), 1), max(temps)],
                         "fan_pct": [min(fans), round(sum(fans) / len(fans), 1), max(fans)],
                         "power_w": [round(sum(pw) / len(pw), 1), max(pw)]}
except OSError:
    pass

fan_conf = None
try:
    with open("/etc/bench-fan-curve.conf") as fh:
        for line in fh:
            if line.startswith("CURVE="):
                fan_conf = line.split("=", 1)[1].strip().strip('"')
except OSError:
    pass

report = {
    "host": host,
    "timestamp": iso,
    "platform": platform.platform(),
    "env": env,
    "fan_curve": fan_conf,
    "telemetry": telemetry,
    "log_dir": logdir,
    "phases": recs,
}
with open(json_out, "w") as fh:
    json.dump(report, fh, indent=2)
    fh.write("\n")

def by(group, phase):
    for r in recs:
        if r["group"] == group and r["phase"] == phase:
            return r
    return None

def err(r):
    if r is None:
        return "not run"
    if r.get("result") is None:
        return "error (exit %s)" % r.get("exit", "?")
    return None

md = []
md.append("# Linux bench - %s" % host)
md.append("")
md.append("Generated: %s" % iso)
md.append("")
md.append("PyTorch %s (CUDA %s) | %s" % (env.get("torch"), env.get("torch_cuda"), env.get("python")))
md.append("")
md.append("Fan curve: %s" % ("`%s`" % fan_conf if fan_conf else "automatic"))
md.append("")

def table(title, rows):
    md.append("## %s" % title)
    md.append("")
    md.append("| phase | result |")
    md.append("|---|---|")
    md.extend(rows)
    md.append("")

def fmt(x):
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return ("%g" % round(x, 2))
    return str(x)

gpu = {}
for p in ("info", "matmul", "membw", "pcie", "conv", "integrity", "streams", "soak"):
    r = by("gpu", p)
    gpu[p] = r["result"] if r and r.get("result") else None
if any(gpu.values()):
    rows = []
    i = gpu["info"]
    if i:
        rows.append("| info | %s | CUDA %s | SMs %s | VRAM %s GiB |" % (
            i.get("device"), i.get("cuda"), i.get("sm_count"), i.get("vram_gib")))
    if gpu["matmul"]:
        for k, v in gpu["matmul"].get("results", {}).items():
            rows.append("| matmul | %s %s TFLOPS |" % (k, v.get("tflops")))
    if gpu["membw"]:
        for k, v in gpu["membw"].get("results", {}).items():
            rows.append("| membw | %s add %s / reduce %s GB/s |" % (
                k, v.get("add_gbps"), v.get("reduce_gbps")))
    if gpu["pcie"]:
        rows.append("| pcie | H2D %s / D2H %s GB/s |" % (
            gpu["pcie"].get("h2d_gbps"), gpu["pcie"].get("d2h_gbps")))
    if gpu["conv"]:
        c = gpu["conv"]
        rows.append("| conv2d | %s |" % ("skipped" if c.get("skipped") else "%s TFLOPS" % c.get("tflops")))
    if gpu["integrity"]:
        v = gpu["integrity"]
        rows.append("| vram | %s GiB, mismatched=%s bad=%s |" % (
            v.get("gib"), v.get("float_mismatched"), v.get("int_bad_elements")))
    if gpu["streams"]:
        s = gpu["streams"]
        if s.get("skipped"):
            rows.append("| streams | skipped (%s) |" % s.get("reason", ""))
        else:
            for k, tf in s.get("results", {}).items():
                rows.append("| streams (%s) | %s TFLOPS |" % (k, tf))
    if gpu["soak"]:
        s = gpu["soak"]
        if s.get("skipped"):
            rows.append("| soak | skipped (%s) |" % s.get("reason", ""))
        else:
            txt = "%s TFLOPS over %ss" % (s.get("matmul_tflops"), s.get("seconds"))
            t = s.get("telemetry")
            if t:
                txt += " · %s/%s/%s °C · fan %s/%s/%s %% · %s W" % (
                    t["temp_c"][0], t["temp_c"][1], t["temp_c"][2],
                    t["fan_pct"][0], t["fan_pct"][1], t["fan_pct"][2], t["power_w"][0])
            rows.append("| soak | %s |" % txt)
    table("GPU", rows)

cpu = {}
for p in ("info", "matmul", "membw", "hash", "compress", "aes", "flops", "soak"):
    r = by("cpu", p)
    cpu[p] = r["result"] if r and r.get("result") else None
if any(cpu.values()):
    rows = []
    if cpu["info"]:
        rows.append("| info | logical_cpus %s | numpy %s |" % (
            cpu["info"].get("logical_cpus"), cpu["info"].get("numpy")))
    if cpu["matmul"]:
        for k, v in cpu["matmul"].get("results", {}).items():
            rows.append("| matmul | %s %s GFLOPS |" % (k, v.get("gflops")))
    if cpu["membw"]:
        m = cpu["membw"].get("results", {})
        rows.append("| membw | copy %s / add %s / reduce %s GB/s |" % (
            m.get("copy_gbps"), m.get("add_gbps"), m.get("reduce_gbps")))
    if cpu["hash"]:
        rows.append("| sha256 | %s MB/s |" % cpu["hash"].get("sha256_mbps"))
    if cpu["compress"]:
        c = cpu["compress"]
        rows.append("| compression | zlib c/d %s/%s, lzma c/d %s/%s MB/s |" % (
            c.get("zlib_compress_mbps"), c.get("zlib_decompress_mbps"),
            c.get("lzma_compress_mbps"), c.get("lzma_decompress_mbps")))
    if cpu["aes"] and not cpu["aes"].get("skipped"):
        rows.append("| AES-256-GCM | %s MB/s |" % cpu["aes"].get("aes256gcm_mbps"))
    if cpu["flops"]:
        rows.append("| flops fp32 | %s GFLOPS |" % cpu["flops"].get("gflops"))
    if cpu["soak"]:
        rows.append("| soak | %s GFLOPS over %ss |" % (
            cpu["soak"].get("gflops"), cpu["soak"].get("seconds")))
    table("CPU", rows)

ram = {}
for p in ("info", "bandwidth", "latency", "integrity", "soak"):
    r = by("ram", p)
    ram[p] = r["result"] if r and r.get("result") else None
if any(ram.values()):
    rows = []
    if ram["info"]:
        rows.append("| info | total %s GiB, available %s GiB, cpus %s |" % (
            ram["info"].get("total_gib"), ram["info"].get("available_gib"),
            ram["info"].get("logical_cpus")))
    if ram["bandwidth"]:
        for k, v in ram["bandwidth"].get("results", {}).items():
            rows.append("| bandwidth %s | %s GB/s |" % (k, v))
    if ram["latency"]:
        rows.append("| latency | ~%s ns/access, gather %s GB/s |" % (
            ram["latency"].get("ns_per_access_approx"), ram["latency"].get("gather_gbps_approx")))
    if ram["integrity"]:
        v = ram["integrity"]
        rows.append("| integrity | %s GiB, passes %s, bad %s |" % (
            v.get("giB"), v.get("passes"), v.get("bad_elements")))
    if ram["soak"]:
        rows.append("| soak | %s GB/s over %ss, errors %s |" % (
            ram["soak"].get("gbps"), ram["soak"].get("seconds"), ram["soak"].get("errors")))
    table("RAM", rows)

disk = {}
for p in ("info", "seqwrite", "seqread", "randread", "randwrite", "randmix", "latency", "steadywrite"):
    r = by("disk", p)
    disk[p] = r["result"] if r and r.get("result") else None
if any(disk.values()):
    rows = []
    if disk["info"]:
        rows.append("| info | %s total %s GiB free %s GiB |" % (
            disk["info"].get("path"), disk["info"].get("total_gib"), disk["info"].get("free_gib")))
    if disk["seqwrite"]:
        rows.append("| seq write | %s MB/s (%ss) |" % (
            disk["seqwrite"].get("mbps"), disk["seqwrite"].get("seconds")))
    if disk["seqread"]:
        rows.append("| seq read | %s MB/s (%ss) |" % (
            disk["seqread"].get("mbps"), disk["seqread"].get("seconds")))
    if disk["randread"]:
        rows.append("| rand 4K read | %s IOPS, %s MB/s |" % (
            disk["randread"].get("iops"), disk["randread"].get("mbps")))
    if disk["randwrite"]:
        rows.append("| rand 4K write | %s IOPS, %s MB/s |" % (
            disk["randwrite"].get("iops"), disk["randwrite"].get("mbps")))
    if disk["randmix"]:
        rows.append("| rand 4K mix (70/30) | %s IOPS, %s MB/s |" % (
            disk["randmix"].get("iops"), disk["randmix"].get("mbps")))
    if disk["latency"]:
        v = disk["latency"]
        rows.append("| 4K latency | %s IOPS, p50 %s/p99 %s/p99.9 %s us |" % (
            v.get("iops"), v.get("us_p50"), v.get("us_p99"), v.get("us_p999")))
    if disk["steadywrite"]:
        v = disk["steadywrite"]
        rows.append("| steady-state write (%s GiB) | first %s -> last %s MB/s (drop %s%%) |" % (
            v.get("gib"), v.get("first10_mbps"), v.get("last10_mbps"), v.get("drop_pct")))
    table("Disk", rows)

comb = by("combined", "stress")
if comb and comb.get("result"):
    r = comb["result"]; g = r.get("gpu") or {}; c = r.get("cpu") or {}
    rows = ["| duration | %ss |" % r.get("seconds")]
    if g.get("matmul_tflops") is not None:
        rows.append("| gpu soak | %s TFLOPS |" % g.get("matmul_tflops"))
    if c.get("gflops") is not None:
        rows.append("| cpu soak | %s GFLOPS |" % c.get("gflops"))
    t = r.get("telemetry") or {}
    if t:
        rows.append("| thermals | %s/%s/%s °C · fan %s/%s/%s %% · %s W |" % (
            t["temp_c"][0], t["temp_c"][1], t["temp_c"][2],
            t["fan_pct"][0], t["fan_pct"][1], t["fan_pct"][2], t["power_w"][0]))
    table("Combined stress (GPU+CPU)", rows)

fs = by("fullsoak", "stress")
if fs and fs.get("result"):
    r = fs["result"]; g = r.get("gpu") or {}; c = r.get("cpu") or {}
    m = r.get("ram") or {}; d = r.get("disk") or {}
    rows = ["| duration | %ss |" % r.get("seconds")]
    rows.append("| gpu / cpu | %s TFLOPS / %s GFLOPS |" % (g.get("matmul_tflops"), c.get("gflops")))
    rows.append("| ram / disk | %s GB/s (%s errors) / %s MB/s |" % (
        m.get("gbps"), m.get("errors"), d.get("mbps")))
    t = r.get("telemetry") or {}
    if t:
        rows.append("| thermals | %s/%s/%s °C · fan %s/%s/%s %% · %s W |" % (
            t["temp_c"][0], t["temp_c"][1], t["temp_c"][2],
            t["fan_pct"][0], t["fan_pct"][1], t["fan_pct"][2], t["power_w"][0]))
    v = r.get("verdict") or {}
    rows.append("| verdict | %s (exit codes %s, xid lines %s) |" % (
        "clean" if v.get("clean") else "check", v.get("exits"), v.get("xid_lines")))
    table("Full-system soak (GPU+CPU+RAM+disk)", rows)

netrec = by("net", "bench")
if netrec and netrec.get("result"):
    r = netrec["result"]; p = r.get("ping_ms") or {}
    rows = []
    if p:
        rows.append("| ping min/avg/max | %s / %s / %s ms |" % (p.get("min"), p.get("avg"), p.get("max")))
    rows.append("| download | %s Mbps |" % fmt(r.get("download_mbps")))
    rows.append("| upload | %s Mbps |" % fmt(r.get("upload_mbps")))
    table("Network", rows)

nvenc = by("nvenc", "bench")
if nvenc and nvenc.get("result"):
    nv = nvenc["result"]
    rows = []
    for codec, v in (nv.get("results") or {}).items():
        if v.get("skipped"):
            rows.append("| %s | skipped (%s) |" % (codec, v.get("reason", "")))
        else:
            rows.append("| %s | %s fps |" % (codec, v.get("fps")))
    table("NVENC (%sx%s)" % (nv.get("width"), nv.get("height")), rows)

sysrec = by("sys", "info")
if sysrec and sysrec.get("result"):
    r = sysrec["result"]; c = r.get("cpu") or {}; g = r.get("gpu") or {}
    rows = ["| os | %s · kernel %s |" % (r.get("distro"), r.get("kernel"))]
    rows.append("| cpu | %s (%s threads) |" % (c.get("model"), c.get("logical_cpus")))
    rows.append("| memory | %s GiB |" % r.get("memory_gib"))
    rows.append("| gpu | %s · driver %s · PCIe %s x%s |" % (
        g.get("name"), g.get("driver"), g.get("pcie_link_gen"), g.get("pcie_link_width")))
    rows.append("| storage | %s |" % "; ".join(r.get("storage") or []))
    rows.append("| boot | %s |" % r.get("booted"))
    rows.append("| errors since boot | %s |" % r.get("errors_since_boot"))
    table("System", rows)

gfx = by("gfx", "furmark")
if gfx and gfx.get("result"):
    r = gfx["result"]
    if r.get("skipped"):
        table("Graphics (FurMark 2)", ["| furmark | skipped (%s) |" % r.get("reason", "")])
    else:
        rows = ["| demo | %s %sx%s (%ss) |" % (
            r.get("demo"), r.get("width"), r.get("height"), r.get("seconds"))]
        rows.append("| score / fps | %s · %s/%s/%s fps |" % (
            r.get("score"), r.get("fps_min"), r.get("fps_avg"), r.get("fps_max")))
        rows.append("| max temp | %s °C |" % r.get("temp_max_c"))
        rows.append("| api | %s |" % r.get("api"))
        if r.get("gpu_csv"):
            rows.append("| gpu data | `furmark/%s` |" % r.get("gpu_csv"))
        table("Graphics (FurMark 2)", rows)

fanrec = by("fan", "check")
if fanrec and fanrec.get("result"):
    r = fanrec["result"]
    rows = ["| verdict | %s |" % r.get("verdict"),
            "| temp start -> max | %s -> %s °C |" % (r.get("temp_start"), r.get("temp_max")),
            "| fan start -> max | %s %s -> %s %s |" % (
                r.get("fan_start_pct"), "%", r.get("fan_max_pct"), "%")]
    if r.get("fan_max_rpm"):
        rows.append("| fan rpm max | %s |" % r.get("fan_max_rpm"))
    table("Fan response check", rows)

if telemetry:
    md.append("## Telemetry")
    md.append("")
    md.append("1 Hz sampler — `%s` (%s samples over %ss):" % (
        telemetry["file"], telemetry["samples"], telemetry["duration_s"]))
    md.append("")
    md.append("| metric | min / avg / max |")
    md.append("|---|---|")
    md.append("| gpu temp °C | %s / %s / %s |" % tuple(telemetry["temp_c"]))
    md.append("| gpu fan %% | %s / %s / %s |" % tuple(telemetry["fan_pct"]))
    md.append("| gpu power W | — / %s / %s |" % tuple(telemetry["power_w"]))
    md.append("")

md.append("## Artifacts")
md.append("")
md.append("- `%s` — machine-readable report" % os.path.basename(json_out))
md.append("- `%s` — this report" % os.path.basename(md_out))
if telemetry:
    md.append("- `%s` — telemetry time series (1 Hz CSV)" % telemetry["file"])
md.append("- `%s/` — raw per-phase stdout/stderr logs" % os.path.basename(logdir))
md.append("- `runs-index.jsonl` — append-only run index (one line per run)")
md.append("")

sys_gpu = ((sysrec or {}).get("result") or {}).get("gpu") or {}
idx = {
    "host": host, "timestamp": iso, "platform": platform.platform(),
    "report": json_out,
    "git": env.get("git_commit"), "dirty": env.get("git_dirty"),
    "torch": env.get("torch"), "python": env.get("python"),
    "gpu": sys_gpu.get("name") or (gpu.get("info") or {}).get("device"),
    "driver": sys_gpu.get("driver"),
    "fan_curve": fan_conf,
    "gpu_matmul_tflops": {k: v.get("tflops") for k, v in (gpu["matmul"] or {}).get("results", {}).items()},
    "gpu_soak_tflops": (gpu["soak"] or {}).get("matmul_tflops"),
    "gpu_soak_telemetry": (gpu["soak"] or {}).get("telemetry"),
    "cpu_matmul_gflops": {k: v.get("gflops") for k, v in (cpu["matmul"] or {}).get("results", {}).items()},
    "cpu_soak_gflops": (cpu["soak"] or {}).get("gflops"),
    "ram_soak_gbps": (ram["soak"] or {}).get("gbps"),
    "disk_seqwrite_mbps": (disk["seqwrite"] or {}).get("mbps"),
    "disk_steady": disk["steadywrite"],
    "net": (netrec or {}).get("result"),
    "combined": comb,
    "fullsoak": fs,
    "telemetry": telemetry,
}
with open(os.path.join(os.path.dirname(json_out), "runs-index.jsonl"), "a") as fh:
    fh.write(json.dumps(idx) + "\n")

md.append("_Linux run via Run-LinuxBench.sh; disk phases use OS-buffered I/O._")
with open(md_out, "w") as fh:
    fh.write("\n".join(md) + "\n")

print("[done] wrote %s and %s" % (json_out, md_out))
PYEOF
