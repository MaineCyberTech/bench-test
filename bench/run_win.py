#!/usr/bin/env python3
"""run_win.py - Windows runner for the bench-test dashboards (GUI/TUI).

Accepts the same flags as Run-LinuxBench.sh, runs the Windows battery
(bench/Run-WorkstationBench.ps1), re-emits its phases as "[group] ..." lines the
dashboards parse, and writes a telemetry CSV (<host>-win-<stamp>.telemetry.csv)
at 4 Hz by default (`--interval`; nvidia-smi is cached at 1 Hz, sensors at 1 Hz).

    python bench/run_win.py [-o DIR] [--gpu-soak S] [--combined-seconds S]
        [--fullsoak-seconds S] [--gfx-seconds S] [--skip-gpu] ... [--valley-bin DIR]
"""
import argparse
import csv
import os
import re
import shutil
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
    ap.add_argument("--disk-size-mb", default="2048",
                    help="randread/randwrite/randmix size; the phases scale with it "
                         "(2048 took 226s of the desktop's 1368s run, 512 takes ~57s)")
    ap.add_argument("--steady-gb", default="10")
    ap.add_argument("--gpu-size", default="12288")
    ap.add_argument("--cpu-size", default="4096")
    ap.add_argument("--ram-size-mb", default="1024")
    ap.add_argument("--passes", default="2")
    ap.add_argument("--soak-until-stable", action="store_true",
                    help="soak until the GPU temperature plateaus (the soak ceiling still applies)")
    ap.add_argument("--stable-hold", default="60",
                    help="keep soaking this many seconds after the plateau is reached")
    ap.add_argument("--gfx-engine", default="furmark", choices=["furmark", "valley"],
                    help="gfx engine: FurMark 2 (default) or Unigine Valley")
    ap.add_argument("--furmark-bin", default="",
                    help="path to furmark.exe (else PATH/env FURMARK/standard install)")
    ap.add_argument("--valley-bin", default=DEFAULT_VALLEY)
    ap.add_argument("--interval", default="0.25",
                    help="row rate for the cheap stats (psutil/GetSystemTimes)")
    ap.add_argument("--gpu-interval", default="1.0",
                    help="nvidia-smi cadence: ~69ms/call, and those fields are thermal-mass, "
                         "util/temp/power traces smooth without polling it flat out")
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
           "gpu_3d_pct", "gpu_copy_pct",
           # Appended (never reordered, so older CSVs keep lining up). These are
           # the Linux logger's columns that the Windows sampler lacked, plus
           # LibreHardwareMonitor-only facts; blank when nothing can read them.
           "vram_total_mib", "gpu_fan_rpm", "cpu_mhz", "cpu_pkg_w", "nvme_c", "load1"]
# per-core CPU load columns are appended to the header at sampler start
# (cpu_core1..cpu_coreN) so a 16-thread APU and a 6-core desktop both fit.

_CPU_TEMP = {"v": None}
_GPU_ENGINE = {"3d": None, "copy": None}
# LibreHardwareMonitor values (Sample-Sensors.ps1), used when there is no
# nvidia-smi - i.e. AMD/Intel GPUs on Windows - so the GPU columns are populated.
# Every value update also stamps "ts", so a sampler that stops answering can be
# detected and its last values blanked instead of being reused forever.
_LHM = {}
_LHM_LOCK = threading.Lock()
# identity/bookkeeping keys that do not go stale like a sensor reading
_LHM_STATIC = ("gpu_name", "cpu_name", "ncores", "elevated", "elevation_warned", "error", "ts")


def _lhm_stale(d):
    """True when the library's last sample is older than 5s.

    A dict with no "ts" (only possible when a caller sets values directly) is
    not called stale: there is no age to compare.
    """
    ts = d.get("ts")
    return ts is not None and time.time() - ts > 5.0


def _lhm_view():
    """Snapshot of _LHM with stale sensor values blanked out.

    If the sampler child died or is being restarted, the same reading must not
    keep being written to every row; past ~5s the value is dropped (blank, not a
    number that stopped being true). Static identity strings are kept.
    """
    with _LHM_LOCK:
        d = dict(_LHM)
    if _lhm_stale(d):
        for k in [k for k in d if k not in _LHM_STATIC]:
            d.pop(k, None)
    return d


def cpu_temp_thread(stop_evt):
    """Fallback Windows CPU temp via ACPI (root/wmi), off the row path.

    LibreHardwareMonitor's CPU temp is preferred (a direct sensor read); the ACPI
    thermal zone is only asked when the library has no fresh value, and at a
    >=30s cadence - it returned nothing at all on a box where it still cost
    0.3-1.3s of CPU every 3s.
    """
    if os.name != "nt":
        return
    ps = ("(Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature "
          "-ErrorAction SilentlyContinue | Measure-Object -Property CurrentTemperature -Maximum).Maximum")
    while not stop_evt.is_set():
        with _LHM_LOCK:
            have_lhm = _LHM.get("cpu_temp") is not None and not _lhm_stale(_LHM)
        if not have_lhm:
            try:
                out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                     capture_output=True, text=True, timeout=8,
                                     creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
                v = float(out)
                _CPU_TEMP["v"] = round(v / 10.0 - 273.15, 1) if 2732 < v < 3832 else None
            except Exception:
                _CPU_TEMP["v"] = None
        stop_evt.wait(30.0)


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
    # One child, looping: each sample costs ~2.5s (the PDH window plus the interpreter
    # startup), and spawning one per read burned half a core and starved the sampler -
    # the AMD handheld managed 2.4 rows/s against 3.5-4.0 elsewhere. The cost now sits
    # in the child's own process; we only read lines.
    ps = ("while ($true) { "
          "$c = Get-Counter '\\GPU Engine(*)\\Utilization Percentage' -ErrorAction SilentlyContinue; "
          "$d = 0.0; $p = 0.0; "
          "foreach ($s in $c.CounterSamples) { if ($s.InstanceName -match 'engtype_3D') { $d += $s.CookedValue } "
          "elseif ($s.InstanceName -match 'engtype_Copy') { $p += $s.CookedValue } }; "
          "'{0} {1}' -f [math]::Min(100, [math]::Round($d)), [math]::Min(100, [math]::Round($p)); "
          "Start-Sleep -Seconds 2 }")
    while not stop_evt.is_set():
        proc = None
        try:
            proc = subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, bufsize=1,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            for line in proc.stdout:
                if stop_evt.is_set():
                    break
                try:
                    d, c = line.split()
                    _GPU_ENGINE["3d"] = float(d)
                    _GPU_ENGINE["copy"] = float(c)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        if not stop_evt.is_set():
            stop_evt.wait(2.0)          # the child exited; start it again


def _fnum(s):
    s = str(s).strip()
    if s in ("", "None"):
        return None
    try:
        v = float(s)
        return v          # the sampler blanks an unreadable temp/power/clock
    except ValueError:
        return None


def _parse_lhm_line(line):
    """One `key=value ...` line from Sample-Sensors.ps1 -> the _LHM update dict.

    Split out from the thread so the mapping can be tested without PowerShell.
    Unknown keys are ignored, so extending the script stays safe.
    """
    d = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            d[k] = v
    upd = {"gpu_temp": _fnum(d.get("gpu_temp")), "gpu_power": _fnum(d.get("gpu_power")),
           "gpu_clk": _fnum(d.get("gpu_clk")), "mem_clk": _fnum(d.get("mem_clk")),
           "gpu_util": _fnum(d.get("gpu_util")), "vram_used": _fnum(d.get("vram_used")),
           "gpu_fan_pct": _fnum(d.get("gpu_fan_pct")), "cpu_temp": _fnum(d.get("cpu_temp")),
           "cpu_power": _fnum(d.get("cpu_power")),
           # the rest of what the library exposes: without these the AMD/Intel
           # columns stayed blank even though the sensors were right there
           "gpu_3d": _fnum(d.get("gpu_3d")), "gpu_copy": _fnum(d.get("gpu_copy")),
           "gpu_enc": _fnum(d.get("gpu_enc")), "gpu_dec": _fnum(d.get("gpu_dec")),
           "vram_total": _fnum(d.get("vram_total")),
           "mem_util": _fnum(d.get("mem_util")),
           "gpu_fan_rpm": _fnum(d.get("gpu_fan_rpm")),
           "cpu_util": _fnum(d.get("cpu_util")),
           "cpu_max_core": _fnum(d.get("cpu_max_core")),
           "cpu_mhz": _fnum(d.get("cpu_mhz")),
           "storage_temp": _fnum(d.get("storage_temp")),
           "elevated": d.get("elevated"), "gpu_name": d.get("gpu_name"),
           "cpu_name": d.get("cpu_name"), "ncores": d.get("ncores"),
           "error": d.get("error")}
    # per-core loads (c1=.. c2=..) -> {"cpu_core1": v, ...}
    cores = {}
    for k, v in d.items():
        if len(k) > 1 and k[0] == "c" and k[1:].isdigit():
            fv = _fnum(v)
            if fv is not None:
                cores["cpu_core%s" % k[1:]] = fv
    if cores:
        upd["cores"] = cores
    return upd


def lhm_thread(stop_evt):
    """Stream LibreHardwareMonitor sensors (Sample-Sensors.ps1) into _LHM.

    nvidia-smi carries the GPU telemetry on NVIDIA; on AMD/Intel the library is
    the only local source for GPU temp/power/clocks/VRAM. Best-effort and off the
    row path (a value the library cannot read stays blank). The child is restarted
    like gpu_engine_thread, so a sampler that exits does not leave its last
    values in _LHM forever; each good line stamps "ts" so the sampler can blank
    values that stop updating. An error line is stored once so its message prints
    once instead of on every line."""
    if os.name != "nt":
        return
    script = os.path.join(BENCH, "Sample-Sensors.ps1")
    if not os.path.isfile(script):
        return
    while not stop_evt.is_set():
        p = None
        try:
            p = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                  "-File", script, "-IntervalMs", "1000",
                                  "-ParentPid", str(os.getpid())],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                 bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            p = None
        if p is not None:
            try:
                for line in p.stdout:
                    if stop_evt.is_set():
                        break
                    upd = _parse_lhm_line(line)
                    # A sensor backend that cannot start has to be visible: otherwise the
                    # GPU/CPU columns are simply blank on AMD/Intel and nothing says why.
                    # Storing the error is what makes the guard print it once.
                    if upd.get("error"):
                        if _LHM.get("error") != upd["error"]:
                            print("[win] LibreHardwareMonitor: %s - GPU/CPU sensor columns will be "
                                  "blank" % upd["error"], flush=True)
                        with _LHM_LOCK:
                            _LHM["error"] = upd["error"]
                        continue      # an error line must not wipe the last good values
                    if upd.get("elevated") == "0" and not _LHM.get("elevation_warned"):
                        print("[win] sensor note: not elevated - CPU temp/power/clock sensors "
                              "normally need an administrator shell", flush=True)
                        _LHM["elevation_warned"] = True
                    with _LHM_LOCK:
                        _LHM.update(upd)
                        _LHM["ts"] = time.time()
            except Exception:
                pass
            finally:
                try:
                    p.terminate()
                except Exception:
                    pass
        if not stop_evt.is_set():
            stop_evt.wait(2.0)        # the child exited; start it again


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


def _win_pagefile_used_gib():
    """Real page-file usage (GiB) via NtQuerySystemInformation, or None.

    GlobalMemoryStatusEx's TotalPageFile - AvailPageFile is the system commit
    charge (13.03 GiB measured on a box whose page file actually held 62 MiB), so
    it must not be reported as swap. SystemPageFileInformation (0x12) returns one
    entry per page file and TotalInUse is the real usage - the same call
    psutil.swap_memory() makes on Windows. Blank (None) when it cannot be read.
    """
    try:
        import ctypes

        class UnicodeString(ctypes.Structure):
            _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                        ("Buffer", ctypes.c_wchar_p)]

        class PageFileInfo(ctypes.Structure):
            _fields_ = [("NextEntryOffset", ctypes.c_ulong), ("CurrentSize", ctypes.c_ulong),
                        ("TotalInUse", ctypes.c_ulong), ("PeakUsage", ctypes.c_ulong),
                        ("PageFileName", UnicodeString)]

        class SysInfo(ctypes.Structure):
            _fields_ = [("wProcessorArchitecture", ctypes.c_ushort), ("wReserved", ctypes.c_ushort),
                        ("dwPageSize", ctypes.c_ulong), ("lpMinimumApplicationAddress", ctypes.c_void_p),
                        ("lpMaximumApplicationAddress", ctypes.c_void_p),
                        ("dwActiveProcessorMask", ctypes.c_void_p),
                        ("dwNumberOfProcessors", ctypes.c_ulong), ("dwProcessorType", ctypes.c_ulong),
                        ("dwAllocationGranularity", ctypes.c_ulong), ("wProcessorLevel", ctypes.c_ushort),
                        ("wProcessorRevision", ctypes.c_ushort)]
        si = SysInfo()
        ctypes.windll.kernel32.GetSystemInfo(ctypes.byref(si))
        buf = ctypes.create_string_buffer(1 << 16)
        ret = ctypes.c_ulong(0)
        if ctypes.windll.ntdll.NtQuerySystemInformation(0x12, buf, ctypes.sizeof(buf),
                                                        ctypes.byref(ret)) != 0:
            return None
        pages = 0
        off = 0
        while True:
            p = PageFileInfo.from_buffer(buf, off)
            pages += p.TotalInUse
            if not p.NextEntryOffset:
                break
            off += p.NextEntryOffset
        return round(pages * si.dwPageSize / 1024 ** 3, 2)
    except Exception:
        return None


def _win_mem():
    """(used_gib, total_gib, pagefile_used_gib) via GlobalMemoryStatusEx.

    The third value is the real page-file usage (like the Linux sampler's real
    swap), not the commit charge: the two diverge by orders of magnitude under
    load and the old formula reported 13 GiB of "swap" on a box using 62 MiB.
    """
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
                _win_pagefile_used_gib())
    except Exception:
        return None, None, None


def sampler(csv_path, stop_evt, interval=0.25, gpu_interval=0.5):
    try:
        import psutil
    except Exception:
        psutil = None
    # one column per logical CPU so per-core load can be charted; the library
    # reports per physical core on Intel, so the tail stays blank there
    ncores = os.cpu_count() or 0
    core_cols = ["cpu_core%d" % (i + 1) for i in range(ncores)]
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS + core_cols)
        fh.flush()
        if psutil is None:
            print("[win] WARNING: python psutil is not installed - disk/net telemetry will be "
                  "blank and CPU/RAM use the stdlib fallback (pip install psutil)", flush=True)
        t0 = time.time()
        prev = {}
        # nvidia-smi costs ~66ms a call; the psutil/GetSystemTimes stats are
        # sub-millisecond, so only the GPU query is throttled and reused between
        # rows (it also carries temp + power, which change on a thermal timescale).
        # A failing/hanging call (a TDR is exactly what the suite hunts) would
        # otherwise block every row at its 5s timeout, so after one failure the
        # next try waits 30s and the rows in between carry no nvidia-smi values
        # (blank/LHM, never stale repeats) - that gap marks the outage.
        gpu_v, gpu_t, gpu_fail_t, gpu_warned = None, 0.0, 0.0, False
        while not stop_evt.is_set():
            tick = time.time()
            due = (tick - gpu_fail_t >= 30.0) if gpu_v is None else (tick - gpu_t >= gpu_interval)
            if due:
                gpu_v = _gpu_row()
                gpu_t = tick
                if gpu_v is None:
                    gpu_fail_t = tick
                    if not gpu_warned:
                        print("[win] nvidia-smi failed - backing off 30s; GPU columns stay blank "
                              "until it answers", flush=True)
                        gpu_warned = True
                else:
                    gpu_warned = False
            row = [round(tick - t0, 2)]
            g = gpu_v
            lhm = _lhm_view()
            # LibreHardwareMonitor's CPU temp is the preferred source; the ACPI
            # thermal zone is only the fallback (it answered nothing on the box
            # where it still cost 0.3-1.3s of CPU per poll).
            cpu_temp = lhm.get("cpu_temp")
            if cpu_temp is None:
                cpu_temp = _CPU_TEMP["v"]
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
                # no nvidia-smi (AMD/Intel GPU): LibreHardwareMonitor values.
                # util, mem_util, temp, temp_mem, fan, power, power_limit, sm, mem, gr,
                # vram_mib, pstate, enc, dec
                # the AMD "GPU Core" load sensor is intermittent (blank in ~half the
                # rows on a Ryzen APU), so fall back to the D3D engine counter, which
                # gpu_engine_thread collects anyway
                util = lhm.get("gpu_util")
                if util is None:
                    util = _GPU_ENGINE["3d"]
                row += [util, lhm.get("mem_util"), lhm.get("gpu_temp"), None,
                        lhm.get("gpu_fan_pct"), lhm.get("gpu_power"), None,
                        lhm.get("gpu_clk"), lhm.get("mem_clk"), lhm.get("gpu_clk"),
                        lhm.get("vram_used"), None, lhm.get("gpu_enc"), lhm.get("gpu_dec")]
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
                        cpu_temp]
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
                        row += [round((nio.bytes_recv - prev["nio"].bytes_recv) / 1e6 / dt, 1),
                                round((nio.bytes_sent - prev["nio"].bytes_sent) / 1e6 / dt, 1)]
                    else:
                        row += [None, None]
                    prev = {"dio": dio, "nio": nio, "t": now}
                except Exception:
                    row += [None, None, None, None]
            else:
                used, total, swap = _win_mem()
                row += [_win_cpu_pct(prev), used, total, swap, cpu_temp,
                        None, None, None, None]
            row += extra
            # the appended columns (see COLUMNS) + per-core loads; a value the
            # library cannot read stays blank rather than becoming a fake zero
            load1 = None
            if psutil:
                try:
                    load1 = round(psutil.getloadavg()[0], 2)
                except Exception:
                    load1 = None
            row += [lhm.get("vram_total"), lhm.get("gpu_fan_rpm"), lhm.get("cpu_mhz"),
                    lhm.get("cpu_power"), lhm.get("storage_temp"), load1]
            lhm_cores = lhm.get("cores") or {}
            for i in range(1, ncores + 1):
                row += [lhm_cores.get("cpu_core%d" % i)]
            w.writerow(row)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
            stop_evt.wait(max(0.05, interval - (time.time() - tick)))


def main():
    a = parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    host = os.environ.get("COMPUTERNAME", "win")
    csv_path = os.path.join(a.out_dir, "%s-win-%s.telemetry.csv" % (host, stamp))

    stop_evt = threading.Event()
    threading.Thread(target=sampler,
                     args=(csv_path, stop_evt, float(a.interval), float(a.gpu_interval)),
                     daemon=True).start()
    threading.Thread(target=cpu_temp_thread, args=(stop_evt,), daemon=True).start()
    threading.Thread(target=gpu_engine_thread, args=(stop_evt,), daemon=True).start()
    if os.name == "nt":
        # AMD/Intel: the library supplies the GPU columns nvidia-smi would
        # otherwise carry. NVIDIA: nvidia-smi keeps the GPU columns and the
        # library fills what it has no answer for (VRAM total, CPU package
        # watts/clock, storage temperature, per-core load).
        threading.Thread(target=lhm_thread, args=(stop_evt,), daemon=True).start()

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
          "-DiskSizeMB", str(a.disk_size_mb),
          "-SteadyGB", str(a.steady_gb),
          "-GfxEngine", a.gfx_engine,
          "-GfxSeconds", str(a.gfx_seconds),
          "-FullSystemSeconds", ("0" if a.skip_fullsoak else str(a.fullsoak_seconds))]
    if a.soak_until_stable:
        ps.append("-SoakUntilStable")
        ps += ["-StableHold", str(a.stable_hold)]
    if a.furmark_bin:
        ps += ["-FurMarkBin", a.furmark_bin]
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
