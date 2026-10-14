#!/usr/bin/env python3
"""Disk (HDD/SSD/NVMe) benchmark for the bench-test toolkit.

Phases (human lines + one "RESULT_JSON:" line):
  info       - target path, free/total space, filesystem
  seqwrite   - sequential write MB/s (1 MiB blocks, flush+fsync)
  seqread    - sequential read MB/s
  randread   - random 4 KiB read IOPS + MB/s (for N seconds)
  randwrite  - random 4 KiB write IOPS + MB/s
  randmix    - 70% read / 30% write 4 KiB mixed
  soak       - alternate seq r/w + random r/w for N seconds
  steadywrite- 10-20 GiB steady write in flush-bounded windows
  sustainwrite - opt-in sustained write (--sustain-gb N) with per-window throughput

CAVEAT: uses OS-buffered I/O, so sequential *reads* of a just-written file can be
served from cache. Where the OS allows it the read phases drop the page cache
first (Linux posix_fadvise DONTNEED); Windows stdlib has no equivalent, so those
phases record `cache_flushed`/`cache_served` instead - a cache-served number is
still cache speed, not device throughput. Use a file larger than RAM for truer
read numbers, and treat results as a like-for-like comparison across drives
rather than absolute peaks.
"""
import argparse
import json
import os
import random
import shutil
import statistics
import sys
import time

import numpy as np

BLK = 1 << 20      # 1 MiB

# Flush this often inside the write phases. With a single flush at the end the
# page cache absorbs the whole file and the number measures free RAM instead of
# the device: 512 MiB reported 45 MB/s that way and 128 MiB 279 MB/s, while the
# same machine measured 257-278 MB/s at either size when flushed every 32 MiB.
FLUSH_EVERY_MB = 32
IO = 4096          # 4 KiB

_RANDBUF = b""


def randbytes(n):
    """Incompressible fill data: constant bytes get zstd-compressed by btrfs/bcachefs,
    which inflates throughput results on such filesystems."""
    global _RANDBUF
    if len(_RANDBUF) < n:
        _RANDBUF = os.urandom(max(n, BLK))
    return _RANDBUF[:n]


def jline(obj):
    print("RESULT_JSON:" + json.dumps(obj), flush=True)


def target_dir(a):
    return a.path or os.environ.get("TEMP", ".")


def datafile(a):
    return os.path.join(target_dir(a), "benchtest_disk.tmp")


def phase_info(a):
    t = target_dir(a)
    try:
        u = shutil.disk_usage(t)
        out = {"phase": "info", "path": t, "total_gib": round(u.total / 1024 ** 3, 1),
               "free_gib": round(u.free / 1024 ** 3, 1)}
    except Exception as e:
        out = {"phase": "info", "path": t, "error": str(e)}
    print("disk path=%s total=%sGiB free=%sGiB" % (t, out.get("total_gib"), out.get("free_gib")))
    jline(out)


def ensure_file(a):
    f = datafile(a)
    if not os.path.exists(f) or os.path.getsize(f) < a.size_mb * BLK:
        buf = randbytes(BLK)
        with open(f, "wb", buffering=0) as fh:
            for _ in range(a.size_mb):
                fh.write(buf)
            fh.flush(); os.fsync(fh.fileno())
    return f


def _ram_bytes():
    """Installed RAM in bytes, or None when it cannot be read (stdlib only)."""
    try:
        with open("/proc/meminfo") as fh:
            for ln in fh:
                if ln.startswith("MemTotal:"):
                    return int(ln.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    if os.name == "nt":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MS()
            st.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullTotalPhys
        except Exception:
            pass
    return None


def _drop_cache(path):
    """Evict `path` from the page cache where the OS allows it; (flushed, method).

    Linux posix_fadvise(POSIX_FADV_DONTNEED) tells the kernel the cached pages of
    the just-written file will not be needed again, so the read that follows hits
    the device. Windows has no stdlib equivalent (FILE_FLAG_NO_BUFFERING needs a
    native handle, not an os.open one), so the caller records `cache_served`
    instead of pretending a cache-speed read is device throughput.
    """
    if hasattr(os, "posix_fadvise") and hasattr(os, "POSIX_FADV_DONTNEED"):
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)
            return True, "posix_fadvise_dontneed"
        except OSError:
            pass
    return False, "unavailable"


def _read_cache_note(a):
    """Result fields saying whether a read phase measured the device or the cache.

    With no way to flush, a file that fits in RAM can be fully served from the
    page cache: `cache_served` marks the number as not being device throughput.
    A larger file cannot be cached whole, though parts of it still may be.
    """
    f = datafile(a)
    flushed, method = _drop_cache(f)
    note = {"cache_flushed": flushed, "cache_flush_method": method}
    if not flushed:
        ram = _ram_bytes()
        note["cache_served"] = bool(ram and os.path.getsize(f) <= ram)
    return note


def phase_seqwrite(a):
    f = datafile(a)
    buf = randbytes(BLK)
    t0 = time.time()
    with open(f, "wb", buffering=0) as fh:
        for i in range(a.size_mb):
            fh.write(buf)
            if (i + 1) % FLUSH_EVERY_MB == 0:
                fh.flush(); os.fsync(fh.fileno())
        fh.flush(); os.fsync(fh.fileno())
    dt = time.time() - t0
    mbps = a.size_mb / dt
    out = {"phase": "seqwrite", "size_mb": a.size_mb, "seconds": round(dt, 2), "mbps": round(mbps, 1)}
    print("disk seqwrite %.0f MB/s (%.1f s for %d MiB)" % (mbps, dt, a.size_mb))
    jline(out)


def phase_seqread(a):
    f = ensure_file(a)
    note = _read_cache_note(a)      # the just-written file is in the page cache
    t0 = time.time()
    n = 0
    with open(f, "rb", buffering=0) as fh:
        while True:
            b = fh.read(BLK)
            if not b:
                break
            n += len(b)
    dt = time.time() - t0
    mbps = (n / BLK) / dt
    out = {"phase": "seqread", "size_mb": n // BLK, "seconds": round(dt, 2), "mbps": round(mbps, 1)}
    out.update(note)
    print("disk seqread %.0f MB/s (%.1f s for %d MiB)" % (mbps, dt, n // BLK))
    jline(out)


def rand_loop(a, read_pct):
    f = ensure_file(a)
    size = os.path.getsize(f)
    nmax = max(1, size // IO)
    rnd = random.Random(1)
    ops = 0; reads = 0; writes = 0; last_flush = time.time()
    t0 = time.time()
    with open(f, "r+b", buffering=0) as fh:
        while time.time() - t0 < a.seconds:
            off = rnd.randrange(nmax) * IO
            fh.seek(off)
            if rnd.randrange(100) < read_pct:
                fh.read(IO); reads += 1
            else:
                fh.write(randbytes(IO)); writes += 1
            ops += 1
            # Writes only reach the page cache until they are flushed, so a random-write
            # figure measured without one is RAM speed. Flush about once a second: enough
            # to keep the device involved without swamping the loop.
            if writes and time.time() - last_flush >= 1.0:
                fh.flush(); os.fsync(fh.fileno())
                last_flush = time.time()
        # stop the clock before the closing flush: it is durability, not speed
        dt = time.time() - t0
        fh.flush(); os.fsync(fh.fileno())
    return ops, reads, writes, dt


def _rand_phase(a, name, read_pct):
    ops, reads, writes, dt = rand_loop(a, read_pct)
    iops = ops / dt
    out = {"phase": name, "seconds": round(dt, 1), "ops": ops, "reads": reads, "writes": writes,
           "iops": round(iops, 0), "mbps": round(ops * IO / dt / 1e6, 1)}
    print("disk %-9s %.0f IOPS, %.1f MB/s (r=%d w=%d)" % (name, iops, out["mbps"], reads, writes))
    jline(out)


def phase_randread(a):  _rand_phase(a, "randread", 100)
def phase_randwrite(a): _rand_phase(a, "randwrite", 0)
def phase_randmix(a):   _rand_phase(a, "randmix", 70)


def phase_soak(a):
    t0 = time.time()
    seq_mb = 0; rand_ops = 0; seq_secs = 0.0
    while time.time() - t0 < a.seconds:
        # seq write then read
        buf = randbytes(BLK)
        st = time.time()
        with open(datafile(a), "wb", buffering=0) as fh:
            for _ in range(min(a.size_mb, 256)):
                fh.write(buf)
            fh.flush(); os.fsync(fh.fileno())
            seq_mb += min(a.size_mb, 256)
        seq_secs += time.time() - st
        with open(datafile(a), "rb", buffering=0) as fh:
            while fh.read(BLK):
                pass
        # random mix burst
        ops, _, _, _ = rand_loop(a, 70)
        rand_ops += ops
    dt = time.time() - t0
    seq_mbps = round(seq_mb / seq_secs, 1) if seq_secs else 0
    out = {"phase": "soak", "seconds": round(dt, 1), "seq_mib": seq_mb, "rand_ops": rand_ops,
           "seq_write_mbps": seq_mbps, "rand_iops_avg": round(rand_ops / dt, 0)}
    print("disk soak %.1fs seq_write=%.0f MB/s rand=%.0f IOPS" % (dt, seq_mbps, out["rand_iops_avg"]))
    jline(out)


def _window_stats(window_secs, window_bytes):
    """Per-window throughput summary for the flush-bounded write phases.

    Every window's MB/s is kept (not just deciles), so a post-SLC-cache collapse
    is visible in the result instead of averaged away. ``window_secs`` are the
    fsync-bounded durations, one per window.
    """
    mbps = [round(window_bytes / dt / 1e6, 1) for dt in window_secs if dt > 0]
    if not mbps:
        return {}
    first, last = mbps[0], mbps[-1]
    return {"windows": len(mbps),
            "windows_mbps": mbps,
            "first_mbps": first,
            "last_mbps": last,
            "min_mbps": min(mbps),
            "p50_mbps": round(statistics.median(mbps), 1),
            "drop_pct": round((1 - last / first) * 100, 1) if first else None}


def phase_steadywrite(a):
    f = datafile(a)
    chunk = 64 * BLK           # 64 MiB fsync-bounded windows
    total = int(a.steady_gb) * 1024 * 1024 * 1024
    buf = randbytes(chunk)
    buckets = []
    written = 0
    t0 = time.time()
    with open(f, "wb", buffering=0) as fh:
        while written < total:
            bt = time.time()
            fh.write(buf)
            os.fsync(fh.fileno())   # window = data actually on stable storage
            buckets.append(chunk / (time.time() - bt))
            written += chunk
    dt = time.time() - t0
    n = len(buckets); k = max(1, n // 10)
    first = sum(buckets[:k]) / k / 1e6
    last = sum(buckets[-k:]) / k / 1e6
    overall = written / dt / 1e6
    out = {"phase": "steadywrite", "gib": written // (1024 ** 3), "seconds": round(dt, 1),
           "overall_mbps": round(overall, 1), "first10_mbps": round(first, 1), "last10_mbps": round(last, 1),
           "drop_pct": round((1 - last / first) * 100, 1) if first else None}
    print("disk steadywrite %.0f GiB: first10=%.0f last10=%.0f overall=%.0f MB/s (drop %.0f%%)" % (
        out["gib"], first, last, overall, out["drop_pct"] or 0))
    jline(out)


def phase_sustainwrite(a):
    """Sustained write: N GiB in 64 MiB fsync-bounded windows.

    The steady write's 10-20 GiB can fit inside an SLC cache, so its average is
    cache speed; the per-window list shows where the device falls off once the
    cache is full. Opt-in (--sustain-gb N, default off).
    """
    total = int(a.sustain_gb * 1024 ** 3)
    if total <= 0:
        out = {"phase": "sustainwrite", "skipped": True,
               "reason": "--sustain-gb N is required (default off)"}
        print("disk sustainwrite skipped (pass --sustain-gb N)")
        jline(out)
        return
    f = datafile(a)
    chunk = 64 * BLK           # 64 MiB fsync-bounded windows
    buf = randbytes(chunk)
    secs = []
    written = 0
    t0 = time.time()
    with open(f, "wb", buffering=0) as fh:
        while written < total:
            bt = time.perf_counter()
            fh.write(buf)
            os.fsync(fh.fileno())   # window = data actually on stable storage
            secs.append(time.perf_counter() - bt)
            written += chunk
    dt = time.time() - t0
    out = {"phase": "sustainwrite", "gib": round(written / 1024 ** 3, 2),
           "seconds": round(dt, 1), "window_mib": chunk // BLK,
           "overall_mbps": round(written / dt / 1e6, 1)}
    out.update(_window_stats(secs, chunk))
    print("disk sustainwrite %.2f GiB in %s windows: first=%.0f last=%.0f min=%.0f overall=%.0f MB/s (drop %s%%)" % (
        out["gib"], out.get("windows"), out.get("first_mbps"), out.get("last_mbps"),
        out.get("min_mbps"), out["overall_mbps"], out.get("drop_pct")))
    jline(out)


def phase_latency(a):
    f = ensure_file(a)
    note = _read_cache_note(a)      # random 4K reads of cached pages are RAM latency
    size = os.path.getsize(f)
    nmax = max(1, size // IO)
    rnd = random.Random(2)
    times = []
    ops = 0
    t0 = time.time()
    with open(f, "rb", buffering=0) as fh:
        while time.time() - t0 < a.seconds and ops < 300000:
            off = rnd.randrange(nmax) * IO
            t = time.perf_counter()
            fh.seek(off); fh.read(IO)
            times.append(time.perf_counter() - t)
            ops += 1
    dt = time.time() - t0
    arr = np.array(times) * 1e6  # microseconds
    p50, p90, p99, p999 = (np.percentile(arr, [50, 90, 99, 99.9])).tolist()
    out = {"phase": "latency", "ops": ops, "iops": round(ops / dt, 0),
           "us_p50": round(p50, 1), "us_p90": round(p90, 1), "us_p99": round(p99, 1), "us_p999": round(p999, 1)}
    out.update(note)
    print("disk 4K latency %.0f IOPS: p50=%.0f p90=%.0f p99=%.0f p99.9=%.0f us" % (
        out["iops"], p50, p90, p99, p999))
    jline(out)


PHASES = {"info": phase_info, "seqwrite": phase_seqwrite, "seqread": phase_seqread,
          "randread": phase_randread, "randwrite": phase_randwrite, "randmix": phase_randmix,
          "steadywrite": phase_steadywrite, "sustainwrite": phase_sustainwrite,
          "latency": phase_latency, "soak": phase_soak}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=sorted(PHASES))
    ap.add_argument("--path", default="", help="directory to test (default %%TEMP%%)")
    ap.add_argument("--size-mb", type=int, default=2048)
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--steady-gb", type=float, default=20.0, help="steadywrite size (GiB)")
    ap.add_argument("--sustain-gb", type=float, default=0.0,
                    help="sustainwrite size (GiB); 0 = off")
    ap.add_argument("--keep", action="store_true", help="keep the test file")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    try:
        PHASES[a.phase](a)
    except Exception as e:
        jline({"phase": a.phase, "error": str(e)})
    if not a.keep:
        try:
            os.remove(datafile(a))
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
