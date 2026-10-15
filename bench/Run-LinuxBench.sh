#!/usr/bin/env bash
# Run-LinuxBench.sh - Linux runner for the bench-test Python phases
# (GPU/CPU/RAM/disk + combined, full-system soak, network, NVENC, LLM, inventory).
# Writes results/<host>-linux-<stamp>.json and .md, mirroring the PowerShell invokers' metrics.
#
# Usage: ./Run-LinuxBench.sh [-o OutDir] [--gpu-soak S] [--cpu-soak S] [--ram-soak S]
#   [--combined-seconds S] [--fullsoak-seconds S] [--gfx-seconds S] [--fan-seconds S]
#   [--gpu-size N] [--cpu-size N] [--ram-size-mb N] [--fill-pct N] [--passes N]
#   [--disk-path DIR] [--disk-size-mb N] [--disk-seconds S] [--steady-gb G]
#   [--idle-seconds S] [--skip-gpu] [--skip-cpu] [--skip-ram] [--skip-disk]
#   [--skip-combined] [--skip-fullsoak] [--skip-net] [--skip-nvenc] [--skip-sys]
#   [--skip-llm] [--skip-gfx] [--skip-fan] [--skip-idle]
#   [--soak-until-stable] [--stable-hold S] [--gfx-engine furmark|glmark2]
#
# Env: BENCH_PY overrides the Python interpreter (default: .venv/bin/python, else python3).
#      FURMARK overrides the FurMark 2 binary path (default: tools/furmark/FurMark_linux64/furmark).
#      GPU phases need PyTorch CUDA; on pre-Turing NVIDIA use torch cu126 wheels.
#      BENCH_OLLAMA_URL overrides the Ollama base URL (default: http://127.0.0.1:11434).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH="$ROOT/bench"
PY="${BENCH_PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

# ROCm ships kernels for the discrete RDNA3/3.5 targets but not for the APU
# iGPUs, which report gfx1103 (Phoenix - e.g. Radeon 780M/880M) or gfx1150/1151
# (Strix). With no override the ROCm runtime refuses the device and every GPU
# phase errors out (torch.cuda.is_available() is False), so present the closest
# supported target. Only when ROCm is actually present; a user override wins.
if [ -z "${HSA_OVERRIDE_GFX_VERSION:-}" ] && command -v rocminfo >/dev/null 2>&1; then
    _gfx="$(rocminfo 2>/dev/null | grep -oE 'gfx[0-9]+' | head -1)"
    case "$_gfx" in
        gfx1103)      export HSA_OVERRIDE_GFX_VERSION=11.0.0 ;;
        gfx1150|gfx1151) export HSA_OVERRIDE_GFX_VERSION=11.5.0 ;;
    esac
    [ -n "${HSA_OVERRIDE_GFX_VERSION:-}" ] && \
        echo "[gpu] ROCm: $_gfx iGPU -> HSA_OVERRIDE_GFX_VERSION=$HSA_OVERRIDE_GFX_VERSION"
fi

# Hybrid live image: the CUDA PyTorch build is baked to /opt/bench-pypackages and
# the system carries ROCm PyTorch. Select the CUDA tree only on an NVIDIA host so
# it shadows ROCm there while AMD keeps the system build. (Harmless on the single
# -vendor images: /opt/bench-pypackages is absent on amd, already on the .pth for
# nvidia.) A user PYTHONPATH that already names it wins.
if [ -d /opt/bench-pypackages ] && command -v nvidia-smi >/dev/null 2>&1 \
        && case ":${PYTHONPATH:-}:" in *":/opt/bench-pypackages:"*) false ;; *) true ;; esac; then
    export PYTHONPATH="/opt/bench-pypackages${PYTHONPATH:+:$PYTHONPATH}"
fi

OUT="$ROOT/results"
GPU_SOAK=600; CPU_SOAK=120; RAM_SOAK=120
COMBINED_SECONDS=300; FULLSOAK_SECONDS=300; GFX_SECONDS=60; FAN_SECONDS=90
GPU_SIZE=12288; CPU_SIZE=4096; RAM_SIZE_MB=1024; FILL_PCT=70; PASSES=2
DISK_PATH="${TMPDIR:-/tmp}"; DISK_SIZE_MB=2048; DISK_SECONDS=15; STEADY_GB=10
DISK_PATH_SET=0
DO_GPU=1; DO_CPU=1; DO_RAM=1; DO_DISK=1
DO_COMBINED=1; DO_FULLSOAK=1; DO_NET=1; DO_NVENC=1; DO_SYS=1; DO_GFX=1; DO_FAN=1
DO_IDLE=1; DO_LLM=1; IDLE_SECONDS=60
STABLE_ARG=""; STABLE_HOLD=60; GFX_ENGINE=furmark

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
        --disk-path)       DISK_PATH="$2"; DISK_PATH_SET=1; shift 2 ;;
        --disk-size-mb)    DISK_SIZE_MB="$2"; shift 2 ;;
        --disk-seconds)    DISK_SECONDS="$2"; shift 2 ;;
        --steady-gb)       STEADY_GB="$2"; shift 2 ;;
        --idle-seconds)    IDLE_SECONDS="$2"; shift 2 ;;
        --skip-gpu)        DO_GPU=0; shift ;;
        --skip-cpu)        DO_CPU=0; shift ;;
        --skip-ram)        DO_RAM=0; shift ;;
        --skip-disk)       DO_DISK=0; shift ;;
        --skip-llm)        DO_LLM=0; shift ;;
        --skip-combined)   DO_COMBINED=0; shift ;;
        --skip-fullsoak)   DO_FULLSOAK=0; shift ;;
        --skip-net)        DO_NET=0; shift ;;
        --skip-nvenc)      DO_NVENC=0; shift ;;
        --skip-sys)        DO_SYS=0; shift ;;
        --skip-gfx)        DO_GFX=0; shift ;;
        --skip-fan)        DO_FAN=0; shift ;;
        --skip-idle)       DO_IDLE=0; shift ;;
    --soak-until-stable) STABLE_ARG="--until-stable --stable-hold $STABLE_HOLD"; shift ;;
    --stable-hold)     STABLE_HOLD="$2"; STABLE_ARG="--until-stable --stable-hold $STABLE_HOLD"; shift 2 ;;
    --gfx-engine)      GFX_ENGINE="$2"; shift 2 ;;
        -h|--help)         sed -n '2,18p' "$0"; exit 0 ;;
        *)                 echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

HOST="$(hostname -s 2>/dev/null)"
[ -n "$HOST" ] || HOST="bench-live"
STAMP="$(date +%Y%m%d-%H%M%S)"
ISO="$(date -Iseconds)"
mkdir -p "$OUT" "$DISK_PATH"
BASE="$OUT/$HOST-linux-$STAMP"
LOGDIR="$BASE-logs"
TELEMETRY="$BASE.telemetry.csv"
JSONL="$(mktemp)"
mkdir -p "$LOGDIR"
TELE_PID=""
trap 'cp -f "$JSONL" "$LOGDIR/records.jsonl" 2>/dev/null; rm -f "$JSONL"; [ -n "$TELE_PID" ] && kill "$TELE_PID" 2>/dev/null' EXIT

echo "[env] python: $($PY -c 'import sys; print(sys.version.split()[0])')  out: $BASE.json|.md"
echo "[telemetry] $PY $BENCH/telemetry_log.py --out $TELEMETRY --interval 0.25 --gpu-interval 1.0 --slow-interval 1.0"
# 4Hz rows; nvidia-smi (~66ms/call) at 2Hz and sensors/nvidia-settings at 1Hz,
# so the row rate is actually delivered instead of being capped at ~3/s
"$PY" "$BENCH/telemetry_log.py" --out "$TELEMETRY" --interval 0.25 \
     --gpu-interval 1.0 --slow-interval 1.0 &
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

# ---- idle baseline ---------------------------------------------------------
# The machine at rest, captured before any load runs: every telemetry row the
# sampler has written so far is an idle row, which gives the soak thermals,
# power, clocks and fan a floor to compare against. No load is started here.
# --skip-idle omits it (a quick check does not need the baseline).
# >>> idle baseline >>>
if [ "$DO_IDLE" = 1 ]; then
    echo "[idle] baseline (${IDLE_SECONDS}s, no load)"
    sleep "$IDLE_SECONDS"
    "$PY" - "$TELEMETRY" "$JSONL" "$IDLE_SECONDS" <<'PYEOF'
import csv, json, sys

telemetry, jsonl, seconds = sys.argv[1:4]


def series(col):
    """Every readable value of `col`; a blank cell is skipped, not averaged as 0."""
    vals = []
    try:
        with open(telemetry, errors="replace") as fh:
            text = fh.read()
    except OSError:
        return vals
    if text and not text.endswith("\n"):
        text = text.rsplit("\n", 1)[0]      # the sampler's in-flight row
    for row in csv.DictReader(text.splitlines()):
        try:
            vals.append(float(row.get(col)))
        except (TypeError, ValueError):
            pass
    return vals


# Linux sampler columns -> cross-platform idle_* keys. cpu_package_c is this
# platform's CPU temp column; the summary names it cpu_temp_c like Windows.
fields = (("temp_c", "temp_c"), ("fan_pct", "fan_pct"), ("power_w", "power_w"),
          ("sm_mhz", "sm_mhz"), ("cpu_package_c", "cpu_temp_c"),
          ("cpu_mhz", "cpu_mhz"), ("cpu_pkg_w", "cpu_pkg_w"),
          ("gpu_fan_rpm", "gpu_fan_rpm"))
res = {"phase": "baseline", "seconds": int(float(seconds))}
for col, key in fields:
    vals = series(col)
    if vals:
        res[key] = [min(vals), round(sum(vals) / len(vals), 1), max(vals)]
        res[key + "_n"] = len(vals)
res["samples"] = res.get("temp_c_n") or 0
if not any(key in res for _col, key in fields):
    res["skipped"] = True
    res["reason"] = "no telemetry samples in the idle window"
with open(jsonl, "a") as fh:
    fh.write(json.dumps({"group": "idle", "phase": "baseline", "result": res}) + "\n")
if res.get("skipped"):
    print("    skipped: %s" % res["reason"])
else:
    bits = []
    for key, unit in (("temp_c", "C"), ("fan_pct", "%"), ("power_w", "W"),
                      ("sm_mhz", "MHz")):
        v = res.get(key)
        if v:
            bits.append("%s %s/%s/%s %s" % (key, v[0], v[1], v[2], unit))
    print("    %s samples: %s" % (res["samples"], ", ".join(bits) or "no readings"))
PYEOF
fi
# <<< idle baseline <<<

# ---- GPU driver bring-up wait ----------------------------------------------
# A live USB can start a run minutes after boot, before the NVIDIA driver has
# finished coming up: the DKMS module is built but not yet loaded, so there is
# no /dev/nvidia* and no libcuda.so.1 - and a CUDA-built torch cannot even be
# imported without libcuda, which makes the backend probe report "no usable
# backend" (the CPU fallback is the same module, so it fails too). Wait for the
# driver before the first GPU phase: `nvidia-smi` answers AND `import torch`
# succeeds. Bounded (BENCH_GPU_WAIT_SECONDS, default 120s, in
# BENCH_GPU_WAIT_STEP-sized steps) and gated on the gpu group, so a skipped
# GPU group never pays it and non-GPU groups never depend on torch. If it never
# comes up, the run proceeds and gpu_bench records the missing backend.
# >>> gpu driver wait >>>
GPU_WAIT_S=0
GPU_READY=0
if [ "$DO_GPU" = 1 ] && command -v nvidia-smi >/dev/null 2>&1; then
    GPU_WAIT_MAX="${BENCH_GPU_WAIT_SECONDS:-120}"
    GPU_WAIT_STEP="${BENCH_GPU_WAIT_STEP:-5}"
    while :; do
        if nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1 \
                && "$PY" -c 'import torch' >/dev/null 2>&1; then
            GPU_READY=1
            break
        fi
        [ "$GPU_WAIT_S" -ge "$GPU_WAIT_MAX" ] && break
        sleep "$GPU_WAIT_STEP"
        GPU_WAIT_S=$((GPU_WAIT_S + GPU_WAIT_STEP))
    done
    if [ "$GPU_READY" = 1 ] && [ "$GPU_WAIT_S" -eq 0 ]; then
        :   # already up: nothing to record
    else
        if [ "$GPU_READY" = 1 ]; then
            echo "[gpu] driver wait: GPU usable after ${GPU_WAIT_S}s"
        else
            echo "[gpu] driver wait: GPU still not usable after ${GPU_WAIT_S}s - proceeding; the GPU phases will record the missing backend"
        fi
        GPU_READY_JSON=false
        [ "$GPU_READY" = 1 ] && GPU_READY_JSON=true
        printf '{"group":"gpu","phase":"driver-wait","result":{"waited_s":%d,"usable":%s}}\n' \
            "$GPU_WAIT_S" "$GPU_READY_JSON" >> "$JSONL"
    fi
fi
# <<< gpu driver wait <<<

if [ "$DO_GPU" = 1 ]; then
    run_phase gpu gpu_bench.py info
    run_phase gpu gpu_bench.py matmul --size "$GPU_SIZE" --seconds 10 --force
    run_phase gpu gpu_bench.py membw  --size "$GPU_SIZE" --seconds 8  --force
    run_phase gpu gpu_bench.py pcie   --size "$GPU_SIZE" --seconds 8  --force
    run_phase gpu gpu_bench.py conv   --size "$GPU_SIZE" --seconds 15 --force
    run_phase gpu gpu_bench.py integrity --size "$GPU_SIZE" --force
    run_phase gpu gpu_bench.py streams --size "$GPU_SIZE" --seconds 8 --force
    run_phase gpu gpu_bench.py soak   --size "$GPU_SIZE" --seconds "$GPU_SOAK" --force $STABLE_ARG
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

# Disk testing needs a real internal volume: never benchmark the live USB or
# tmpfs (RAM). Skip unless an internal disk is mounted at /mnt/internal, or the
# operator passes an explicit --disk-path.
if [ "$DO_DISK" = 1 ] && [ "$DISK_PATH_SET" = 0 ]; then
    if mountpoint -q /mnt/internal; then
        DISK_PATH=/mnt/internal/bench-scratch
        mkdir -p "$DISK_PATH" 2>/dev/null || true
        echo "    [info] disk target: internal volume /mnt/internal"
    elif [ ! -e /run/archiso ] && [ "$(df -T /var/tmp 2>/dev/null | awk 'NR==2 {print $2}')" != "tmpfs" ]; then
        # non-live host (desktop/CI): test the machine's real disk
        DISK_PATH=/var/tmp/bench-scratch
        mkdir -p "$DISK_PATH" 2>/dev/null || true
        echo "    [info] disk target: $DISK_PATH (host scratch)"
    else
        echo "    [info] disk: skipped - no internal disk available (live USB / RAM only)"
        echo "           mount an internal volume at /mnt/internal, or pass --disk-path DIR"
        DO_DISK=0
    fi
fi
if [ "$DO_DISK" = 1 ]; then
    FSTYPE="$(df -T "$DISK_PATH" 2>/dev/null | awk 'NR==2 {print $2}')"
    if [ "$FSTYPE" = "tmpfs" ]; then
        echo "    [warn] disk path $DISK_PATH is tmpfs (RAM) - numbers reflect RAM, not a disk"
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

# ---- LLM inference (Ollama) ------------------------------------------------
# Linux port of Invoke-LlmBench.ps1: a /api/tags readiness probe, bounded CLI
# calls, and per-model tok/s from eval_count/eval_duration. Every failure mode
# is a recorded skipped/error reason, never a zero, and no call can hang. Runs
# only when the llm group is selected (--skip-llm turns it off). The server base
# URL can be overridden with BENCH_OLLAMA_URL.
# >>> llm bench >>>
if [ "$DO_LLM" = 1 ]; then
    echo "[llm] Ollama inference"
    LLM_OUT="$("$PY" - "$JSONL" 2>&1 <<'PYEOF'
import json, os, shutil, subprocess, sys, urllib.request

jsonl = sys.argv[1]
base = (os.environ.get("BENCH_OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
TOKENS = 128
PROBE_TIMEOUT = 5      # the readiness probe
CLI_TIMEOUT = 30       # `ollama list`/`ps` block forever on a wedged server
REQUEST_TIMEOUT = 600  # one generation; a hung server must not hang the battery


def record(res):
    with open(jsonl, "a") as fh:
        fh.write(json.dumps({"group": "llm", "phase": "bench", "result": res}) + "\n")
    if res.get("skipped"):
        print("skipped: %s" % res.get("reason", ""))
    for r in res.get("results") or []:
        if r.get("tok_per_s") is not None:
            print("%s: %s tok/s (%s tokens in %ss)" % (
                r.get("model"), r["tok_per_s"], r.get("eval_tokens"), r.get("eval_seconds")))
        else:
            print("%s: error: %s" % (r.get("model"), r.get("error")))


# fail fast with a reason when the server is not answering at all, instead of
# discovering it one (bounded, but slow) generation at a time
try:
    with urllib.request.urlopen(base + "/api/tags", timeout=PROBE_TIMEOUT) as resp:
        tags = json.loads(resp.read().decode("utf-8", "replace") or "{}")
except Exception as e:
    record({"skipped": True, "reason": "ollama server not responding on %s: %s" % (base, e)})
    raise SystemExit(0)

models = [str(m.get("name")) for m in (tags.get("models") or [])
          if isinstance(m, dict) and m.get("name")]
if not models:
    # CLI-only installs / older servers: `ollama list` is the fallback, bounded
    cli = shutil.which("ollama")
    if cli:
        try:
            out = subprocess.run([cli, "list"], capture_output=True, text=True,
                                 timeout=CLI_TIMEOUT).stdout
        except Exception as e:
            record({"skipped": True, "reason": "ollama list failed: %s" % e})
            raise SystemExit(0)
        models = [ln.split()[0] for ln in out.splitlines()[1:] if ln.split()]
if not models:
    record({"skipped": True, "reason": "no ollama models installed"})
    raise SystemExit(0)

results = []
for m in models:
    body = json.dumps({"model": m, "stream": False,
                       "prompt": "Write a detailed paragraph about graphics processing.",
                       "options": {"num_predict": TOKENS}}).encode()
    req = urllib.request.Request(base + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            out = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception as e:
        results.append({"model": m, "error": str(e)})
        continue
    count, dur_ns = out.get("eval_count"), out.get("eval_duration")
    if not count or not dur_ns:
        # a missing/zero counter is a reason, not a 0 tok/s "measurement"
        results.append({"model": m, "error": "no eval_count/eval_duration in the response"})
        continue
    dur = float(dur_ns) / 1e9
    results.append({"model": m, "tok_per_s": round(float(count) / dur, 1),
                    "eval_tokens": count, "eval_seconds": round(dur, 2)})

# `ollama ps` is best-effort: its failure is recorded, never fatal, and it
# cannot lose the measurements above
res = {"results": results, "tokens": TOKENS}
cli = shutil.which("ollama")
if cli:
    try:
        res["ps"] = [ln for ln in subprocess.run(
            [cli, "ps"], capture_output=True, text=True,
            timeout=CLI_TIMEOUT).stdout.splitlines() if ln.strip()]
    except Exception as e:
        res["ps_error"] = "ollama ps: %s" % e
else:
    res["ps_error"] = "ollama CLI not found"
record(res)
PYEOF
)"; LLM_RC=$?
    printf '%s\n' "$LLM_OUT" | grep -v '^$' | sed 's/^/    /'
    printf '%s\n' "$LLM_OUT" > "$LOGDIR/llm-bench.log"
    if [ "$LLM_RC" -ne 0 ]; then
        echo "    [warn] llm exited $LLM_RC"
        # fail closed: a phase that died without leaving a record still leaves a
        # reason, so the summary can never read the llm group as "not run"
        if ! grep -q '"group": "llm"' "$JSONL"; then
            printf '{"group":"llm","phase":"bench","result":{"skipped":true,"reason":"llm phase exited %d"}}\n' "$LLM_RC" >> "$JSONL"
        fi
    fi
fi
# <<< llm bench <<<

if [ "$DO_GFX" = 1 ]; then
    FM=""
    if [ "$GFX_ENGINE" != "glmark2" ]; then
        echo "[gfx] FurMark 2 (furmark-gl, 1920x1080, ${GFX_SECONDS}s)"
        FM="${FURMARK:-}"
        if [ -z "$FM" ]; then
            for c in "$ROOT/tools/furmark/FurMark_linux64/furmark" \
                     "${BENCH_PERSIST_DIR:-/mnt/benchdata}/tools/FurMark_linux64/furmark" \
                     /opt/bench-tools/furmark/FurMark_linux64/furmark; do
                [ -x "$c" ] && FM="$c" && break
            done
        fi
    fi
    if [ -z "$FM" ] || [ ! -x "$FM" ]; then
        if [ "$GFX_ENGINE" = "glmark2" ]; then
            echo "[gfx] glmark2 (engine requested; FurMark skipped)"
        else
            echo "    furmark not found (looked in the toolkit, \$BENCH_PERSIST_DIR/tools and /opt/bench-tools) -- download FurMark 2 (linux64) from geeks3d.com into tools/, or set \$FURMARK"
            printf '{"group":"gfx","phase":"furmark","result":{"skipped":true,"reason":"furmark not installed"}}\n' >> "$JSONL"
        fi
        # no FurMark to run: still set up the display for the glmark2 fallbacks
        GFRC=${GFRC:-1}
        mkdir -p "$LOGDIR"
    fi
    if [ -n "$FM" ] && [ -x "$FM" ]; then
        # live consoles have no X; start a headless one on :9 (nvidia
        # UseDisplayDevice=none) so GL works without blanking the console and
        # without stealing the GUI's :0 display
        if ! xdpyinfo -display "${DISPLAY:-:0}" >/dev/null 2>&1; then
            if ! xdpyinfo -display :9 >/dev/null 2>&1; then
                echo "    starting headless X (:9) for the gfx phase"
                XARGS=""
                [ -d /etc/X11/bench-headless.d ] && XARGS="-configdir /etc/X11/bench-headless.d"
                X :9 vt2 $XARGS -ac -novtswitch -sharevts -nolisten tcp > /tmp/bench-gfx-x.log 2>&1 &
                for i in $(seq 1 20); do
                    sleep 0.5
                    xdpyinfo -display :9 >/dev/null 2>&1 && break
                done
            fi
            export DISPLAY=:9
        fi
        # FurMark's X11 DPI/monitor probe crashes when RESOURCE_MANAGER is
        # missing (Xwayland) or when the server reports ZERO monitors (headless
        # X): give it a virtual monitor and a verified RESOURCE_MANAGER.
        if [ -n "$(command -v xrandr 2>/dev/null)" ]; then
            xrandr -display "$DISPLAY" --setmonitor bench 1920/597x1080/336+0+0 none 2>/dev/null || true
        fi
        if [ -n "$(command -v xprop 2>/dev/null)" ]; then
            for i in 1 2 3 4 5; do
                xprop -display "$DISPLAY" -root -f RESOURCE_MANAGER 8s -set RESOURCE_MANAGER '*dpi: 96' 2>/dev/null || true
                xprop -display "$DISPLAY" -root RESOURCE_MANAGER 2>/dev/null | grep -q dpi && break
                sleep 0.5
            done
        fi
        # report what the probe actually saw - FurMark dies on a zero-monitor or
        # missing-RESOURCE_MANAGER display, and this is how we find out why
        MON="$(xrandr -display "$DISPLAY" --listmonitors 2>/dev/null | head -1)"
        RM="$(xprop -display "$DISPLAY" -root RESOURCE_MANAGER 2>/dev/null | head -1)"
        [ -z "$RM" ] && echo "    WARNING: no RESOURCE_MANAGER on $DISPLAY"
        # FurMark's X11 probe walks the monitor list and dies (SIGSEGV) on a
        # display that reports none - a live box with no monitor attached. Detect
        # that instead of burning the timeout on a core dump: glmark2 does not
        # need a monitor (it just needs a GLX surface, which :9 has).
        FM_RUN=1
        case "$MON" in
            *"Monitors: 0"*)
                echo "    no monitor on $DISPLAY - skipping FurMark (its probe needs one), using glmark2"
                FM_RUN=0 ;;
        esac
        # diagnostic snapshot: uploaded with the run logs for gfx post-mortems
        {
            echo "display: $DISPLAY"
            xdpyinfo -display "$DISPLAY" 2>&1 | head -15
            xrandr -display "$DISPLAY" --listmonitors 2>&1
            xprop -display "$DISPLAY" -root RESOURCE_MANAGER 2>&1
            if command -v glxinfo >/dev/null 2>&1; then glxinfo -B 2>&1 | head -12; fi
        } > "$LOGDIR/gfx-diag.txt" 2>&1
        mkdir -p "$LOGDIR/furmark"
        if [ "$FM_RUN" = 1 ]; then
            GFX_OUT="$(cd "$(dirname "$FM")" && timeout $((GFX_SECONDS * 2 + 120)) ./furmark --demo furmark-gl \
                --width 1920 --height 1080 --benchmark --duration-ms "$((GFX_SECONDS * 1000))" \
                --no-score-box --vsync 0 --log-gpu-data --gpu-monitor-print \
                --export-dir "$LOGDIR/furmark" --hw-polling-interval 1000 2>&1)"; GFRC=$?
            printf '%s\n' "$GFX_OUT" > "$LOGDIR/gfx-furmark.log"
            printf '%s\n' "$GFX_OUT" | grep -a -v '^$' | sed 's/^/    /'
        else
            GFRC=1
            printf '{"group":"gfx","phase":"furmark","result":{"skipped":true,"reason":"no monitor on the X display"}}\n' >> "$JSONL"
        fi
        # glmark2's default is ~10s per scene (25 scenes ~= 4 min, which can run
        # past the timeout and lose the final Score line), so the fallback below
        # runs a bounded scene subset instead of the whole suite.
        # if FurMark still dies, fall back to glmark2 so the gfx phase always
        # produces a score (glmark2 ships in the live image). Note: glmark2's -b
        # takes a benchmark NAME, so a bare "-b" is an error - run the default suite.
        # glmark2's -d is --debug (it floods the log and does not shorten the run);
        # the per-scene duration is a scene option, and its default of ~10s per
        # scene over the full suite runs past any sane timeout. Run a fixed,
        # representative subset with a short per-scene duration so the phase is
        # bounded and comparable run to run.
        GLD=2
        GL_SCENES="-b build:duration=$GLD -b texture:duration=$GLD -b shading:duration=$GLD \
-b bump:duration=$GLD -b effect2d:duration=$GLD -b jellyfish:duration=$GLD \
-b terrain:duration=$GLD -b shadow:duration=$GLD"
        if [ "$GFRC" -ne 0 ] && command -v glmark2 >/dev/null 2>&1; then
            echo "    running glmark2 (8 scenes, ${GLD}s each)"
            GM_OUT="$(timeout $((GFX_SECONDS + 120)) glmark2 $GL_SCENES 2>&1)"; GMRC=$?
            printf '%s\n' "$GM_OUT" > "$LOGDIR/gfx-glmark2.log"
            printf '%s\n' "$GM_OUT" | grep -E "glmark2 Score|FPS:" | tail -3 | sed 's/^/    /'
            # the last numeric token, whitespace-stripped: grep -oE emits one line
            # PER MATCH, so two matches became "2\n31596" inside the JSON record and
            # split it across lines (which killed the whole summary)
            GSCORE="$(printf '%s\n' "$GM_OUT" | grep -oE "glmark2 Score: [0-9]+" | tail -1 \
                      | grep -oE "[0-9]+" | tail -1 | tr -d '[:space:]')"
            printf '{"group":"gfx","phase":"glmark2","result":{"score":%s,"furmark_rc":%s,"scenes":8,"per_scene_s":%s,"exit":%s}}\n' "${GSCORE:-0}" "$GFRC" "$GLD" "$GMRC" >> "$JSONL"
        fi
        # last resort: EGL/GLES off-screen, which needs no X output/CRTC (the
        # headless NVIDIA server has none, which is what kills the GL apps above)
        if [ "${GSCORE:-0}" = "0" ] && command -v glmark2-es2 >/dev/null 2>&1; then
            echo "    trying glmark2-es2 --off-screen (EGL, no X output needed)"
            GME_OUT="$(DISPLAY= timeout 90 glmark2-es2 --off-screen 2>&1)"; GMERC=$?
            printf '%s\n' "$GME_OUT" > "$LOGDIR/gfx-glmark2-es2.log"
            printf '%s\n' "$GME_OUT" | grep -E "glmark2 Score|FPS:" | tail -3 | sed 's/^/    /'
            GESCORE="$(printf '%s\n' "$GME_OUT" | grep -oE "glmark2 Score: [0-9]+" | tail -1 \
                       | grep -oE "[0-9]+" | tail -1 | tr -d '[:space:]')"
            printf '{"group":"gfx","phase":"glmark2-es2","result":{"score":%s,"exit":%s,"furmark_rc":%s}}\n' "${GESCORE:-0}" "$GMERC" "$GFRC" >> "$JSONL"
        fi
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
    "$PY" "$BENCH/gpu_bench.py" soak --size "$GPU_SIZE" --seconds "$COMBINED_SECONDS" --force $STABLE_ARG >"$D/gpu.out" 2>&1 &
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
    "$PY" "$BENCH/gpu_bench.py" soak --size "$GPU_SIZE" --seconds "$FULLSOAK_SECONDS" --force $STABLE_ARG >"$D/gpu.out" 2>&1 &
    GPID=$!
    "$PY" "$BENCH/cpu_bench.py" soak --size "$CPU_SIZE" --seconds "$FULLSOAK_SECONDS" >"$D/cpu.out" 2>&1 &
    CPID=$!
    "$PY" "$BENCH/ram_bench.py" soak --size-mb "$RAM_SIZE_MB" --fill-pct "$FILL_PCT" --passes "$PASSES" --force --seconds "$FULLSOAK_SECONDS" >"$D/ram.out" 2>&1 &
    RPID=$!
    if [ "$DO_DISK" = 1 ]; then
        "$PY" "$BENCH/disk_bench.py" soak --path "$DISK_PATH" --size-mb "$DISK_SIZE_MB" --seconds "$FULLSOAK_SECONDS" --force >"$D/disk.out" 2>&1 &
        DPID=$!
    else
        echo "    [info] fullsoak: disk skipped (no internal disk)"
        : > "$D/disk.out"
        DPID=""
    fi
    wait "$GPID"; GRC=$?
    wait "$CPID"; CRC=$?
    wait "$RPID"; RRC=$?
    if [ -n "$DPID" ]; then wait "$DPID"; DRC=$?; else DRC=0; fi
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

# >>> run summary >>>
"$PY" - "$JSONL" "$BASE.json" "$BASE.md" "$HOST" "$ISO" "$PY" "$ROOT" "$TELEMETRY" "$LOGDIR" <<'PYEOF' 2> "$LOGDIR/summary-error.txt"
import json, os, re, subprocess, sys, platform
jsonl, json_out, md_out, host, iso, py, root, telemetry_csv, logdir = sys.argv[1:10]

recs = []
bad = 0
with open(jsonl) as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except ValueError:
            # one malformed record must not cost the whole summary - it did:
            # the JSONDecodeError killed the report and left summary.gfx empty
            bad += 1
if bad:
    print("[warn] skipped %d malformed record(s) in %s" % (bad, jsonl))


def find(group, phase):
    for r in recs:
        if r.get("group") == group and r.get("phase") == phase:
            return r
    return None


# idle baseline + LLM records, when their groups ran. The idle values become
# flat idle_* summary fields so a reader can compute a soak delta.
idle_rec = find("idle", "baseline")
idle_res = idle_rec.get("result") if isinstance(idle_rec, dict) else None
llm_rec = find("llm", "bench")
llm_res = llm_rec.get("result") if isinstance(llm_rec, dict) else None

# GPU driver bring-up: the wait before the first GPU phase records what it cost
# and whether it ended usable, so a slow driver shows up as a wait, not a gap.
wait_rec = find("gpu", "driver-wait")
wait_res = wait_rec.get("result") if isinstance(wait_rec, dict) else None


# A GPU group that found no usable backend must not read like a run that never
# asked for one: gpu_bench marks the failed phases with backend_error_kind, so
# turn that into an explicit warn verdict. Same finding as
# benchreport.apply_backend_liveness (the dashboards' summary path).
def backend_finding():
    for r in recs:
        res = r.get("result") if isinstance(r, dict) else None
        if not isinstance(res, dict) or r.get("group") != "gpu":
            continue
        kind = res.get("backend_error_kind")
        if not kind:
            continue
        err = str(res.get("error") or "").strip()
        detail = re.sub(r"^no usable backend(?: \(kind=[^)]+\))?:?\s*", "", err) or err
        return {"level": "warn", "kind": kind, "detail": detail or None,
                "text": "GPU group found no usable backend (%s)%s - the GPU phases "
                        "recorded no results; this run was asked for a GPU test, it "
                        "was not skipped" % (kind, (": " + detail) if detail else "")}
    return None


backend = backend_finding()


def idle_fields(res):
    out = {}
    if isinstance(res, dict):
        for k in ("seconds", "samples", "temp_c", "fan_pct", "power_w", "sm_mhz",
                  "cpu_temp_c", "cpu_mhz", "cpu_pkg_w", "gpu_fan_rpm"):
            if res.get(k) is not None:
                out["idle_" + k] = res[k]
    return out


idle_flat = idle_fields(idle_res)

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
report.update(idle_flat)
if wait_res is not None:
    report["gpu_driver_wait_s"] = wait_res.get("waited_s")
    report["gpu_driver_usable"] = wait_res.get("usable")
if backend is not None:
    report["gpu_backend"] = {"ok": False, "kind": backend["kind"],
                             "detail": backend["detail"]}
    report["verdicts"] = [{"level": backend["level"], "text": backend["text"]}]
    print("[warn] " + backend["text"])
if idle_res is not None:
    report["idle"] = idle_res
if llm_res is not None:
    report["llm"] = llm_res
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
if wait_res is not None and wait_res.get("usable") is not True:
    md.append("**GPU driver wait:** GPU still not usable after %ss - the GPU phases "
              "record the missing backend, not a measurement" % wait_res.get("waited_s"))
    md.append("")
if backend is not None:
    md.append("**GPU backend:** missing - %s" % backend["text"])
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

if idle_res and not idle_res.get("skipped"):
    rows = []
    for label, key, unit in (("gpu temp", "temp_c", "°C"), ("gpu fan", "fan_pct", "%"),
                             ("gpu power", "power_w", "W"), ("sm clock", "sm_mhz", "MHz"),
                             ("cpu temp", "cpu_temp_c", "°C"), ("cpu clock", "cpu_mhz", "MHz"),
                             ("cpu package", "cpu_pkg_w", "W")):
        v = idle_res.get(key)
        if v:
            rows.append("| %s | %s / %s / %s %s |" % (label, v[0], v[1], v[2], unit))
    if rows:
        table("Idle baseline (%ss, no load)" % idle_res.get("seconds", "?"), rows)

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

if llm_res:
    rows = []
    if llm_res.get("skipped"):
        rows.append("| ollama | skipped (%s) |" % llm_res.get("reason", ""))
    for m in llm_res.get("results") or []:
        if m.get("tok_per_s") is not None:
            rows.append("| %s | %s tok/s (%s tokens / %ss) |" % (
                m.get("model"), m.get("tok_per_s"), m.get("eval_tokens"),
                m.get("eval_seconds")))
        else:
            rows.append("| %s | error (%s) |" % (m.get("model"), m.get("error", "")))
    if llm_res.get("ps_error"):
        rows.append("| ollama ps | %s |" % llm_res.get("ps_error"))
    if rows:
        table("LLM inference (Ollama)", rows)

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

def _with_score(rec):
    """A gfx record only counts if it produced a score (a 'skipped' furmark record
    is truthy and used to short-circuit past the glmark2 fallback's real result)."""
    r = (rec or {}).get("result") or {}
    return rec if (r.get("score") is not None and not r.get("skipped")) else None


gfx = (_with_score(by("gfx", "furmark")) or _with_score(by("gfx", "glmark2"))
       or _with_score(by("gfx", "glmark2-es2"))
       or by("gfx", "furmark") or by("gfx", "glmark2") or by("gfx", "glmark2-es2"))
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
idx.update(idle_flat)
if wait_res is not None:
    idx["gpu_driver_wait_s"] = wait_res.get("waited_s")
    idx["gpu_driver_usable"] = wait_res.get("usable")
if backend is not None:
    idx["gpu_backend"] = {"ok": False, "kind": backend["kind"],
                          "detail": backend["detail"]}
    idx["verdicts"] = [{"level": backend["level"], "text": backend["text"]}]
if idle_res is not None:
    idx["idle"] = idle_res
if llm_res is not None:
    idx["llm"] = llm_res
with open(os.path.join(os.path.dirname(json_out), "runs-index.jsonl"), "a") as fh:
    fh.write(json.dumps(idx) + "\n")

md.append("_Linux run via Run-LinuxBench.sh; disk phases use OS-buffered I/O._")
with open(md_out, "w") as fh:
    fh.write("\n".join(md) + "\n")

print("[done] wrote %s and %s" % (json_out, md_out))
PYEOF
# <<< run summary <<<
# The battery itself is done at this point; if the report writer throws we must not
# report the whole run as failed (it also loses the summary). Surface it loudly,
# keep the traceback next to the logs, and exit 0 - the phases really did run.
SUMMARY_RC=$?
if [ "$SUMMARY_RC" -ne 0 ]; then
    echo "[warn] summary generation failed (rc=$SUMMARY_RC) - phases completed, report missing"
    {
        echo ""
        echo "Run-LinuxBench.sh: summary step exited $SUMMARY_RC"
        echo "results dir: $OUT"
    } >> "$LOGDIR/summary-error.txt" 2>/dev/null || true
fi

# Exit non-zero only if nothing ran at all. One phase failing is recorded in the
# summary and the run is still a run; a battery that produced no records at all
# (no python, no vendor tools, a broken environment) has to be visible to the
# caller instead of looking like a successful run.
if ! grep -q '"group"' "$JSONL" 2>/dev/null; then
    echo "[fail] no phase produced a result - see $LOGDIR" >&2
    exit 1
fi
exit 0
