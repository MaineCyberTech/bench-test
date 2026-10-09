#!/usr/bin/env bash
# Run-LinuxBench.sh - Linux runner for the bench-test Python phases (GPU/CPU/RAM/disk).
# Writes results/<host>-linux-<stamp>.json and .md, mirroring the PowerShell invokers' metrics.
#
# Usage: ./Run-LinuxBench.sh [-o OutDir] [--gpu-soak S] [--cpu-soak S] [--ram-soak S]
#   [--gpu-size N] [--cpu-size N] [--ram-size-mb N] [--fill-pct N] [--passes N]
#   [--disk-path DIR] [--disk-size-mb N] [--disk-seconds S] [--steady-gb G]
#   [--skip-gpu] [--skip-cpu] [--skip-ram] [--skip-disk]
#
# Env: BENCH_PY overrides the Python interpreter (default: .venv/bin/python, else python3).
#      GPU phases need PyTorch CUDA; on pre-Turing NVIDIA use torch cu126 wheels.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH="$ROOT/bench"
PY="${BENCH_PY:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"

OUT="$ROOT/results"
GPU_SOAK=600; CPU_SOAK=120; RAM_SOAK=120
GPU_SIZE=12288; CPU_SIZE=4096; RAM_SIZE_MB=1024; FILL_PCT=70; PASSES=2
DISK_PATH="${TMPDIR:-/tmp}"; DISK_SIZE_MB=2048; DISK_SECONDS=15; STEADY_GB=10
DO_GPU=1; DO_CPU=1; DO_RAM=1; DO_DISK=1

while [ $# -gt 0 ]; do
    case "$1" in
        -o|--out-dir)      OUT="$2"; shift 2 ;;
        --gpu-soak)        GPU_SOAK="$2"; shift 2 ;;
        --cpu-soak)        CPU_SOAK="$2"; shift 2 ;;
        --ram-soak)        RAM_SOAK="$2"; shift 2 ;;
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
        -h|--help)         sed -n '2,12p' "$0"; exit 0 ;;
        *)                 echo "unknown option: $1" >&2; exit 1 ;;
    esac
done

HOST="$(hostname -s)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ISO="$(date -Iseconds)"
mkdir -p "$OUT" "$DISK_PATH"
BASE="$OUT/$HOST-linux-$STAMP"
JSONL="$(mktemp)"
trap 'rm -f "$JSONL"' EXIT

echo "[env] python: $($PY -c 'import sys; print(sys.version.split()[0])')  out: $BASE.json|.md"

run_phase() {
    local group="$1" script="$2" phase="$3"; shift 3
    echo "[$group] $phase"
    local out rc line
    out="$("$PY" "$BENCH/$script" "$phase" "$@" 2>&1)"; rc=$?
    printf '%s\n' "$out" | grep -v '^RESULT_JSON:' | grep -v '^$' | sed 's/^/    /'
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

"$PY" - "$JSONL" "$BASE.json" "$BASE.md" "$HOST" "$ISO" "$PY" <<'PYEOF'
import json, sys, platform
jsonl, json_out, md_out, host, iso, py = sys.argv[1:7]

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

report = {
    "host": host,
    "timestamp": iso,
    "platform": platform.platform(),
    "env": env,
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
        rows.append("| soak | %s |" % ("skipped" if s.get("skipped") else "%s TFLOPS over %ss" % (
            s.get("matmul_tflops"), s.get("seconds"))))
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

md.append("_Linux run via Run-LinuxBench.sh; disk phases use OS-buffered I/O._")
with open(md_out, "w") as fh:
    fh.write("\n".join(md) + "\n")

print("[done] wrote %s and %s" % (json_out, md_out))
PYEOF
