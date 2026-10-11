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


def sampler(csv_path, stop_evt):
    try:
        import psutil
    except Exception:
        psutil = None
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["elapsed_s", "temp_c", "fan_pct", "power_w", "gpu_util_pct",
                    "gpu_fan_rpm", "cpu_util_pct", "ram_used_gib"])
        fh.flush()
        t0 = time.time()
        while not stop_evt.is_set():
            row = [round(time.time() - t0, 1)]
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=temperature.gpu,fan.speed,power.draw,utilization.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)).stdout.strip()
                v = [x.strip() for x in out.split(",")]
                row += [float(v[0]), float(v[1]), float(v[2]), float(v[3]), float(v[1])]
            except Exception:
                row += ["", "", "", "", ""]
            if psutil:
                try:
                    row += [round(psutil.cpu_percent(), 0),
                            round(psutil.virtual_memory().used / 1024 ** 3, 1)]
                except Exception:
                    row += ["", ""]
            else:
                row += ["", ""]
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
