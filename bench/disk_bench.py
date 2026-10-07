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

CAVEAT: uses OS-buffered I/O, so sequential *reads* of a just-written file can be
served from cache. Use a file larger than RAM for truer read numbers, and treat
results as a like-for-like comparison across drives rather than absolute peaks.
"""
import argparse
import json
import os
import random
import shutil
import sys
import time

BLK = 1 << 20      # 1 MiB
IO = 4096          # 4 KiB


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
        buf = b"\xA5" * BLK
        with open(f, "wb", buffering=0) as fh:
            for _ in range(a.size_mb):
                fh.write(buf)
            fh.flush(); os.fsync(fh.fileno())
    return f


def phase_seqwrite(a):
    f = datafile(a)
    buf = b"\x5A" * BLK
    t0 = time.time()
    with open(f, "wb", buffering=0) as fh:
        for _ in range(a.size_mb):
            fh.write(buf)
        fh.flush(); os.fsync(fh.fileno())
    dt = time.time() - t0
    mbps = a.size_mb / dt
    out = {"phase": "seqwrite", "size_mb": a.size_mb, "seconds": round(dt, 2), "mbps": round(mbps, 1)}
    print("disk seqwrite %.0f MB/s (%.1f s for %d MiB)" % (mbps, dt, a.size_mb))
    jline(out)


def phase_seqread(a):
    f = ensure_file(a)
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
    print("disk seqread %.0f MB/s (%.1f s for %d MiB)" % (mbps, dt, n // BLK))
    jline(out)


def rand_loop(a, read_pct):
    f = ensure_file(a)
    size = os.path.getsize(f)
    nmax = max(1, size // IO)
    rnd = random.Random(1)
    ops = 0; reads = 0; writes = 0
    t0 = time.time()
    with open(f, "r+b", buffering=0) as fh:
        while time.time() - t0 < a.seconds:
            off = rnd.randrange(nmax) * IO
            fh.seek(off)
            if rnd.randrange(100) < read_pct:
                fh.read(IO); reads += 1
            else:
                fh.write(b"\x3C" * IO); writes += 1
            ops += 1
        fh.flush(); os.fsync(fh.fileno())
    dt = time.time() - t0
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
    seq_mb = 0; rand_ops = 0
    while time.time() - t0 < a.seconds:
        # seq write then read
        buf = b"\x11" * BLK
        with open(datafile(a), "wb", buffering=0) as fh:
            for _ in range(min(a.size_mb, 256)):
                fh.write(buf)
            fh.flush(); os.fsync(fh.fileno())
            seq_mb += min(a.size_mb, 256)
        with open(datafile(a), "rb", buffering=0) as fh:
            while fh.read(BLK):
                pass
        # random mix burst
        ops, _, _, _ = rand_loop(a, 70)
        rand_ops += ops
    dt = time.time() - t0
    out = {"phase": "soak", "seconds": round(dt, 1), "seq_mib": seq_mb, "rand_ops": rand_ops,
           "seq_mbps_avg": round(seq_mb / dt, 1), "rand_iops_avg": round(rand_ops / dt, 0)}
    print("disk soak %.1fs seq=%.0f MB/s rand=%.0f IOPS" % (dt, out["seq_mbps_avg"], out["rand_iops_avg"]))
    jline(out)


PHASES = {"info": phase_info, "seqwrite": phase_seqwrite, "seqread": phase_seqread,
          "randread": phase_randread, "randwrite": phase_randwrite, "randmix": phase_randmix,
          "soak": phase_soak}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=sorted(PHASES))
    ap.add_argument("--path", default="", help="directory to test (default %%TEMP%%)")
    ap.add_argument("--size-mb", type=int, default=2048)
    ap.add_argument("--seconds", type=float, default=15.0)
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
