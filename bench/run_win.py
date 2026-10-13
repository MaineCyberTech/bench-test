#!/usr/bin/env python3
"""run_win.py - Windows runner for the bench-test dashboards (GUI/TUI).

Accepts the same flags as Run-LinuxBench.sh, runs the Windows battery
(bench/Run-WorkstationBench.ps1), re-emits its phases as "[group] ..." lines the
dashboards parse, and writes a 1 Hz telemetry CSV (<host>-win-<stamp>.telemetry.csv).

    python bench/run_win.py [-o DIR] [--gpu-soak S] [--combined-seconds S]
        [--fullsoak-seconds S] [--gfx-seconds S] [--skip-gpu] ... [--valley-bin DIR]
"""
import argparse
import csv
import os
import re
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "bench")
BATTERY = os.path.join(BENCH, "Run-WorkstationBench.ps1")
DEFAULT_VALLEY = r"C:\Program Files (x86)\Unigine\Valley Benchmark 1.0\bin"

# battery phase index (from "=== [n/11] TITLE ===") -> dashboard group
PHASE_GROUP = {1: "sys", 2: "gpu", 3: "cpu", 4: "combined", 5: "llm",
               6: "nvenc", 7: "gfx", 8: "ram", 9: "disk", 10: "net", 11: "fullsoak"}


def parse_args():
    ap = argparse.ArgumentParser()
    for g in ("gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "sys", "gfx",
              "llm", "fan"):
        ap.add_argument("--skip-" + g, action="store_true")
    ap.add_argument("--gpu-soak", default="600")
    ap.add_argument("--combined-seconds", default="300")
    ap.add_argument("--fullsoak-seconds", default="300")
    ap.add_argument("--gfx-seconds", default="60")
    ap.add_argument("--cpu-soak", default="120")
    ap.add_argument("--ram-soak", default="120")
    ap.add_argument("--disk-seconds", default="15")
    ap.add_argument("--steady-gb", default="10")
    ap.add_argument("--gpu-size", default="12288")
    ap.add_argument("--cpu-size", default="4096")
    ap.add_argument("--ram-size-mb", default="1024")
    ap.add_argument("--passes", default="2")
    ap.add_argument("--soak-until-stable", action="store_true",
                    help="soak until the GPU temperature plateaus (the soak ceiling still applies)")
    ap.add_argument("--stable-hold", default="60",
                    help="keep soaking this many seconds after the plateau is reached")
    ap.add_argument("--valley-bin", default=DEFAULT_VALLEY)
    ap.add_argument("-o", "--out-dir", default=os.path.join(REPO, "results"))
    return ap.parse_args()


GPU_Q = ("utilization.gpu,utilization.memory,temperature.gpu,temperature.memory,fan.speed,"
         "power.draw,power.limit,clocks.sm,clocks.mem,clocks.gr,memory.used,pstate,"
         "utilization.encoder,utilization.decoder,"
         # headroom / health (added): thermal limit, power headroom, video clock,
         # PCIe link state, retired pages, active throttle/slowdown reasons
         "temperature.gpu.tlimit,power.default_limit,power.max_limit,clocks.video,"
         "pcie.link.gen.current,pcie.link.width.current,retired_pages.pending,"
         "clocks_throttle_reasons.active")

COLUMNS = ["elapsed_s", "gpu_util_pct", "mem_util_pct", "temp_c", "temp_mem_c", "fan_pct",
           "power_w", "power_limit_w", "sm_mhz", "mem_mhz", "gr_mhz", "vram_used_mib",
           "pstate", "gpu_enc_pct", "gpu_dec_pct", "cpu_util_pct", "ram_used_gib",
           "ram_total_gib", "swap_used_gib", "cpu_temp_c", "disk_read_mbs", "disk_write_mbs",
           "net_rx_mbs", "net_tx_mbs",
           "temp_limit_c", "power_default_w", "power_max_w", "video_mhz", "pcie_gen",
           "pcie_width", "retired", "throttle", "throttle_bits",
           "gpu_3d_pct", "gpu_copy_pct"]

_CPU_TEMP = {"v": None}
_GPU_ENGINE = {"3d": None, "copy": None}


def _bits(s):
    """'0x0000000000000001' -> 1 (throttle/slowdown reason bitmask)."""
    try:
        return int(str(s).strip().replace("0x", ""), 16)
    except Exception:
        return None


def _flag(s):
    """nvidia-smi yes/no -> 1/0."""
    v = str(s).strip().lower()
    return 1 if v in ("yes", "true", "1") else (0 if v in ("no", "false", "0") else None)


def cpu_temp_thread(stop_evt):
    """Best-effort Windows CPU temp via ACPI (root/wmi), off the 1 Hz path."""
    if os.name != "nt":
        return
    ps = ("(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
          "-ErrorAction SilentlyContinue | Measure-Object -Property CurrentTemperature -Maximum).Maximum")
    while not stop_evt.is_set():
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=8,
                                 creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
            v = float(out)
            _CPU_TEMP["v"] = round(v / 10.0 - 273.15, 1) if 2732 < v < 3832 else None
        except Exception:
            _CPU_TEMP["v"] = None
        stop_evt.wait(3.0)


def _num(s):
    s = str(s).strip()
    if not s or s.startswith("[") or s.lower() in ("n/a", "not supported"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def gpu_engine_thread(stop_evt):
    """Per-engine GPU utilisation (PDH counters), Windows only.

    nvidia-smi's `utilization.gpu` is coarse; the engine counters expose Copy /
    VideoEncode load (e.g. saturation during PCIe H2D/D2H or NVENC).
    """
    if os.name != "nt":
        return
    ps = ("$c = Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -ErrorAction SilentlyContinue; "
          "$d = 0.0; $p = 0.0; "
          "foreach ($s in $c.CounterSamples) { if ($s.InstanceName -match 'engtype_3D') { $d += $s.CookedValue } "
          "elseif ($s.InstanceName -match 'engtype_Copy') { $p += $s.CookedValue } }; "
          "'{0} {1}' -f [math]::Min(100, [math]::Round($d)), [math]::Min(100, [math]::Round($p))")
    while not stop_evt.is_set():
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=12,
                                 creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
            d, p = out.split()
            _GPU_ENGINE["3d"] = float(d)
            _GPU_ENGINE["copy"] = float(p)
        except Exception:
            pass
        stop_evt.wait(5.0)


def _gpu_row():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=" + GPU_Q, "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5,
                             creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)).stdout.strip()
        v = [x.strip() for x in out.splitlines()[0].split(",")]
        if len(v) >= 14:
            return v
    except Exception:
        pass
    return None


def _win_cpu_pct(prev):
    """CPU % from GetSystemTimes deltas, so psutil isn't required."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        idle = ctypes.c_ulonglong()
        kern = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                                     ctypes.byref(user)):
            return None
        cur = (idle.value, kern.value, user.value)
        old = prev.get("cpu_t")
        prev["cpu_t"] = cur
        if not old:
            return None
        di, dk, du = cur[0] - old[0], cur[1] - old[1], cur[2] - old[2]
        total = dk + du                       # kernel time already includes idle
        if total <= 0:
            return None
        return round(100.0 * (total - di) / total, 1)
    except Exception:
        return None


def _win_mem():
    """(used_gib, total_gib, swap_used_gib) via GlobalMemoryStatusEx."""
    if os.name != "nt":
        return None, None, None
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
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None, None, None
        return (round((st.ullTotalPhys - st.ullAvailPhys) / 1024 ** 3, 2),
                round(st.ullTotalPhys / 1024 ** 3, 2),
                round((st.ullTotalPageFile - st.ullAvailPageFile) / 1024 ** 3, 2))
    except Exception:
        return None, None, None


def sampler(csv_path, stop_evt):
    try:
        import psutil
    except Exception:
        psutil = None
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        fh.flush()
        if psutil is None:
            print("[win] WARNING: python psutil is not installed - disk/net telemetry will be "
                  "blank and CPU/RAM use the stdlib fallback (pip install psutil)", flush=True)
        t0 = time.time()
        prev = {}
        while not stop_evt.is_set():
            row = [round(time.time() - t0, 1)]
            g = _gpu_row()
            extra = [None] * 11
            extra[9] = _GPU_ENGINE["3d"]
            extra[10] = _GPU_ENGINE["copy"]
            if g:
                # util, mem_util, temp, temp_mem, fan, power, power_limit, sm, mem, gr,
                # vram_mib, pstate, enc, dec
                row += [_num(g[0]), _num(g[1]), _num(g[2]), _num(g[3]), _num(g[4]),
                        _num(g[5]), _num(g[6]), _num(g[7]), _num(g[8]), _num(g[9]),
                        _num(g[10]), (str(g[11]).strip().upper().lstrip("P") or None),
                        _num(g[12]), _num(g[13])]
                if len(g) >= 22:
                    bits = _bits(g[21])
                    # headroom / health (appended after the psutil block; see COLUMNS)
                    extra[:9] = [_num(g[14]), _num(g[15]), _num(g[16]), _num(g[17]),
                                 _num(g[18]), _num(g[19]), _flag(g[20]),
                                 (1 if (bits & ~1) else 0) if bits is not None else None, bits]
            else:
                row += [None] * 14
            if psutil:
                try:
                    vm = psutil.virtual_memory()
                    sm = psutil.swap_memory()
                    cpu = psutil.cpu_percent()
                except Exception:
                    vm = sm = None; cpu = None
                row += [cpu,
                        round(vm.used / 1024 ** 3, 2) if vm else None,
                        round(vm.total / 1024 ** 3, 2) if vm else None,
                        round(sm.used / 1024 ** 3, 2) if sm else None,
                        _CPU_TEMP["v"]]
                try:
                    dio = psutil.disk_io_counters()
                    nio = psutil.net_io_counters()
                    now = time.time()
                    if dio and prev.get("dio"):
                        dt = max(0.001, now - prev["t"])
                        row += [round((dio.read_bytes - prev["dio"].read_bytes) / 1e6 / dt, 1),
                                round((dio.write_bytes - prev["dio"].write_bytes) / 1e6 / dt, 1)]
                    else:
                        row += [None, None]
                    if nio and prev.get("nio"):
                        dt = max(0.001, now - prev["t"])
                        row += [round((nio.bytes_recv - prev["nio"].bytes_recv) * 8 / 1e6 / dt, 1),
                                round((nio.bytes_sent - prev["nio"].bytes_sent) * 8 / 1e6 / dt, 1)]
                    else:
                        row += [None, None]
                    prev = {"dio": dio, "nio": nio, "t": now}
                except Exception:
                    row += [None, None, None, None]
            else:
                used, total, swap = _win_mem()
                row += [_win_cpu_pct(prev), used, total, swap, _CPU_TEMP["v"],
                        None, None, None, None]
            row += extra
            w.writerow(row)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
            stop_evt.wait(1.0)


def main():
    a = parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    host = os.environ.get("COMPUTERNAME", "win")
    csv_path = os.path.join(a.out_dir, "%s-win-%s.telemetry.csv" % (host, stamp))

    stop_evt = threading.Event()
    threading.Thread(target=sampler, args=(csv_path, stop_evt), daemon=True).start()
    threading.Thread(target=cpu_temp_thread, args=(stop_evt,), daemon=True).start()
    threading.Thread(target=gpu_engine_thread, args=(stop_evt,), daemon=True).start()

    ps = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", BATTERY,
          "-OutDir", a.out_dir,
          "-SoakSeconds", str(a.gpu_soak),
          "-CombinedSeconds", str(a.combined_seconds),
          "-CpuSoakSeconds", str(a.cpu_soak),
          "-RamSoakSeconds", str(a.ram_soak),
          "-Size", str(a.gpu_size),
          "-CpuSize", str(a.cpu_size),
          "-RamSizeMB", str(a.ram_size_mb),
          "-Passes", str(a.passes),
          "-DiskSeconds", str(a.disk_seconds),
          "-SteadyGB", str(a.steady_gb),
          "-FullSystemSeconds", ("0" if a.skip_fullsoak else str(a.fullsoak_seconds))]
    if a.soak_until_stable:
        ps.append("-SoakUntilStable")
        ps += ["-StableHold", str(a.stable_hold)]
    if a.skip_cpu:
        ps.append("-SkipCpu")
    if a.skip_gpu:
        ps.append("-SkipGpu")
    if a.skip_ram:
        ps.append("-SkipRam")
    if a.skip_disk:
        ps.append("-SkipDisk")
    if a.skip_net:
        ps.append("-SkipNet")
    if a.skip_nvenc:
        ps.append("-SkipNvenc")
    if a.skip_llm:
        ps.append("-SkipLlm")
    if a.skip_combined:
        ps.append("-SkipCombined")
    if a.skip_sys:
        ps.append("-SkipSys")
    if a.skip_gfx:
        ps.append("-SkipValley")
    elif os.path.isdir(a.valley_bin):
        ps += ["-ValleyBin", a.valley_bin]
    if a.gfx_seconds != 60:
        # Unigine Valley's run length is fixed by its preset, so the Windows gfx
        # duration is not configurable; say so rather than silently ignoring it.
        print("[win] note: --gfx-seconds is not adjustable on Windows (Valley preset)", flush=True)

    cmd = ["powershell"] + ps
    print("[win] runner: %s" % " ".join(cmd), flush=True)

    env = os.environ.copy()
    pydir = os.path.dirname(sys.executable)
    env["PATH"] = pydir + os.pathsep + env.get("PATH", "")

    rc = 1
    try:
        p = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1,
                             creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        cur_group = None
        for line in p.stdout:
            line = line.rstrip("\n")
            m = re.match(r"^=== \[(\d+)/\d+\]\s*(.+?)\s*===\s*$", line)
            if m:
                idx, title = int(m.group(1)), m.group(2)
                grp = PHASE_GROUP.get(idx)
                cur_group = grp
                if grp:
                    print("[%s] %s" % (grp, title), flush=True)
                else:
                    print("[%s]" % title, flush=True)
            else:
                # break out the sub-phases the bench scripts emit ("[phase] matmul")
                pm = re.match(r"^\[phase\]\s*(.+)$", line)
                if pm and cur_group:
                    print("[%s] %s" % (cur_group, pm.group(1)), flush=True)
                else:
                    print(line, flush=True)
        rc = p.wait()
    finally:
        stop_evt.set()
    return rc


if __name__ == "__main__":
    sys.exit(main())
