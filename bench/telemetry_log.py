#!/usr/bin/env python3
"""Time-series telemetry logger for the bench-test toolkit (Linux Sample-GpuTelemetry).

Samples NVIDIA/AMD GPU state, hwmon temperatures (`sensors -j` / sysfs), CPU load
and memory once per interval and appends one CSV row per sample to --out. Runs
until terminated (SIGTERM) or for --duration seconds. Intended to run alongside a
benchmark battery; the runner starts it at the beginning of a run and stops it at
the end.

Columns: elapsed_s, time, gpu_util_pct, mem_util_pct, temp_c, fan_pct, power_w,
         sm_mhz, mem_mhz, vram_used_mib, pstate, cpu_package_c, ram_used_gib, load1,
         cpu_max_core_c, swap_used_gib, cpu_util_pct, nvme_c, gpu_enc_pct, gpu_dec_pct
"""
import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime

GPU_FIELDS = ("utilization.gpu,utilization.memory,temperature.gpu,fan.speed,power.draw,"
              "clocks.sm,clocks.mem,memory.used,pstate,utilization.encoder,utilization.decoder")
HEADER = ("elapsed_s,time,gpu_util_pct,mem_util_pct,temp_c,fan_pct,power_w,"
          "sm_mhz,mem_mhz,vram_used_mib,pstate,cpu_package_c,ram_used_gib,load1,"
          "cpu_max_core_c,swap_used_gib,cpu_util_pct,nvme_c,gpu_enc_pct,gpu_dec_pct,"
          "cpu_fan_rpm,gpu_fan_rpm")


def gpu_sample():
    """NVIDIA via nvidia-smi; otherwise fall back to gpu_util (AMD/rocm-sysfs)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + GPU_FIELDS, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        if out:
            vals = [v.strip() for v in out.splitlines()[0].split(",")]
            if len(vals) == 11:
                return vals
    except Exception:
        pass
    try:
        import gpu_util
        s = gpu_util.sample() or {}
        if s.get("temp_c") is not None or s.get("fan_pct") is not None or s.get("power_w") is not None:
            def v(x):
                return "" if x is None else x
            return [v(s.get("util")), v(s.get("mem_util")), v(s.get("temp_c")), v(s.get("fan_pct")),
                    v(s.get("power_w")), v(s.get("sm_mhz")), v(s.get("mem_mhz")),
                    v(s.get("vram_used_mib")), s.get("pstate") or "", "", ""]
    except Exception:
        pass
    return None


def sensors_temps():
    """(package_c, max_core_c, max_fan_rpm) from `sensors -j`; any may be ''."""
    package = maxcore = maxfan = ""
    try:
        out = subprocess.run(["sensors", "-j"], capture_output=True, text=True, timeout=5).stdout
        data = json.loads(out)
        cores = []
        fans = []
        for _chip, sensors in data.items():
            for name, vals in sensors.items():
                if not isinstance(vals, dict):
                    continue
                for k, v in vals.items():
                    if not isinstance(v, (int, float)):
                        continue
                    if k.startswith("temp") and k.endswith("_input"):
                        if "Package" in name and package == "":
                            package = v
                        elif "core" in name.lower():
                            cores.append(v)
                    elif k.startswith("fan") and k.endswith("_input"):
                        fans.append(v)
        if package == "" and cores:
            package = max(cores)
        if cores:
            maxcore = max(cores)
        if fans:
            maxfan = int(max(fans))
    except Exception:
        pass
    return package, maxcore, maxfan


def gpu_fan_rpm():
    """AMD sysfs first, then NVIDIA via nvidia-settings (needs a display)."""
    for path in glob.glob("/sys/class/drm/card[0-9]*/device/hwmon/hwmon*/fan1_input"):
        try:
            with open(path) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            pass
    ns = "/opt/nvidia-settings/usr/bin/nvidia-settings"
    if not os.path.exists(ns):
        ns = shutil.which("nvidia-settings")
    if ns and os.environ.get("DISPLAY"):
        env = os.environ.copy()
        for lib in ("/opt/nvidia-settings/usr/lib", "/usr/lib/nvidia"):
            if os.path.isdir(lib):
                env["LD_LIBRARY_PATH"] = lib
        try:
            r = subprocess.run([ns, "-q", "GPUCurrentFanSpeedRPM", "-t"], capture_output=True,
                               text=True, timeout=5, env=env)
            return float(r.stdout.strip().splitlines()[-1])
        except Exception:
            pass
    return ""


def nvme_temp():
    best = ""
    for path in glob.glob("/sys/class/nvme/nvme*/hwmon*/temp1_input"):
        try:
            with open(path) as fh:
                val = int(fh.read().strip()) / 1000.0
            best = max(best, val) if isinstance(best, float) else val
        except (OSError, ValueError):
            pass
    return best


def meminfo():
    """(ram_used_gib, swap_used_gib) from /proc/meminfo."""
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                if ":" in line:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0])
        total = info.get("MemTotal", 0) / 1048576.0
        avail = info.get("MemAvailable", 0) / 1048576.0
        swap = info.get("SwapTotal", 0) / 1048576.0
        swap_free = info.get("SwapFree", 0) / 1048576.0
        return round(total - avail, 2), round(swap - swap_free, 2)
    except Exception:
        return "", ""


def cpu_snapshot():
    try:
        with open("/proc/stat") as fh:
            parts = fh.readline().split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=0, help="0 = run until killed")
    a = ap.parse_args()

    stop = {"now": False}

    def _term(_signum, _frame):
        stop["now"] = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    t0 = time.time()
    prev_cpu = cpu_snapshot()
    with open(a.out, "w") as fh:
        fh.write(HEADER + "\n")
        fh.flush()
        while not stop["now"]:
            g = gpu_sample()
            cur_cpu = cpu_snapshot()
            cpu_pct = ""
            if prev_cpu and cur_cpu:
                di = cur_cpu[0] - prev_cpu[0]
                dt = cur_cpu[1] - prev_cpu[1]
                cpu_pct = round(100.0 * (1.0 - di / dt), 1) if dt > 0 else ""
            prev_cpu = cur_cpu
            pkg, maxcore, maxfan = sensors_temps()
            ram, swap = meminfo()
            try:
                load1 = round(os.getloadavg()[0], 2)
            except Exception:
                load1 = ""
            # GPU fields go blank when no driver tool answers; the rest of the
            # row is still recorded so dashboards show system load regardless.
            if g:
                gpu = g[:9]
                enc = g[9] if g[9] not in ("[N/A]", "N/A") else ""
                dec = g[10] if g[10] not in ("[N/A]", "N/A") else ""
            else:
                gpu = [""] * 9
                enc = dec = ""
            row = ([round(time.time() - t0, 1), datetime.now().strftime("%H:%M:%S")] + gpu
                   + [pkg, ram, load1, maxcore, swap, cpu_pct, nvme_temp(), enc, dec,
                      maxfan, gpu_fan_rpm()])
            fh.write(",".join(str(x) for x in row) + "\n")
            fh.flush()
            if a.duration and time.time() - t0 >= a.duration:
                break
            time.sleep(max(0.2, a.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
