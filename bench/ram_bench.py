#!/usr/bin/env python3
"""RAM benchmark / stress for the bench-test toolkit.

Phases (each prints human lines + one "RESULT_JSON:" line):
  info       - total/available RAM, CPU count, numpy version
  bandwidth  - STREAM-like copy/scale/add/triad GB/s
  latency    - approximate random-access latency (ns) and gather GB/s
  integrity  - fill + verify a large fraction of free RAM (bit errors)
  soak       - sustained bandwidth + allocation churn for N seconds, with error check

Uses numpy (and psutil if present). ASCII-only output.
"""
import argparse
import json
import os
import sys
import time

import numpy as np

try:
    import psutil
except Exception:
    psutil = None


def jline(obj):
    print("RESULT_JSON:" + json.dumps(obj), flush=True)


def os_mem():
    """Return (total, available, used_pct) via psutil if present, else the Windows API."""
    if psutil:
        vm = psutil.virtual_memory()
        return vm.total, vm.available, vm.percent
    try:
        import ctypes

        class _MSX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MSX()
        st.dwLength = ctypes.sizeof(_MSX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullTotalPhys, st.ullAvailPhys, float(st.dwMemoryLoad)
    except Exception:
        pass
    return 0, 0, None


def mem_available_bytes():
    _, avail, _ = os_mem()
    if avail:
        return avail
    # last resort: assume half the RAM is free
    return 8 * 1024 ** 3


def timed(seconds, fn, nbytes):
    fn()
    t0 = time.time()
    it = 0
    while time.time() - t0 < seconds:
        fn()
        it += 1
    dt = time.time() - t0
    return {"iters": it, "seconds": round(dt, 2), "gbps": round(nbytes * it / dt / 1e9, 1)}


def phase_info(a):
    total, avail, pct = os_mem()
    out = {"phase": "info", "total_gib": round(total / 1024 ** 3, 1),
           "available_gib": round(avail / 1024 ** 3, 1), "used_pct": pct,
           "logical_cpus": os.cpu_count(), "numpy": np.__version__}
    print("RAM total=%.1fGiB available=%.1fGiB used=%s%% cpus=%d numpy=%s" % (
        out["total_gib"], out["available_gib"], pct, out["logical_cpus"], out["numpy"]))
    jline(out)


def phase_bandwidth(a):
    n = a.size_mb * 1024 * 1024 // 8  # float64 elements
    avail = mem_available_bytes()
    while n * 8 * 3 > avail * 0.9 and n > 1024 * 1024:  # need ~3 arrays
        n //= 2
    x = np.ones(n, dtype=np.float64)
    y = np.full(n, 2.0, dtype=np.float64)
    z = np.empty(n, dtype=np.float64)
    s = 3.0
    out = {"phase": "bandwidth", "elements": n, "gib_per_array": round(n * 8 / 1024 ** 3, 2), "results": {}}
    tests = [
        ("copy",  lambda: np.copyto(z, x),        2 * n * 8),
        ("scale", lambda: np.multiply(x, s, out=z), 2 * n * 8),
        ("add",   lambda: np.add(x, y, out=z),     3 * n * 8),
        ("triad", lambda: np.add(y, x * s, out=z), 4 * n * 8),
    ]
    for name, fn, nb in tests:
        r = timed(a.seconds, fn, nb)
        out["results"][name] = r["gbps"]
        print("ram bandwidth %-6s %.0f GB/s" % (name, r["gbps"]))
    del x, y, z
    jline(out)


def phase_latency(a):
    n = max(1 << 22, min(a.size_mb * 1024 * 1024 // 8, 1 << 26))
    idx = np.random.randint(0, n, size=n).astype(np.int64)
    data = np.random.rand(n)
    m = 5_000_000
    # gather (approx random-access bandwidth)
    t0 = time.time()
    acc = 0.0
    done = 0
    while done < m:
        k = min(1_000_000, m - done)
        acc += float(data[idx[done:done + k]].sum())
        done += k
    dt = time.time() - t0
    ns_per = dt / m * 1e9
    gbps = m * 8 / dt / 1e9
    out = {"phase": "latency", "accesses": m, "ns_per_access_approx": round(ns_per, 1),
           "gather_gbps_approx": round(gbps, 1), "note": "numpy gather; not a pure dependent-load chase"}
    print("ram latency approx %.1f ns/access, gather %.1f GB/s" % (ns_per, gbps))
    jline(out)


def phase_integrity(a):
    avail = mem_available_bytes()
    target = int(avail * (a.fill_pct / 100.0))
    chunk_bytes = 256 * 1024 * 1024
    n_chunks = max(1, target // chunk_bytes)
    n_elems = chunk_bytes // 4
    C = 0x7F3A2C1B
    passes = a.passes
    mismatched = 0
    bad_elems = 0
    for p in range(passes):
        arrs = []
        try:
            for _ in range(n_chunks):
                arrs.append(np.empty(n_elems, dtype=np.int32))
            for t in arrs:
                t.fill(np.int32(C) if p % 2 == 0 else np.int32(C ^ p))
            for t in arrs:
                v = np.int32(C) if p % 2 == 0 else np.int32(C ^ p)
                if int((t != v).sum()) != 0:
                    bad_elems += int((t != v).sum())
        except MemoryError:
            print("integrity: MemoryError at pass %d (allocated %d chunks)" % (p, len(arrs)))
            break
        finally:
            del arrs
    out = {"phase": "integrity", "giB": round(n_chunks * chunk_bytes / 1024 ** 3, 1),
           "chunks": n_chunks, "passes": passes, "bad_elements": bad_elems}
    print("ram integrity filled=%.1fGiB chunks=%d passes=%d bad=%d" % (
        out["giB"], n_chunks, passes, bad_elems))
    jline(out)


def phase_soak(a):
    n = min(a.size_mb * 1024 * 1024 // 8, mem_available_bytes() // 8 // 4)
    x = np.ones(n, dtype=np.float64)
    y = np.full(n, 2.0, dtype=np.float64)
    z = np.empty(n, dtype=np.float64)
    t0 = time.time()
    it = 0
    by = 0
    errors = 0
    while time.time() - t0 < a.seconds:
        np.add(x, y, out=z)
        np.multiply(z, 3.0, out=z)
        it += 1
        by += 3 * n * 8
        # periodic sample check
        if it % 200 == 0:
            if abs(z[0] - 9.0) > 1e-9:
                errors += 1
        # churn allocation sometimes
        if it % 500 == 0:
            big = np.empty(n // 4, dtype=np.float64)
            big.fill(1.0)
            del big
    dt = time.time() - t0
    out = {"phase": "soak", "seconds": round(dt, 1), "iters": it,
           "gbps": round(by / dt / 1e9, 1), "errors": errors,
           "gib_per_array": round(n * 8 / 1024 ** 3, 2)}
    print("ram soak %.1fs iters=%d %.0f GB/s errors=%d" % (dt, it, out["gbps"], errors))
    del x, y, z
    jline(out)


PHASES = {"info": phase_info, "bandwidth": phase_bandwidth, "latency": phase_latency,
          "integrity": phase_integrity, "soak": phase_soak}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=sorted(PHASES))
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--size-mb", type=int, default=1024, help="per-array MB (bandwidth/soak)")
    ap.add_argument("--fill-pct", type=float, default=70.0, help="integrity: percent of free RAM")
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    PHASES[a.phase](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
