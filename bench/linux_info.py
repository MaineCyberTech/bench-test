#!/usr/bin/env python3
"""System inventory for the bench-test toolkit (Linux subset of Get-SystemInfo.ps1).

One phase, `info`: OS/kernel, CPU, memory, GPU + driver + PCIe link, block devices,
network interfaces, boot time, and the error count since boot.

    RESULT_JSON:{"phase":"info", ...}
"""
import argparse
import json
import os
import platform
import re
import subprocess


def jline(obj):
    print("RESULT_JSON:" + json.dumps(obj), flush=True)


def run(args, timeout=15):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return ""


def cpu_info():
    txt = read("/proc/cpuinfo")
    model = ""
    for line in txt.splitlines():
        if line.lower().startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    mhz = []
    for line in txt.splitlines():
        if line.startswith("cpu MHz"):
            try:
                mhz.append(float(line.split(":", 1)[1]))
            except ValueError:
                pass
    return {"model": model,
            "logical_cpus": len([l for l in txt.splitlines() if l.startswith("processor")]),
            "max_mhz_observed": round(max(mhz)) if mhz else None}


def mem_gib():
    m = re.search(r"MemTotal:\s+(\d+) kB", read("/proc/meminfo"))
    return round(int(m.group(1)) / 1024 / 1024, 1) if m else None


def gpu_info():
    out = run(["nvidia-smi",
               "--query-gpu=name,driver_version,memory.total,pcie.link.gen.current,pcie.link.width.current",
               "--format=csv,noheader"])
    if not out:
        return None
    p = [x.strip() for x in out.splitlines()[0].split(",")]
    return {"name": p[0], "driver": p[1], "vram": p[2],
            "pcie_link_gen": p[3], "pcie_link_width": p[4]}


def storage():
    out = run(["lsblk", "-dno", "NAME,SIZE,TYPE,ROTA,MODEL"])
    return [" ".join(l.split()) for l in out.splitlines() if l.strip()]


def net_ifaces():
    base = "/sys/class/net"
    ifaces = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return ifaces
    for n in names:
        if n == "lo":
            continue
        ifaces.append({"name": n,
                       "state": read(os.path.join(base, n, "operstate")),
                       "speed_mbps": read(os.path.join(base, n, "speed"))})
    return ifaces


def error_count():
    out = run(["journalctl", "-p", "err", "-b", "--no-pager"], timeout=30)
    return len([l for l in out.splitlines() if l.strip()]) if out else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["info"])
    a = ap.parse_args()

    distro = ""
    for line in read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            distro = line.split("=", 1)[1].strip('"')
            break
    info = {
        "phase": "info",
        "distro": distro,
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpu": cpu_info(),
        "memory_gib": mem_gib(),
        "gpu": gpu_info(),
        "storage": storage(),
        "network_ifaces": net_ifaces(),
        "booted": run(["uptime", "-s"]),
        "errors_since_boot": error_count(),
    }
    g = info["gpu"] or {}
    print("host=%s kernel=%s cpu=%s (%s threads) mem=%.1fGiB gpu=%s driver=%s" % (
        info["distro"], info["kernel"], info["cpu"]["model"],
        info["cpu"]["logical_cpus"], info["memory_gib"] or 0,
        g.get("name", "?"), g.get("driver", "?")))
    print("storage: " + "; ".join(info["storage"][:4]))
    print("ifaces: " + ", ".join("%s(%s)" % (i["name"], i["state"]) for i in info["network_ifaces"]))
    jline(info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
