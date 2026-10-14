#!/usr/bin/env python3
"""CPU benchmark phases for the bench-test toolkit.

Mirrors gpu_bench.py's contract: each phase prints human-readable lines and a
single machine-readable summary line:

    RESULT_JSON:{"phase":"...", ...}

Phases: info, matmul, membw, soak, hash, compress, aes, flops, scaling

numpy is imported lazily by the phases that need it (already present in the
PyTorch venv), so the plateau and scaling helpers stay importable without it.
"""
import argparse
import glob
import json
import os
import statistics
import subprocess
import sys
import threading
import time

_NP = None


def _np():
    global _NP
    if _NP is None:
        import numpy as _n
        _NP = _n
    return _NP


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


def _measure_matmul(n, seconds, dtype):
    """One timed square matmul; the scaling phase measures the same op."""
    np = _np()
    A = np.random.randn(n, n).astype(dtype)
    B = np.random.randn(n, n).astype(dtype)
    r = timed(seconds, lambda: A @ B, flops=2.0 * n ** 3)
    del A, B
    return r


def phase_info(_):
    info = {"phase": "info", "numpy": _np().__version__, "logical_cpus": os.cpu_count()}
    print("numpy=%s logical_cpus=%s" % (info["numpy"], info["logical_cpus"]))
    jline(info)


def phase_matmul(a):
    n = a.size
    out = {"phase": "matmul", "n": n, "results": {}}
    np = _np()
    for dtype, name in [(np.float32, "fp32"), (np.float64, "fp64")]:
        r = _measure_matmul(n, a.seconds, dtype)
        out["results"][name] = r
        print("matmul %-4s N=%d iters=%d GFLOPS=%.1f" % (name, n, r["iters"], r["gflops"]))
    jline(out)


def phase_membw(a):
    np = _np()
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


# --- telemetry (soak) -------------------------------------------------------

def _cpu_temp_c():
    """Best-effort CPU temperature in Celsius, or None when nothing is readable.

    Linux reads the sysfs thermal zones / CPU hwmon inputs (cheap). Windows asks
    the ACPI thermal zone (MSAcpi_ThermalZoneTemperature) via PowerShell - the
    same fallback the Windows telemetry sampler uses - which costs 0.3-1.3 s of
    CPU per read, so the soak samples it at a slower cadence.
    """
    if os.name != "nt":
        temps = []
        for path in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                with open(path) as fh:
                    v = float(fh.read().strip())
            except (OSError, ValueError):
                continue
            c = v / 1000.0 if v > 1000 else v
            if 0 < c < 150:
                temps.append(c)
        for hw in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                with open(os.path.join(hw, "name")) as fh:
                    if fh.read().strip().lower() not in ("coretemp", "k10temp", "zenpower",
                                                         "cpu_thermal", "acpitz"):
                        continue
            except OSError:
                continue
            for path in glob.glob(os.path.join(hw, "temp*_input")):
                try:
                    with open(path) as fh:
                        v = float(fh.read().strip())
                except (OSError, ValueError):
                    continue
                c = v / 1000.0 if v > 1000 else v
                if 0 < c < 150:
                    temps.append(c)
        return max(temps) if temps else None
    ps = ("(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
          "-ErrorAction SilentlyContinue | Measure-Object -Property CurrentTemperature -Maximum).Maximum")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        v = float(out)
        return round(v / 10.0 - 273.15, 1) if 2732 < v < 3832 else None
    except Exception:
        return None


def _telemetry_interval():
    """Sample cadence: sysfs is free, the Windows ACPI read is not."""
    return 2.0 if os.name != "nt" else 10.0


def _telemetry_loop(samples, stop, interval):
    while not stop.wait(interval):
        t = _cpu_temp_c()
        if t is not None:
            samples.append(t)


def _telemetry_summary(samples):
    if not samples:
        return None
    return {"samples": len(samples),
            "temp_c": [min(samples), round(statistics.mean(samples), 1), max(samples)]}


def _temp_stable(samples, window=8, tol=0.5):
    """True when the CPU temperature has plateaued.

    Same sense as gpu_bench._temp_stable: compare the mean of the older half of
    the window with the newer half, under `tol` degrees of drift. The window is
    8 samples; the sampler cadence (2 s sysfs / 10 s ACPI) sets its wall span.
    """
    if len(samples) < window:
        return False
    recent = list(samples[-window:])
    half = len(recent) // 2
    return abs(statistics.mean(recent[half:]) - statistics.mean(recent[:half])) < tol


def _soak_decision(t0, a, samples, stable_since, now=None):
    """Return ``(keep_going, stable_since)`` for one soak iteration.

    Stops at the ceiling (``--seconds``), or once the temperature has *held*
    steady for ``--stable-hold`` seconds after it first plateaued - a plateau
    alone is not enough (never before the minimum). Mirrors gpu_bench: the hold
    must be continuous, so a moving temperature resets ``stable_since``.
    """
    now = time.time() if now is None else now
    el = now - t0
    if el >= a.seconds:
        return False, stable_since
    if not getattr(a, "until_stable", False):
        return True, None
    if el < min(45.0, a.seconds):
        return True, None
    if not _temp_stable(samples):
        return True, None
    if stable_since is None:
        stable_since = now
    if now - stable_since >= getattr(a, "stable_hold", 60.0):
        return False, stable_since
    return True, stable_since


def phase_soak(a):
    np = _np()
    n = min(a.size, 2048)
    A = np.random.randn(n, n).astype(np.float32)
    B = np.random.randn(n, n).astype(np.float32)
    # Telemetry is only needed for the plateau decision; a fixed-duration soak
    # stays exactly as it was (on Windows the ACPI read costs real CPU).
    samples, stop, tel = [], threading.Event(), None
    if getattr(a, "until_stable", False):
        tel = threading.Thread(target=_telemetry_loop,
                               args=(samples, stop, _telemetry_interval()), daemon=True)
        tel.start()
    t0 = time.time()
    it = 0
    stable_since = None
    while True:
        keep, stable_since = _soak_decision(t0, a, samples, stable_since)
        if not keep:
            break
        _ = A @ B
        it += 1
    dt = time.time() - t0
    stop.set()
    if tel is not None:
        tel.join(timeout=5)
    out = {"phase": "soak", "seconds": round(dt, 1), "iters": it, "size": n,
           "gflops": round(2.0 * n ** 3 * it / dt / 1e9, 1),
           "until_stable": bool(getattr(a, "until_stable", False)),
           "stable": stable_since is not None,
           "stable_at_s": (round(stable_since - t0, 1) if stable_since is not None else None),
           "stable_hold_s": float(getattr(a, "stable_hold", 60.0))}
    ts = _telemetry_summary(samples)
    if ts:
        out["telemetry"] = ts
    print("soak %.1fs iters=%d GFLOPS=%.1f%s" % (
        dt, it, out["gflops"],
        (" stable@%.0fs+%.0fs" % (out["stable_at_s"], out["stable_hold_s"]))
        if out["stable"] else ""))
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
    np = _np()
    n = 64 * 1024 * 1024
    x = np.random.rand(n).astype(np.float32); y = np.random.rand(n).astype(np.float32); z = np.empty_like(x)
    r = timed(a.seconds, lambda: np.add(np.multiply(x, y, out=z), z, out=z), flops=2 * n)
    gf = round(2 * n * r["iters"] / r["seconds"] / 1e9, 1)
    out = {"phase": "flops", "gflops": gf, "elements": n, "dtype": "fp32"}
    print("flops (fp32 mul+add) %.1f GFLOPS" % gf)
    jline(out)


# --- thread scaling ---------------------------------------------------------

def _thread_env(k):
    """Environment for one scaling child: BLAS/OpenMP thread counts are read at
    numpy import time, so each count needs its own interpreter."""
    env = dict(os.environ)
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        env[var] = str(k)
    return env


def _run_scaling_child(n, seconds, threads):
    """Measure one thread count in a child process and return its RESULT_JSON."""
    cmd = [sys.executable, os.path.abspath(__file__), "scaling-one",
           "--size", str(n), "--seconds", str(seconds), "--threads", str(threads)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           env=_thread_env(threads), timeout=seconds * 5 + 120)
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    for line in (p.stdout or "").splitlines():
        if line.startswith("RESULT_JSON:"):
            return json.loads(line[len("RESULT_JSON:"):])
    return {"error": "no RESULT_JSON from child", "rc": p.returncode,
            "stderr": (p.stderr or "")[-300:]}


def _scaling_efficiency(gflops, threads, base_gflops):
    """Parallel efficiency relative to 1 thread: GFLOPS(k) / (k * GFLOPS(1)).

    1.0 means the count scaled linearly with the threads, 0.5 half of linear.
    None when there is no 1-thread baseline to divide by.
    """
    if not base_gflops or not gflops or not threads:
        return None
    return round(gflops / (threads * base_gflops), 3)


def phase_scaling_one(a):
    """One thread count for the scaling curve; a child of phase_scaling."""
    np = _np()
    n = min(a.size, 4096)
    r = _measure_matmul(n, a.seconds, np.float32)
    out = {"phase": "scaling-one", "threads": a.threads, "n": n,
           "iters": r["iters"], "seconds": r["seconds"], "gflops": r["gflops"]}
    print("scaling-one threads=%d N=%d GFLOPS=%.1f" % (a.threads, n, r["gflops"]))
    jline(out)


def phase_scaling(a):
    """Matmul GFLOPS at 1/2/4/8 threads and all logical cores.

    Aggregate GFLOPS cannot say whether the cores scale or the clock does; the
    curve (and its efficiency relative to 1 thread) is the like-for-like way to
    compare a low-core handheld with a many-thread desktop. N is recorded so
    curves from different machines are only compared at the same size.
    """
    n = min(a.size, 4096)
    all_cores = os.cpu_count() or 1
    counts = [1, 2, 4, 8]
    if all_cores not in counts:
        counts.append(all_cores)
    out = {"phase": "scaling", "n": n, "dtype": "fp32", "all_cores": all_cores,
           "results": {}, "efficiency": {}}
    base = None
    for k in counts:
        r = _run_scaling_child(n, a.seconds, k)
        out["results"][str(k)] = r
        if r.get("gflops") is not None:
            if k == 1:
                base = r["gflops"]
            eff = _scaling_efficiency(r["gflops"], k, base)
            if eff is not None:
                out["efficiency"][str(k)] = eff
        print("scaling threads=%-3d GFLOPS=%s efficiency=%s" % (
            k, r.get("gflops"), out["efficiency"].get(str(k))))
    jline(out)


PHASES = {"info": phase_info, "matmul": phase_matmul, "membw": phase_membw, "soak": phase_soak,
          "hash": phase_hash, "compress": phase_compress, "aes": phase_aes, "flops": phase_flops,
          "scaling": phase_scaling, "scaling-one": phase_scaling_one}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=sorted(PHASES))
    ap.add_argument("--seconds", type=float, default=8.0, help="per-measurement seconds")
    ap.add_argument("--size", type=int, default=4096, help="matrix size N")
    ap.add_argument("--until-stable", action="store_true",
                    help="soak until the CPU temperature plateaus (--seconds is the ceiling)")
    ap.add_argument("--stable-hold", type=float, default=60.0,
                    help="keep soaking this many seconds after the plateau is reached")
    ap.add_argument("--threads", type=int, default=1, help="thread count for scaling-one (internal)")
    a = ap.parse_args()
    PHASES[a.phase](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
