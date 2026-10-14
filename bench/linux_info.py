#!/usr/bin/env python3
"""System inventory for the bench-test toolkit (Linux subset of Get-SystemInfo.ps1).

One phase, `info`: OS/kernel, CPU, memory, GPU + driver + PCIe link, block devices,
network interfaces, boot time, and the error count since boot.

    RESULT_JSON:{"phase":"info", ...}
"""
import argparse
import glob
import json
import os
import platform
import re
import subprocess

import redact  # opt-in PII redaction (BENCH_REDACT); pass-through when disabled


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
               "--query-gpu=name,driver_version,memory.total,pcie.link.gen.current,"
               "pcie.link.width.current,uuid,vbios_version,pci.bus_id,"
               "temperature.gpu.tlimit,power.default_limit,power.max_limit",
               "--format=csv,noheader"])
    if not out:
        return None
    p = [x.strip() for x in out.splitlines()[0].split(",")]
    info = {"name": p[0], "driver": p[1], "vram": p[2],
            "pcie_link_gen": p[3], "pcie_link_width": p[4]}
    if len(p) >= 11:
        info.update({"uuid": p[5], "vbios": p[6], "pci_bus": p[7],
                     "temp_limit_c": p[8], "power_default_w": p[9], "power_max_w": p[10]})
    apps = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader"], timeout=10)
    procs = [x.strip() for x in apps.splitlines() if x.strip()]
    if procs:
        info["compute_apps"] = procs[:12]   # other GPU users at run start (validity)
    return info


def _int(v):
    try:
        return int(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def disk_health():
    """NVMe SMART via nvme-cli (and a smartctl fallback for SATA). Best-effort."""
    out = {}
    listing = run(["nvme", "list", "-o", "json"], timeout=10)
    if listing:
        try:
            devs = json.loads(listing).get("Devices", [])
        except Exception:
            devs = []
        for d in devs:
            dev = d.get("DevicePath")
            if not dev:
                continue
            try:
                j = json.loads(run(["nvme", "smart-log", dev, "-o", "json"], timeout=10) or "{}")
            except Exception:
                j = {}
            k = j.get("temperature")
            out[dev] = {"model": d.get("ModelNumber"), "fw": d.get("Firmware"),
                        "temp_c": round(k - 273.15, 1) if isinstance(k, (int, float)) and k else None,
                        "used_pct": _int(j.get("percent_used")),
                        "media_errors": _int(j.get("media_errors")),
                        "unsafe_shutdowns": _int(j.get("unsafe_shutdowns")),
                        "critical_warning": _int(j.get("critical_warning"))}
    for dev in sorted(glob.glob("/dev/sd[a-z]") + glob.glob("/dev/nvme[0-9]n[0-9]")):
        if dev in out:
            continue
        j = run(["smartctl", "-H", "-A", "-j", dev], timeout=10)
        if not j:
            continue
        try:
            s = json.loads(j)
        except Exception:
            continue
        t = (s.get("temperature") or {}).get("current")
        attrs = {a.get("name"): a.get("raw", {}).get("value") for a in s.get("ata_smart_attributes", {}).get("table", [])}
        out[dev] = {"model": s.get("model_name"), "fw": s.get("firmware_version"),
                    "temp_c": t, "used_pct": None,
                    "media_errors": None,
                    "reallocated": attrs.get("Reallocated_Sector_Ct"),
                    "pending": attrs.get("Current_Pending_Sector"),
                    "power_on_hours": attrs.get("Power_On_Hours"),
                    "health": (s.get("smart_status") or {}).get("passed")}
    return out


def edac():
    """ECC correctable/uncorrectable counts from EDAC (empty when unsupported)."""
    tot = {"ce": 0, "ue": 0, "present": False}
    for f in glob.glob("/sys/devices/system/edac/mc/mc*/ce_count") + \
             glob.glob("/sys/devices/system/edac/mc/mc*/csrow*/ce_count") + \
             glob.glob("/sys/devices/system/edac/mc/mc*/dimm*/dimm_ce_count"):
        try:
            tot["ce"] += int(read(f) or 0)
            tot["present"] = True
        except ValueError:
            pass
    for f in glob.glob("/sys/devices/system/edac/mc/mc*/ue_count") + \
             glob.glob("/sys/devices/system/edac/mc/mc*/csrow*/ue_count") + \
             glob.glob("/sys/devices/system/edac/mc/mc*/dimm*/dimm_ue_count"):
        try:
            tot["ue"] += int(read(f) or 0)
            tot["present"] = True
        except ValueError:
            pass
    return tot if tot["present"] else {}


def hardware_errors():
    """Notable HW error lines since boot (Xid / MCE / AER / thermal). Best-effort."""
    pat = re.compile(r"Xid|Machine Check|mce:|AER|PCIe Bus Error|thermal event|"
                     r"hardware error|GPU has fallen off|corrected error", re.I)
    txt = run(["journalctl", "-k", "-b", "--no-pager"], timeout=30) or read("/var/log/dmesg")
    hits = [ln.strip()[:200] for ln in txt.splitlines() if pat.search(ln)]
    return hits[:40]


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
    ap.add_argument("--redact", action="store_true",
                    help="replace hardware identifiers and PII with stable pseudonyms "
                         "(same as BENCH_REDACT=1)")
    a = ap.parse_args()
    if a.redact:
        os.environ["BENCH_REDACT"] = "1"

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
        "hardware_errors": hardware_errors(),
        "disk_health": disk_health(),
        "ecc": edac(),
    }
    info = redact.redact_obj(info) if redact.is_enabled() else info
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
