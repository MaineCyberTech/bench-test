#!/usr/bin/env python3
"""CPU benchmark phases for the bench-test toolkit.

Mirrors gpu_bench.py's contract: each phase prints human-readable lines and a
single machine-readable summary line:

    RESULT_JSON:{"phase":"...", ...}

Phases: info, matmul, membw, soak
Requires numpy (already present in the PyTorch venv).
"""
import argparse
import json
import os
import time

import numpy as np


def jline(obj):
    print("RESULT_JSON:" + json.dumps(obj), flush=True)


def timed(seconds, fn, flops=0, nbytes=0):
    fn()
    t0 = time.time()
    it = 0
    while time.time() - t0 < seconds:
        fn()
        it += 1
    dt = time.time() - t0
    out = {"iters": it, "seconds": round(dt, 2)}
    if flops:
        out["gflops"] = round(flops * it / dt / 1e9, 1)
    if nbytes:
        out["gbps"] = round(nbytes * it / dt / 1e9, 0)
    return out


def phase_info(_):
    info = {"phase": "info", "numpy": np.__version__, "logical_cpus": os.cpu_count()}
    print("numpy=%s logical_cpus=%s" % (info["numpy"], info["logical_cpus"]))
    jline(info)


def phase_matmul(a):
    n = a.size
    out = {"phase": "matmul", "n": n, "results": {}}
    for dtype, name in [(np.float32, "fp32"), (np.float64, "fp64")]:
        A = np.random.randn(n, n).astype(dtype)
        B = np.random.randn(n, n).astype(dtype)
        r = timed(a.seconds, lambda: A @ B, flops=2.0 * n ** 3)
        out["results"][name] = r
        print("matmul %-4s N=%d iters=%d GFLOPS=%.1f" % (name, n, r["iters"], r["gflops"]))
        del A, B
    jline(out)


def phase_membw(a):
    n = 64 * 1024 * 1024  # 64M fp32 = 256 MiB per array
    x = np.random.rand(n).astype(np.float32)
    y = np.random.rand(n).astype(np.float32)
    dst = np.empty_like(x)
    out = {"phase": "membw", "bytes_per_array": n * 4, "results": {}}
    out["results"]["copy_gbps"] = timed(a.seconds, lambda: np.copyto(dst, x), nbytes=2 * n * 4)["gbps"]
    out["results"]["add_gbps"] = timed(a.seconds, lambda: np.add(x, y, out=dst), nbytes=3 * n * 4)["gbps"]
    out["results"]["reduce_gbps"] = timed(a.seconds, lambda: x.sum(), nbytes=n * 4)["gbps"]
    m = out["results"]
    print("membw copy=%.0f add=%.0f reduce=%.0f GB/s" % (m["copy_gbps"], m["add_gbps"], m["reduce_gbps"]))
    jline(out)


def phase_soak(a):
    n = min(a.size, 2048)
    A = np.random.randn(n, n).astype(np.float32)
    B = np.random.randn(n, n).astype(np.float32)
    t0 = time.time()
    it = 0
    while time.time() - t0 < a.seconds:
        _ = A @ B
        it += 1
    dt = time.time() - t0
    out = {"phase": "soak", "seconds": round(dt, 1), "iters": it,
           "gflops": round(2.0 * n ** 3 * it / dt / 1e9, 1)}
    print("soak %.1fs iters=%d GFLOPS=%.1f" % (dt, it, out["gflops"]))
    jline(out)


PHASES = {"info": phase_info, "matmul": phase_matmul, "membw": phase_membw, "soak": phase_soak}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=sorted(PHASES))
    ap.add_argument("--seconds", type=float, default=8.0, help="per-measurement seconds")
    ap.add_argument("--size", type=int, default=4096, help="matrix size N")
    a = ap.parse_args()
    PHASES[a.phase](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
