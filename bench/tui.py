#!/usr/bin/env python3
"""bench-tui - an htop/nvtop-style terminal dashboard for the bench-test toolkit.

Full-screen curses UI: run presets and group toggles, live phase status with
elapsed timers, htop-style CPU/RAM and nvtop-style GPU telemetry (temps, fan % +
RPM, power, clocks, VRAM) fed from the 1 Hz telemetry CSV, a log tail, a report
viewer, and one-key collect to the live USB.

    python bench/tui.py [--results-dir DIR] [--demo] [--exit-after SECS]

Keys:  s start · x stop · c collect · f fan-curve · p cycle preset · 1-0 groups
       r report · G graphical dashboard · t shell · PgUp/PgDn log · q quit
"""
import argparse
import curses
import glob
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections import deque

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "bench")
RUNNER = os.path.join(BENCH, "Run-LinuxBench.sh")
FAN_CURVE = os.path.join(BENCH, "fan_curve.py")
FAN_CONF = "/etc/bench-fan-curve.conf"
RESULT_COLLECTOR = "/usr/local/bin/bench-collect"
GUI_LAUNCHER = "/usr/local/bin/bench-gui"
sys.path.insert(0, BENCH)

GROUPS = ["gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "sys", "gfx"]

PRESETS = [
    ("Quick check", {"groups": GROUPS,
                     "dur": {"gpu": "60", "combined": "60", "fullsoak": "60", "gfx": "30"}}),
    ("Full run", {"groups": GROUPS,
                  "dur": {"gpu": "600", "combined": "300", "fullsoak": "300", "gfx": "60"}}),
    ("Graphics only", {"groups": ["sys", "gfx"],
                       "dur": {"gpu": "600", "combined": "300", "fullsoak": "300", "gfx": "60"}}),
    ("CPU + RAM + disk", {"groups": ["cpu", "ram", "disk", "net", "sys"],
                          "dur": {"gpu": "600", "combined": "300", "fullsoak": "300", "gfx": "60"}}),
]

SPARK = "▁▂▃▄▅▆▇█"


class BenchTUI:
    def __init__(self, scr, opts):
        self.scr = scr
        self.results_dir = opts["results_dir"]
        self.demo = opts["demo"]
        self.exit_after = opts["exit_after"]
        self.preset = 1
        self.groups = dict.fromkeys(GROUPS, True)
        self.dur = {"gpu": "600", "combined": "300", "fullsoak": "300", "gfx": "60"}

        self.q = queue.Queue()
        self.proc = None
        self.running = False
        self.exited = False
        self.phases = []
        self.last_phase = {}
        self.log = deque(maxlen=2000)
        self.scroll = 0
        self.follow = True
        self.hw = "detecting..."
        self.tel = {k: deque(maxlen=1000) for k in ("t", "temp", "fan", "power", "util")}
        self.tel_path = None
        self.tel_cols = {}
        self.csv_last = None
        self.csv_now = {}
        self.gpu = {}
        self.status = "idle - press s to start"
        self.status_attr = 0

        # live system sampling (htop-style)
        self.sys_cpu = 0.0
        self.sys_cores = []
        self.sys_ram = (0.0, 0.0)
        self.sys_swap = (0.0, 0.0)
        self.sys_load = 0.0
        self._prev_stat = None
        self.curve = self._curve_state()

        self._apply_preset(1)
        self._demo_seed()

    # ------------------------------------------------------------- presets --
    def _apply_preset(self, idx):
        self.preset = idx
        spec = PRESETS[idx][1]
        self.groups = dict.fromkeys(GROUPS, False)
        for g in spec["groups"]:
            self.groups[g] = True
        self.dur.update(spec["dur"])

    def _args(self):
        args = []
        for g in GROUPS:
            if not self.groups[g]:
                args.append("--skip-" + g)
        args += ["--gpu-soak", self.dur["gpu"], "--combined-seconds", self.dur["combined"],
                 "--fullsoak-seconds", self.dur["fullsoak"], "--gfx-seconds", self.dur["gfx"],
                 "-o", self.results_dir]
        return args

    # ---------------------------------------------------------------- run ---
    def start(self):
        if self.running:
            return
        self.phases, self.last_phase, self.scroll, self.follow = [], {}, 0, True
        self.tel = {k: deque(maxlen=1000) for k in ("t", "temp", "fan", "power", "util")}
        self.tel_path, self.tel_cols, self.csv_last = None, {}, None
        self.exited = False
        os.makedirs(self.results_dir, exist_ok=True)
        env = os.environ.copy()
        env.setdefault("BENCH_PY", sys.executable)
        try:
            self.proc = subprocess.Popen([RUNNER] + self._args(), cwd=REPO, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1, start_new_session=True)
        except Exception as exc:
            self._set("failed to start: %s" % exc, 4)
            return
        self.running = True
        self._set("running: %s" % PRESETS[self.preset][0], 2)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                self.q.put(line)
        except Exception:
            pass
        self.q.put(None)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self._set("stopping...", 3)
            except Exception:
                pass

    def collect(self):
        if os.path.exists(RESULT_COLLECTOR):
            self._set("collecting to USB...", 2)

            def work():
                try:
                    subprocess.run([RESULT_COLLECTOR, self.results_dir], timeout=900)
                    self._set("collected to BENCHDATA", 1)
                except Exception as exc:
                    self._set("collect failed: %s" % exc, 3)
            threading.Thread(target=work, daemon=True).start()
        else:
            self._set("results kept in %s" % self.results_dir, 5)

    def _set(self, text, attr=0):
        self.status = text
        self.status_attr = attr

    # --------------------------------------------------------- fan curve ----
    def _curve_state(self):
        try:
            with open(FAN_CONF) as fh:
                for line in fh:
                    if line.startswith("CURVE="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return None

    def toggle_curve(self):
        if self.running:
            self._set("stop the run first (x)", 3)
            return
        script = FAN_CURVE if os.path.exists(FAN_CURVE) else None
        if not script:
            self._set("fan_curve.py not found", 4)
            return
        cmd = "off" if self.curve else "set"

        def work():
            try:
                r = subprocess.run([sys.executable, script, cmd], capture_output=True,
                                   text=True, timeout=90)
                out = (r.stdout or r.stderr).strip().splitlines()
                self._set(out[-1] if out else "fan curve %s: rc=%s" % (cmd, r.returncode),
                          (1 if r.returncode == 0 else 3))
            except Exception as exc:
                self._set("fan curve failed: %s" % exc, 3)
            self.curve = self._curve_state()
        threading.Thread(target=work, daemon=True).start()

    # ----------------------------------------------------------- plumbing ---
    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    self._on_exit()
                else:
                    self._handle(str(item).rstrip("\n"))
        except queue.Empty:
            pass

    def _handle(self, line):
        self.log.append(line)
        if line.startswith("[") and "]" in line:
            head = line[1:line.index("]")]
            rest = line[line.index("]") + 1:].strip()
            if head == "warn" and "/" in rest:
                grp = rest.split("/")[0]
                ph = self.last_phase.get(grp)
                if ph:
                    ph["status"] = "warn"
                    ph["detail"] = rest
            elif head in GROUPS:
                prev = self.last_phase.get(head)
                if prev:
                    prev["status"] = "ok"
                    prev["t1"] = time.time()
                ph = {"group": head, "label": rest, "status": "run", "t0": time.time(),
                      "t1": None, "detail": ""}
                self.phases.append(ph)
                self.last_phase[head] = ph
                self.follow = True
        elif self.phases and line.strip():
            self.phases[-1]["detail"] = line.strip()

    def _on_exit(self):
        if self.exited:
            return
        self.exited = True
        self.running = False
        rc = self.proc.poll() if self.proc else None
        for ph in self.phases:
            if ph["status"] == "run":
                ph["status"] = "ok"
                ph["t1"] = time.time()
        self._set("finished (exit %s) - c collect · r report" % rc, 1 if rc == 0 else 4)
        if os.path.exists(RESULT_COLLECTOR):
            self.collect()

    def _read_telemetry(self):
        files = glob.glob(os.path.join(self.results_dir, "*-linux-*.telemetry.csv"))
        if not files:
            return
        newest = max(files, key=os.path.getmtime)
        if newest != self.tel_path:
            self.tel_path = newest
            self.tel_cols, self.csv_last = {}, None
            try:
                with open(newest) as fh:
                    self.tel_cols = {n: i for i, n in enumerate(fh.readline().strip().split(","))}
            except OSError:
                return
        try:
            with open(self.tel_path) as fh:
                lines = fh.read().strip().splitlines()
            if len(lines) < 2:
                return
            vals = lines[-1].split(",")
        except OSError:
            return

        def get(name):
            i = self.tel_cols.get(name)
            if i is None or i >= len(vals):
                return None
            try:
                return float(vals[i])
            except ValueError:
                return None

        def raw(name):
            i = self.tel_cols.get(name)
            return vals[i].strip() if i is not None and i < len(vals) else None

        self.csv_now = {k: get(k) for k in ("temp_c", "fan_pct", "power_w", "gpu_util_pct",
                                            "mem_util_pct", "vram_used_mib", "sm_mhz",
                                            "cpu_package_c", "cpu_max_core_c", "nvme_c",
                                            "cpu_fan_rpm", "gpu_fan_rpm", "gpu_enc_pct", "gpu_dec_pct")}
        self.csv_now["pstate"] = raw("pstate")
        elapsed = get("elapsed_s")
        if elapsed is not None and elapsed != self.csv_last:
            self.csv_last = elapsed
            self.tel["t"].append(elapsed)
            for key, col in (("temp", "temp_c"), ("fan", "fan_pct"), ("power", "power_w"),
                             ("util", "gpu_util_pct")):
                v = self.csv_now.get(col)
                if v is not None:
                    self.tel[key].append(v)

    def _sample_system(self):
        try:
            with open("/proc/stat") as fh:
                stats = []
                for line in fh:
                    if not line.startswith("cpu"):
                        continue
                    vals = [int(x) for x in line.split()[1:]]
                    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                    stats.append((idle, sum(vals)))
                    if len(stats) > 1 + 16:
                        break
            if self._prev_stat:
                def pct(p, c):
                    di, dt = c[0] - p[0], c[1] - p[1]
                    return 100.0 * (1.0 - di / dt) if dt > 0 else 0.0
                self.sys_cpu = pct(self._prev_stat[0], stats[0])
                self.sys_cores = [pct(self._prev_stat[1][i], stats[i + 1])
                                  for i in range(min(len(stats) - 1, len(self._prev_stat[1])))]
            self._prev_stat = (stats[0], stats[1:])
        except Exception:
            pass
        try:
            mem = {}
            with open("/proc/meminfo") as fh:
                for line in fh:
                    k, v = line.split(":", 1)
                    mem[k] = int(v.split()[0])
            total = mem.get("MemTotal", 0) / 1048576.0
            avail = mem.get("MemAvailable", 0) / 1048576.0
            st = mem.get("SwapTotal", 0) / 1048576.0
            sf = mem.get("SwapFree", 0) / 1048576.0
            self.sys_ram = (total - avail, total)
            self.sys_swap = (st - sf, st)
        except Exception:
            pass
        try:
            self.sys_load = os.getloadavg()[0]
        except Exception:
            pass

    def _detect_hardware(self):
        parts = []
        try:
            out = subprocess.run([sys.executable, os.path.join(BENCH, "linux_info.py"), "info"],
                                 capture_output=True, text=True, timeout=90).stdout
            for line in out.splitlines():
                if line.startswith("RESULT_JSON:"):
                    d = json.loads(line.split(":", 1)[1])
                    cpu = (d.get("cpu") or {}).get("model", "")
                    cpu = cpu.replace("(R)", "").replace("(TM)", "").replace("CPU", "").strip()
                    parts += [cpu, "%s GiB" % d.get("memory_gib")]
        except Exception:
            pass
        try:
            import gpu_util
            gpu_util.probe()
            if gpu_util.NAME:
                parts.append(gpu_util.NAME)
        except Exception:
            pass
        self.hw = " · ".join(p for p in parts if p) or "hardware detection unavailable"

    # -------------------------------------------------------------- demo ----
    def _demo_seed(self):
        if not self.demo:
            threading.Thread(target=self._detect_hardware, daemon=True).start()
            return
        self.hw = "Omarchy · i7-3770 · 15.6 GiB · NVIDIA GeForce GTX 970"
        now = time.time()
        demo = [("gpu", "info", 4), ("gpu", "matmul", 31), ("gpu", "membw", 22), ("gpu", "pcie", 24),
                ("gpu", "conv", 19), ("gpu", "integrity", 5), ("gpu", "soak", 601), ("cpu", "info", 2),
                ("cpu", "matmul", 13)]
        for i, (grp, label, dur) in enumerate(demo):
            status = "run" if i == len(demo) - 1 else "ok"
            t1 = now if status == "run" else now - (len(demo) - i) * 15
            self.phases.append({"group": grp, "label": label, "status": status,
                                "t0": t1 - dur, "t1": None if status == "run" else t1,
                                "detail": ("soak %.1fs matmuls=495 matmul_TFLOPS=1.8" % 601.0)
                                if label == "soak" else ""})
            self.last_phase[grp] = self.phases[-1]
        for i in range(200):
            phase = min(1.0, i / 60)
            self.tel["t"].append(i)
            self.tel["temp"].append(31 + 35 * phase + (i % 5) * 0.3)
            self.tel["power"].append(22 + 128 * phase + (i % 7) * 1.2)
            self.tel["fan"].append(27 + 32 * phase)
            self.tel["util"].append(min(100, 100 * phase))
        self.sys_cpu, self.sys_cores = 87.0, [91, 88, 90, 85, 92, 86, 89, 83]
        self.sys_ram, self.sys_swap, self.sys_load = (9.8, 15.6), (0.0, 0.0), 3.42
        self.csv_now = {"temp_c": 66, "fan_pct": 59, "power_w": 152, "gpu_util_pct": 100,
                        "mem_util_pct": 48, "vram_used_mib": 1337, "sm_mhz": 1189, "pstate": "P2",
                        "cpu_package_c": 54, "cpu_max_core_c": 58, "nvme_c": 41,
                        "cpu_fan_rpm": None, "gpu_fan_rpm": 2601}
        self.log.append("[gpu] soak")
        self.log.append("    soak 601.0s matmuls=495 convs=495 matmul_TFLOPS=1.8")
        self._set("demo mode - press s to run for real", 5)

    # -------------------------------------------------------------- draw ----
    def _put(self, y, x, text, attr=0, w=None):
        H, W = self.scr.getmaxyx()
        if y < 0 or y >= H or x >= W:
            return
        text = str(text)
        if w:
            text = text[:w]
        text = text[:max(0, W - x - 1)]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _box(self, y, x, h, w, title="", attr=0):
        if h < 2 or w < 4:
            return
        self._put(y, x, "┌" + "─" * (w - 2) + "┐", curses.color_pair(8) | curses.A_DIM, w)
        for i in range(1, h - 1):
            self._put(y + i, x, "│", curses.color_pair(8) | curses.A_DIM)
            self._put(y + i, x + w - 1, "│", curses.color_pair(8) | curses.A_DIM)
        self._put(y + h - 1, x, "└" + "─" * (w - 2) + "┘", curses.color_pair(8) | curses.A_DIM, w)
        if title:
            self._put(y, x + 2, " %s " % title, curses.color_pair(2) | curses.A_BOLD)

    def _bar(self, y, x, width, frac, label, value, attr):
        self._put(y, x, "%-6s" % label, curses.color_pair(8))
        frac = max(0.0, min(1.0, frac))
        filled = int(frac * width)
        self._put(y, x + 7, "█" * filled, curses.color_pair(attr))
        self._put(y, x + 7 + filled, "░" * (width - filled), curses.color_pair(8) | curses.A_DIM)
        self._put(y, x + 9 + width, value)

    def _spark(self, y, x, width, series, scale, attr):
        vals = list(series)[-width:]
        if not vals:
            return
        text = "".join(SPARK[min(7, int(v / scale * 7.99))] for v in vals)
        self._put(y, x, text, curses.color_pair(attr))

    @staticmethod
    def _severity(temp):
        if temp is None:
            return 8
        if temp >= 80:
            return 4
        if temp >= 70:
            return 3
        return 1

    def draw(self):
        self.scr.erase()
        H, W = self.scr.getmaxyx()
        if H < 20 or W < 74:
            self._put(0, 0, "terminal too small (need >= 74x20)", curses.color_pair(4))
            self.scr.refresh()
            return

        hdr = " bench-test "
        self._put(0, 0, hdr, curses.color_pair(1) | curses.A_BOLD)
        self._put(0, len(hdr) + 1, self.hw[:W - 32], curses.color_pair(8))
        self._put(0, max(len(hdr) + 2, W - 10), time.strftime("%H:%M:%S"), curses.color_pair(2))
        self._put(1, 0, "─" * (W - 1), curses.color_pair(8) | curses.A_DIM, W - 1)

        LW = 34
        MX = LW + 1
        MW = W - MX - 1
        log_h = 7
        top = 2
        log_y = H - 2 - log_h
        left_h = log_y - top
        tel_h = min(16, max(8, left_h - 5))

        # left panel
        self._box(top, 0, left_h, LW, "run profile")
        for i, name in enumerate([p[0] for p in PRESETS]):
            mark = "▶" if i == self.preset else " "
            attr = curses.color_pair(2) | curses.A_BOLD if i == self.preset else curses.color_pair(8)
            self._put(top + 1 + i, 2, "%s %s" % (mark, name), attr)
        gy = top + 2 + len(PRESETS)
        self._put(gy, 2, "groups (1-0 toggle)", curses.color_pair(8))
        for i, g in enumerate(GROUPS):
            row = gy + 1 + (i % 5)
            col = 2 + (i // 5) * 16
            on = self.groups[g]
            self._put(row, col, "%d%s %s" % ((i + 1) % 10, "◉" if on else "○", g),
                      curses.color_pair(1) if on else curses.color_pair(8) | curses.A_DIM)
        dy = gy + 7
        self._put(dy, 2, "soaks: gpu %ss · comb %ss" % (self.dur["gpu"], self.dur["combined"]),
                  curses.color_pair(8))
        self._put(dy + 1, 2, "       full %ss · gfx %ss" % (self.dur["fullsoak"], self.dur["gfx"]),
                  curses.color_pair(8))
        if dy + 3 < top + left_h:
            state = "on: %s" % self.curve if self.curve else "off (auto)"
            self._put(dy + 3, 2, "fan curve: %s" % state,
                      curses.color_pair(2) if self.curve else curses.color_pair(8) | curses.A_DIM)
            self._put(dy + 4, 2, "[f] toggle curve", curses.color_pair(8) | curses.A_DIM)

        # telemetry panel (nvtop/htop-style)
        self._box(top, MX, tel_h, MW, "gpu / system telemetry")
        ry = top + 1
        barw = max(10, MW - 40)
        t = self.csv_now.get("temp_c")
        fan = self.csv_now.get("fan_pct")
        rpm = self.csv_now.get("gpu_fan_rpm")
        pw = self.csv_now.get("power_w")
        util = self.csv_now.get("gpu_util_pct")
        vram = self.csv_now.get("vram_used_mib")

        def room():
            return ry < top + tel_h - 1

        if room():
            self._bar(ry, MX + 2, barw, (t or 0) / 100, "temp", "%5s" % (
                ("%.0f °C" % t) if t is not None else "--"), self._severity(t)); ry += 1
        if room():
            fan_txt = ("%.0f %%" % fan) if fan is not None else "--"
            if rpm:
                fan_txt += " %.0frpm" % rpm
            self._bar(ry, MX + 2, barw, (fan or 0) / 100, "fan", "%9s" % fan_txt, 1); ry += 1
        if room():
            self._bar(ry, MX + 2, barw, (pw or 0) / 250, "power", "%5s" % (
                ("%.0f W" % pw) if pw is not None else "--"), 3); ry += 1
        if room():
            self._bar(ry, MX + 2, barw, (util or 0) / 100, "gpu", "%5s" % (
                ("%.0f %%" % util) if util is not None else "--"), 2); ry += 1
        if room():
            total = None
            try:
                import gpu_util
                total = gpu_util.VRAM_TOTAL_MIB
            except Exception:
                pass
            frac = (vram / total) if (vram and total) else 0
            vtxt = ("%.0f/%.0f MiB" % (vram, total)) if (vram and total) else "--"
            self._bar(ry, MX + 2, barw, frac, "vram", vtxt, 6); ry += 1
        if room():
            cbarw = max(10, barw - 30)
            self._bar(ry, MX + 2, cbarw, self.sys_cpu / 100, "cpu", "%3.0f %%" % self.sys_cpu, 1)
            ram, ram_t = self.sys_ram
            extra = "  ram %.1f/%.1f GiB · load %.2f" % (ram, ram_t, self.sys_load)
            if self.sys_swap[1] > 0:
                extra += " · swap %.1f GiB" % self.sys_swap[0]
            self._put(ry, MX + 2 + cbarw + 14, extra, curses.color_pair(8) | curses.A_DIM)
            ry += 1
        if room() and self.sys_cores:
            x = MX + 2
            for i, c in enumerate(self.sys_cores[:16]):
                bar = "▁▂▃▄▅▆▇█"[min(7, int(max(0.0, min(100.0, c)) / 14.3))]
                self._put(ry, x, "%2d%s%3.0f" % (i, bar, c), curses.color_pair(8))
                x += 7
            ry += 1
        if room():
            chunks = []
            if t is not None:
                chunks.append("gpu %.0f°" % t)
            if self.csv_now.get("cpu_package_c") is not None:
                chunks.append("cpu %.0f°" % self.csv_now["cpu_package_c"])
            if self.csv_now.get("cpu_max_core_c") is not None:
                chunks.append("core-max %.0f°" % self.csv_now["cpu_max_core_c"])
            if self.csv_now.get("nvme_c") is not None:
                chunks.append("nvme %.0f°" % self.csv_now["nvme_c"])
            if self.csv_now.get("cpu_fan_rpm"):
                chunks.append("cpufan %.0frpm" % self.csv_now["cpu_fan_rpm"])
            if self.csv_now.get("gpu_enc_pct") is not None or self.csv_now.get("gpu_dec_pct") is not None:
                chunks.append("enc/dec %s/%s%%" % (self.csv_now.get("gpu_enc_pct", "--"),
                                                   self.csv_now.get("gpu_dec_pct", "--")))
            self._put(ry, MX + 2, "temps  " + " · ".join(chunks), curses.color_pair(
                self._severity(max([v for v in (t, self.csv_now.get("cpu_package_c"),
                                                self.csv_now.get("nvme_c")) if v is not None] or [0]))))
            ry += 1
        if room():
            clk = self.csv_now.get("sm_mhz")
            self._put(ry, MX + 2, "clock %s MHz · %s" % (
                ("%.0f" % clk) if clk is not None else "--", self.csv_now.get("pstate") or ""),
                curses.color_pair(8)); ry += 1
        if room():
            self._spark(ry, MX + 2, barw, self.tel["temp"], 100.0, 4)
            self._put(ry, MX + barw + 4, "temp hist", curses.color_pair(8) | curses.A_DIM); ry += 1
        if room():
            self._spark(ry, MX + 2, barw, self.tel["power"], 250.0, 3)
            self._put(ry, MX + barw + 4, "pwr hist", curses.color_pair(8) | curses.A_DIM); ry += 1
        if room():
            self._spark(ry, MX + 2, barw, self.tel["util"], 100.0, 2)
            self._put(ry, MX + barw + 4, "gpu hist", curses.color_pair(8) | curses.A_DIM); ry += 1
        if room():
            self._put(ry, MX + 2, self.status[:MW - 4], curses.color_pair(self.status_attr or 5))

        # phases
        ph_h = left_h - tel_h
        if ph_h >= 3:
            self._box(top + tel_h, MX, ph_h, MW, "phases")
            for i, ph in enumerate(self.phases[-(ph_h - 2):]):
                y = top + tel_h + 1 + i
                icon, attr = {"run": ("▶", 2), "ok": ("✓", 1), "warn": ("!", 3)}.get(ph["status"], ("·", 8))
                elapsed = (ph["t1"] or time.time()) - ph["t0"]
                txt = "%-9s %-22s %s" % (ph["group"], ph["label"][:22],
                                         "%d:%02d" % (elapsed // 60, elapsed % 60))
                self._put(y, MX + 2, "%s %s" % (icon, txt[:MW - 4]), curses.color_pair(attr))
                if ph.get("detail") and MW >= 84:
                    self._put(y, MX + MW - 30, ph["detail"][-28:].rjust(28), curses.color_pair(8))

        # log tail
        lines = list(self.log)
        if self.follow:
            view = lines[-(log_h - 2):]
        else:
            start = max(0, len(lines) - (log_h - 2) - self.scroll)
            view = lines[start:start + log_h - 2]
        self._box(log_y, 0, log_h, W, "log")
        for i, line in enumerate(view):
            self._put(log_y + 1 + i, 2, line[:W - 4], curses.color_pair(8))

        keys = (" s start · x stop · c collect · f fan-curve · p profile · 1-0 groups · "
                "r report · G gui · t shell · PgUp/PgDn · q quit ")
        self._put(H - 1, 0, keys.ljust(W - 1)[:W - 1], curses.color_pair(2) | curses.A_REVERSE)
        self.scr.refresh()

    # --------------------------------------------------------- report view --
    def _show_report(self):
        files = sorted(glob.glob(os.path.join(self.results_dir, "*-linux-*.json")),
                       key=os.path.getmtime)
        if not files:
            self._set("no report yet", 3)
            return
        md = files[-1][:-5] + ".md"
        if os.path.exists(md):
            with open(md, errors="replace") as fh:
                lines = fh.read().splitlines()
        else:
            with open(files[-1], errors="replace") as fh:
                lines = json.dumps(json.load(fh), indent=2).splitlines()
        offset = 0
        while True:
            self.scr.erase()
            H, W = self.scr.getmaxyx()
            self._box(0, 0, H - 1, W, os.path.basename(files[-1]))
            for i in range(H - 3):
                if offset + i < len(lines):
                    self._put(1 + i, 2, lines[offset + i][:W - 4], curses.color_pair(8))
            self._put(H - 1, 0, " PgUp/PgDn scroll · any other key close ".ljust(W - 1)[:W - 1],
                      curses.color_pair(2) | curses.A_REVERSE)
            self.scr.refresh()
            k = self.scr.getch()
            if k == curses.KEY_PGUP:
                offset = max(0, offset - 5)
            elif k == curses.KEY_PGDN:
                offset = min(max(0, len(lines) - 1), offset + 5)
            elif k == curses.KEY_UP:
                offset = max(0, offset - 1)
            elif k == curses.KEY_DOWN:
                offset += 1
            else:
                return

    # -------------------------------------------------------------- keys ----
    def key(self, ch):
        if ch in (ord("q"), ord("Q")):
            if self.running:
                self._set("stop the run first (x)", 3)
                return True
            return False
        if ch == ord("s"):
            self.start()
        elif ch == ord("x"):
            self.stop()
        elif ch == ord("c"):
            self.collect()
        elif ch == ord("f"):
            self.toggle_curve()
        elif ch == ord("r"):
            self._show_report()
        elif ch == ord("p") and not self.running:
            self._apply_preset((self.preset + 1) % len(PRESETS))
            self._set("preset: %s" % PRESETS[self.preset][0], 2)
        elif ord("0") <= ch <= ord("9") and not self.running:
            idx = (ch - ord("0") - 1) % 10
            g = GROUPS[idx]
            self.groups[g] = not self.groups[g]
            self._set("%s: %s" % (g, "on" if self.groups[g] else "off"), 2)
        elif ch == curses.KEY_PGUP:
            self.follow = False
            self.scroll = min(max(0, len(self.log) - 1), self.scroll + 5)
        elif ch == curses.KEY_PGDN:
            self.scroll = max(0, self.scroll - 5)
            self.follow = self.scroll == 0
        elif ch == ord("G"):
            self.shell([GUI_LAUNCHER] if os.path.exists(GUI_LAUNCHER)
                       else [sys.executable, os.path.join(BENCH, "gui.py")])
        elif ch == ord("t"):
            self.shell([os.environ.get("SHELL", "/bin/bash")])
        return True

    def shell(self, cmd):
        curses.endwin()
        try:
            subprocess.call(cmd)
        except Exception:
            pass
        self.scr.refresh()
        self.scr.timeout(250)

    def run(self):
        curses.curs_set(0)
        self.scr.timeout(250)
        deadline = time.time() + self.exit_after if self.exit_after else None
        frame = 0
        while True:
            self._drain()
            self._read_telemetry()
            self._sample_system()
            try:
                import gpu_util
                self.gpu = gpu_util.sample() or {}
            except Exception:
                self.gpu = {}
            frame += 1
            if frame % 40 == 0:
                # self-heal: something wrote outside curses (kernel/VT) and the
                # screen model desynced; force a full repaint occasionally
                self.scr.clearok(True)
            self.draw()
            if deadline and time.time() > deadline:
                return
            ch = self.scr.getch()
            if ch == -1:
                continue
            if not self.key(ch):
                return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default=os.path.join(REPO, "results"))
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--exit-after", type=float, default=0)
    a = ap.parse_args()

    def wrapped(scr):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        curses.init_pair(5, curses.COLOR_WHITE, -1)
        curses.init_pair(6, curses.COLOR_MAGENTA, -1)
        curses.init_pair(7, curses.COLOR_BLUE, -1)
        curses.init_pair(8, curses.COLOR_WHITE, -1)
        app = BenchTUI(scr, {"results_dir": a.results_dir, "demo": a.demo, "exit_after": a.exit_after})
        try:
            app.run()
        except KeyboardInterrupt:
            pass

    curses.wrapper(wrapped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
