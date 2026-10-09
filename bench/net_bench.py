#!/usr/bin/env python3
"""Network benchmark for the bench-test toolkit (Linux port of Invoke-NetBench.ps1).

One phase, `bench`: ICMP ping latency to a target, plus download/upload throughput
against Cloudflare's speed endpoints (same service the PowerShell script uses).

Prints human-readable lines and a single machine-readable summary:

    RESULT_JSON:{"phase":"bench", ...}
"""
import argparse
import json
import os
import re
import subprocess
import time
import urllib.request

UA = "Mozilla/5.0"


def jline(obj):
    print("RESULT_JSON:" + json.dumps(obj), flush=True)


def ping_stats(target, count, interval):
    try:
        out = subprocess.run(
            ["ping", "-c", str(count), "-i", str(interval), target],
            capture_output=True, text=True, timeout=count * 3 + 15).stdout
        m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/", out)
        if m:
            return {"min": float(m.group(1)), "avg": float(m.group(2)), "max": float(m.group(3))}
    except Exception as exc:
        print("ping failed: %s" % exc)
    return None


def download_mbps(url, megabytes, chunk_mb=25, timeout=90):
    """Cloudflare rejects large `bytes=` values (>=100MB -> 403), so fetch in chunks."""
    total = 0
    t0 = time.time()
    try:
        remaining = megabytes
        while remaining > 0:
            n = min(chunk_mb, remaining)
            req = urllib.request.Request(url % (n << 20), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                total += len(r.read())
            remaining -= n
    except Exception as exc:
        print("download error after %d bytes: %s" % (total, exc))
    dt = time.time() - t0
    if total:
        return round(total * 8 / dt / 1e6, 1), total
    return None, 0


def upload_mbps(url, megabytes, timeout=120):
    payload = os.urandom(1 << 20) * megabytes
    try:
        req = urllib.request.Request(url, data=payload, method="POST", headers={"User-Agent": UA})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        dt = time.time() - t0
        return round(len(payload) * 8 / dt / 1e6, 1)
    except Exception as exc:
        print("upload failed: %s" % exc)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["bench"])
    ap.add_argument("--target", default="1.1.1.1")
    ap.add_argument("--ping-count", type=int, default=10)
    ap.add_argument("--down-mb", type=int, default=100)
    ap.add_argument("--up-mb", type=int, default=25)
    a = ap.parse_args()

    ping = ping_stats(a.target, a.ping_count, 0.2)
    if ping:
        print("ping min/avg/max %.1f/%.1f/%.1f ms (%s)" % (
            ping["min"], ping["avg"], ping["max"], a.target))
    down, nbytes = download_mbps(
        "https://speed.cloudflare.com/__down?bytes=%d", a.down_mb)
    print(("download %.1f Mbps" % down) if down is not None else "download failed")
    up = upload_mbps("https://speed.cloudflare.com/__up", a.up_mb)
    print(("upload %.1f Mbps" % up) if up is not None else "upload failed")
    jline({"phase": "bench", "target": a.target, "ping_ms": ping,
           "download_mbps": down, "download_bytes": nbytes, "upload_mbps": up})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
