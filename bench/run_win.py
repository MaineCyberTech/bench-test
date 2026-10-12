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
PHASE_GROUP = {1: "sys", 2: "gpu", 3: "cpu", 4: "combined", 5: None,
               6: "nvenc", 7: "gfx", 8: "ram", 9: "disk", 10: "net", 11: "fullsoak"}


def parse_args():
    ap = argparse.ArgumentParser()
    for g in ("gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "sys", "gfx"):
        ap.add_argument("--skip-" + g, action="store_true")
    ap.add_argument("--gpu-soak", default="600")
    ap.add_argument("--combined-seconds", default="300")
    ap.add_argument("--fullsoak-seconds", default="300")
    ap.add_argument("--gfx-seconds", default="60")
    ap.add_argument("--valley-bin", default=DEFAULT_VALLEY)
    ap.add_argument("-o", "--out-dir", default=os.path.join(REPO, "results"))
    return ap.parse_args()


GPU_Q = ("utilization.gpu,utilization.memory,temperature.gpu,temperature.memory,fan.speed,"
         "power.draw,power.limit,clocks.sm,clocks.mem,clocks.gr,memory.used,pstate,"
         "utilization.encoder,utilization.decoder")

COLUMNS = ["elapsed_s", "gpu_util_pct", "mem_util_pct", "temp_c", "temp_mem_c", "fan_pct",
           "power_w", "power_limit_w", "sm_mhz", "mem_mhz", "gr_mhz", "vram_used_mib",
           "pstate", "gpu_enc_pct", "gpu_dec_pct", "cpu_util_pct", "ram_used_gib",
           "ram_total_gib", "swap_used_gib", "disk_read_mbs", "disk_write_mbs",
           "net_rx_mbs", "net_tx_mbs"]


def _num(s):
    s = str(s).strip()
    if not s or s.startswith("[") or s.lower() in ("n/a", "not supported"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


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


def sampler(csv_path, stop_evt):
    try:
        import psutil
    except Exception:
        psutil = None
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        fh.flush()
        t0 = time.time()
        prev = {}
        while not stop_evt.is_set():
            row = [round(time.time() - t0, 1)]
            g = _gpu_row()
            if g:
                # util, mem_util, temp, temp_mem, fan, power, power_limit, sm, mem, gr,
                # vram_mib, pstate, enc, dec
                row += [_num(g[0]), _num(g[1]), _num(g[2]), _num(g[3]), _num(g[4]),
                        _num(g[5]), _num(g[6]), _num(g[7]), _num(g[8]), _num(g[9]),
                        _num(g[10]), (str(g[11]).strip().upper().lstrip("P") or None),
                        _num(g[12]), _num(g[13])]
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
                        round(sm.used / 1024 ** 3, 2) if sm else None]
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
                row += [None] * 8
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

    ps = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", BATTERY,
          "-OutDir", a.out_dir,
          "-SoakSeconds", str(a.gpu_soak),
          "-CombinedSeconds", str(a.combined_seconds),
          "-FullSystemSeconds", ("0" if a.skip_fullsoak else str(a.fullsoak_seconds))]
    if a.skip_cpu:
        ps.append("-SkipCpu")
    if a.skip_ram:
        ps.append("-SkipRam")
    if a.skip_disk:
        ps.append("-SkipDisk")
    if a.skip_net:
        ps.append("-SkipNet")
    if a.skip_nvenc:
        ps.append("-SkipNvenc")
    if a.skip_combined:
        ps.append("-SkipCombined")
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
        for line in p.stdout:
            line = line.rstrip("\n")
            m = re.match(r"^=== \[(\d+)/\d+\]\s*(.+?)\s*===\s*$", line)
            if m:
                idx, title = int(m.group(1)), m.group(2)
                grp = PHASE_GROUP.get(idx)
                if grp:
                    print("[%s] %s" % (grp, title), flush=True)
                else:
                    print("[%s]" % title, flush=True)
            else:
                print(line, flush=True)
        rc = p.wait()
    finally:
        stop_evt.set()
    return rc


if __name__ == "__main__":
    sys.exit(main())
