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


def phase_hash(a):
    import hashlib
    data = os.urandom(64 * 1024 * 1024)
    r = timed(a.seconds, lambda: hashlib.sha256(data).digest(), nbytes=len(data))
    mbps = round(len(data) * r["iters"] / r["seconds"] / 1e6, 0)
    out = {"phase": "hash", "sha256_mbps": mbps, "mb": len(data) // (1024 * 1024)}
    print("sha256 %.0f MB/s (%d MiB block)" % (mbps, out["mb"]))
    jline(out)


def phase_compress(a):
    import zlib
    import lzma
    data = os.urandom(64 * 1024 * 1024)
    r = timed(a.seconds, lambda: zlib.compress(data, 6), nbytes=len(data))
    zc = round(len(data) * r["iters"] / r["seconds"] / 1e6, 0)
    comp = zlib.compress(data, 6)
    r2 = timed(a.seconds, lambda: zlib.decompress(comp), nbytes=len(data))
    zd = round(len(data) * r2["iters"] / r2["seconds"] / 1e6, 0)
    t0 = time.time(); lzma.compress(data, preset=1); lc = round(len(data) / (time.time() - t0) / 1e6, 0)
    t0 = time.time(); lzma.decompress(lzma.compress(data, preset=1)); ld = round(len(data) / (time.time() - t0) / 1e6, 0)
    out = {"phase": "compress", "zlib_compress_mbps": zc, "zlib_decompress_mbps": zd,
           "lzma_compress_mbps": lc, "lzma_decompress_mbps": ld}
    print("compress zlib c/d=%.0f/%.0f  lzma c/d=%.0f/%.0f MB/s" % (zc, zd, lc, ld))
    jline(out)


def phase_aes(a):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except Exception:
        out = {"phase": "aes", "skipped": True, "reason": "cryptography not installed"}
        print("aes skipped (pip install cryptography)")
        jline(out)
        return
    key = os.urandom(32); nonce = os.urandom(12); data = os.urandom(64 * 1024 * 1024)
    g = AESGCM(key)
    r = timed(a.seconds, lambda: g.encrypt(nonce, data, None), nbytes=len(data))
    mbps = round(len(data) * r["iters"] / r["seconds"] / 1e6, 0)
    out = {"phase": "aes", "aes256gcm_mbps": mbps, "mb": len(data) // (1024 * 1024)}
    print("aes-256-gcm %.0f MB/s" % mbps)
    jline(out)


def phase_flops(a):
    n = 64 * 1024 * 1024
    x = np.random.rand(n).astype(np.float32); y = np.random.rand(n).astype(np.float32); z = np.empty_like(x)
    r = timed(a.seconds, lambda: np.add(np.multiply(x, y, out=z), z, out=z), flops=2 * n)
    gf = round(2 * n * r["iters"] / r["seconds"] / 1e9, 1)
    out = {"phase": "flops", "gflops": gf, "elements": n, "dtype": "fp32"}
    print("flops (fp32 mul+add) %.1f GFLOPS" % gf)
    jline(out)


PHASES = {"info": phase_info, "matmul": phase_matmul, "membw": phase_membw, "soak": phase_soak,
          "hash": phase_hash, "compress": phase_compress, "aes": phase_aes, "flops": phase_flops}


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
