#!/usr/bin/env python3
"""bench-test dashboard (GUI).

A flat, dark Tkinter dashboard for the Linux bench toolkit: hardware header,
run presets, live phase table, telemetry tiles + chart, parsed results summary,
tagged log, fan-curve toggle and result collection (BENCHDATA on the live USB).

    python bench/gui.py [--results-dir DIR] [--demo] [--self-test]
"""
import argparse
import glob
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, ttk

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "bench")
RUNNER = os.path.join(BENCH, "Run-LinuxBench.sh")
FAN_CURVE = os.path.join(BENCH, "fan_curve.py")
sys.path.insert(0, BENCH)

# ---------------------------------------------------------------- palette ---
BG = "#0f1218"
CARD = "#161b24"
CARD2 = "#1d2430"
BORDER = "#2b3442"
TEXT = "#dce3ee"
MUTED = "#8592a6"
ACCENT = "#4c9aff"
GOOD = "#46c98d"
WARN = "#e5b567"
BAD = "#ef6b73"

GROUPS = [
    ("gpu", "GPU compute + soak"), ("cpu", "CPU"), ("ram", "RAM"), ("disk", "Disk"),
    ("combined", "Combined GPU+CPU"), ("fullsoak", "Full-system soak"), ("net", "Network"),
    ("nvenc", "Video encode"), ("sys", "Inventory"), ("gfx", "Graphics (FurMark)"),
]

PRESETS = {
    "Select a preset...": None,
    "Quick check (short soaks)": {"spin": {"gpu": 60, "combined": 60, "fullsoak": 60, "gfx": 30}},
    "Full run (defaults)": {"spin": {"gpu": 600, "combined": 300, "fullsoak": 300, "gfx": 60}},
    "Graphics only": {"groups": {"gpu": 0, "cpu": 0, "ram": 0, "disk": 0, "combined": 0,
                                 "fullsoak": 0, "net": 0, "nvenc": 0, "sys": 0, "gfx": 1}},
    "CPU + RAM + disk": {"groups": {"gpu": 0, "cpu": 1, "ram": 1, "disk": 1, "combined": 0,
                                    "fullsoak": 0, "net": 1, "nvenc": 0, "sys": 1, "gfx": 0}},
}

STATUS_COLOR = {"running": ACCENT, "done": GOOD, "WARN": WARN, "warn": WARN, "error": BAD}


def pick_font(candidates, fallback, size, weight="normal"):
    try:
        have = set(tkfont.families())
    except Exception:
        have = set()
    for name in candidates:
        if name in have:
            return (name, size, weight)
    return (fallback, size, weight)


class Dashboard:
    def __init__(self, root, results_dir, demo=False, self_test=False, tab=0):
        self.root = root
        self.results_dir = results_dir
        self.proc = None
        self.q = queue.Queue()
        self.last_row = {}
        self.starts = {}
        self.tele_path = None
        self._cols = {}
        self._last_elapsed = None
        self.tel = {"t": [], "temp": [], "fan": [], "power": [], "util": []}
        self.running = False
        self.exited = False
        self._curve_on = False
        self.run_t0 = None

        self._fonts()
        self._styles()
        self._build()
        if tab:
            try:
                self.nb.select(tab)
            except Exception:
                pass
        self._detect_hardware()
        if demo:
            self._demo_data()
        if self_test:
            self.root.after(3000, self.root.destroy)
        self.root.after(200, self._poll)
        self.root.after(1000, self._tick)

    # ------------------------------------------------------------- fonts ----
    def _fonts(self):
        self.f_display = pick_font(["Inter", "Cantarell", "Noto Sans", "DejaVu Sans"], "DejaVu Sans", 19, "bold")
        self.f_head = pick_font(["Inter", "Cantarell", "Noto Sans", "DejaVu Sans"], "DejaVu Sans", 12, "bold")
        self.f_body = pick_font(["Inter", "Cantarell", "Noto Sans", "DejaVu Sans"], "DejaVu Sans", 10)
        self.f_small = pick_font(["Inter", "Cantarell", "Noto Sans", "DejaVu Sans"], "DejaVu Sans", 9)
        self.f_value = pick_font(["JetBrains Mono", "Fira Code", "Noto Sans Mono", "DejaVu Sans Mono"],
                                 "DejaVu Sans Mono", 20, "bold")
        self.f_mono = pick_font(["JetBrains Mono", "Fira Code", "Noto Sans Mono", "DejaVu Sans Mono"],
                                "DejaVu Sans Mono", 9)

    # ------------------------------------------------------------ styles ----
    def _styles(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=TEXT, font=self.f_body,
                     bordercolor=BORDER, darkcolor=CARD, lightcolor=CARD,
                     troughcolor=CARD2, focuscolor=ACCENT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, foreground=TEXT, font=self.f_body)
        st.configure("Card.TLabel", background=CARD, foreground=TEXT)
        st.configure("Dim.TLabel", background=BG, foreground=MUTED, font=self.f_small)
        st.configure("DimCard.TLabel", background=CARD, foreground=MUTED, font=self.f_small)
        st.configure("Head.TLabel", background=BG, foreground=TEXT, font=self.f_display)
        st.configure("Sect.TLabel", background=CARD, foreground=MUTED, font=self.f_small)
        st.configure("Value.TLabel", background=CARD, foreground=ACCENT, font=self.f_value)

        st.configure("TButton", background=CARD2, foreground=TEXT, borderwidth=0,
                     focusthickness=0, padding=(10, 7), font=self.f_body)
        st.map("TButton", background=[("active", BORDER), ("disabled", CARD)],
               foreground=[("disabled", MUTED)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#0c1016", font=self.f_head)
        st.map("Accent.TButton", background=[("active", "#6cb0ff"), ("disabled", CARD2)],
               foreground=[("disabled", MUTED)])
        st.configure("Danger.TButton", background="#3a2530", foreground=BAD)
        st.map("Danger.TButton", background=[("active", "#54303f")])
        st.configure("Ghost.TButton", background=CARD, foreground=TEXT)
        st.map("Ghost.TButton", background=[("active", CARD2)])

        st.configure("TCombobox", fieldbackground=CARD2, background=CARD2, foreground=TEXT,
                     arrowcolor=MUTED, borderwidth=0, padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", CARD2)])
        self.root.option_add("*TCombobox*Listbox.background", CARD2)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#0c1016")
        st.configure("TSpinbox", fieldbackground=CARD2, background=CARD2, foreground=TEXT,
                     arrowcolor=MUTED, borderwidth=0, padding=3)
        st.configure("TCheckbutton", background=CARD, foreground=TEXT, focuscolor=CARD,
                     font=self.f_small)
        st.map("TCheckbutton", background=[("active", CARD)], foreground=[("active", TEXT)])

        st.configure("Card.Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                     rowheight=26, borderwidth=0, font=self.f_mono)
        st.configure("Card.Treeview.Heading", background=CARD, foreground=MUTED,
                     font=self.f_small, relief="flat", padding=(8, 6))
        st.map("Card.Treeview.Heading", background=[("active", CARD)])
        st.map("Card.Treeview", background=[("selected", "#233046")], foreground=[("selected", TEXT)])

        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
        st.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(16, 8),
                     font=self.f_head, borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", ACCENT)])

        st.configure("Accent.Horizontal.TProgressbar", troughcolor=CARD2, background=ACCENT,
                     borderwidth=0, thickness=6)

    # -------------------------------------------------------------- build ---
    def _card(self, parent, **kw):
        kw.setdefault("padding", 12)
        return ttk.Frame(parent, style="Card.TFrame", **kw)

    def _build(self):
        r = self.root
        r.title("bench-test dashboard")
        r.geometry("1280x820")
        r.minsize(1040, 660)
        r.configure(bg=BG)
        self._center()

        # header
        head = ttk.Frame(r, padding=(14, 10, 14, 6))
        head.pack(fill="x")
        ttk.Label(head, text="bench-test", style="Head.TLabel").pack(side="left")
        ttk.Label(head, text="  Linux workstation benchmark", style="Dim.TLabel").pack(side="left")
        self.hw = ttk.Label(head, text="detecting hardware...", style="Dim.TLabel")
        self.hw.pack(side="right")

        body = ttk.Frame(r, padding=(12, 4, 12, 8))
        body.pack(fill="both", expand=True)

        # ---------------------------------------------------------- side --
        side = self._card(body, width=260)
        side.pack(side="left", fill="y", padx=(0, 10))
        side.pack_propagate(False)

        ttk.Label(side, text="RUN PROFILE", style="Sect.TLabel").pack(anchor="w")
        self.preset = ttk.Combobox(side, values=list(PRESETS.keys()), state="readonly")
        self.preset.current(0)
        self.preset.pack(fill="x", pady=(4, 12))
        self.preset.bind("<<ComboboxSelected>>", self._apply_preset)

        ttk.Label(side, text="GROUPS", style="Sect.TLabel").pack(anchor="w")
        grid = ttk.Frame(side, style="Card.TFrame")
        grid.pack(fill="x", pady=(4, 12))
        self.gvars = {}
        for i, (key, label) in enumerate(GROUPS):
            var = tk.BooleanVar(value=True)
            self.gvars[key] = var
            ttk.Checkbutton(grid, text=label, variable=var).grid(row=i, column=0, sticky="w", pady=1)

        ttk.Label(side, text="DURATIONS (SECONDS)", style="Sect.TLabel").pack(anchor="w")
        dgrid = ttk.Frame(side, style="Card.TFrame")
        dgrid.pack(fill="x", pady=(4, 12))
        self.svars = {}
        for i, (key, label) in enumerate([("gpu", "GPU soak"), ("combined", "Combined"),
                                          ("fullsoak", "Full soak"), ("gfx", "Graphics")]):
            ttk.Label(dgrid, text=label, style="Card.TLabel", font=self.f_small).grid(
                row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value={"gpu": "600", "combined": "300", "fullsoak": "300", "gfx": "60"}[key])
            self.svars[key] = var
            ttk.Spinbox(dgrid, from_=5, to=7200, textvariable=var, width=6).grid(
                row=i, column=1, sticky="e", pady=2)

        btns = ttk.Frame(side, style="Card.TFrame")
        btns.pack(fill="x", side="bottom")
        self.start_btn = ttk.Button(btns, text="Start run  ▶", style="Accent.TButton", command=self.start)
        self.start_btn.pack(fill="x", pady=2)
        self.stop_btn = ttk.Button(btns, text="Stop  ■", style="Danger.TButton", command=self.stop,
                                   state="disabled")
        self.stop_btn.pack(fill="x", pady=2)
        self.collect_btn = ttk.Button(btns, text="Collect results  ↓", style="Ghost.TButton",
                                      command=self.collect)
        self.collect_btn.pack(fill="x", pady=2)
        self.curve_btn = ttk.Button(btns, text="Fan curve: off", style="Ghost.TButton",
                                    command=self.toggle_curve)
        self.curve_btn.pack(fill="x", pady=2)

        # ---------------------------------------------------------- main --
        main = ttk.Frame(body)
        main.pack(side="left", fill="both", expand=True)
        nb = ttk.Notebook(main)
        nb.pack(fill="both", expand=True)
        self.nb = nb

        # run tab
        run = ttk.Frame(nb, padding=8)
        nb.add(run, text="Run")
        treecard = self._card(run)
        treecard.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(treecard, style="Card.Treeview",
                                 columns=("group", "phase", "status", "elapsed"), show="headings")
        for col, w, anchor in [("group", 130, "w"), ("phase", 430, "w"),
                               ("status", 120, "w"), ("elapsed", 90, "e")]:
            self.tree.heading(col, text=col.capitalize())
            self.tree.column(col, width=w, anchor=anchor)
        self.tree.tag_configure("odd", background="#1a2029")
        self.tree.tag_configure("ok", foreground=GOOD)
        self.tree.tag_configure("run", foreground=ACCENT)
        self.tree.tag_configure("warn", foreground=WARN)
        sb = ttk.Scrollbar(treecard, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        strip = ttk.Frame(run, style="Card.TFrame", padding=(12, 8))
        strip.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(strip, style="Accent.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100)
        self.progress.pack(fill="x")

        # telemetry tab
        tele = ttk.Frame(nb, padding=8)
        nb.add(tele, text="Telemetry")
        tiles = ttk.Frame(tele, style="Card.TFrame")
        tiles.pack(fill="x")
        self.tiles = {}
        for key, label in [("temp", "GPU TEMP"), ("fan", "FAN %/RPM"), ("power", "POWER W"),
                           ("util", "GPU %"), ("cpu", "CPU %"), ("ram", "RAM GIB")]:
            card = self._card(tiles, padding=10)
            card.pack(side="left", expand=True, fill="x", padx=(0, 8))
            ttk.Label(card, text=label, style="Sect.TLabel").pack(anchor="w")
            val = ttk.Label(card, text="--", style="Value.TLabel")
            val.pack(anchor="w")
            self.tiles[key] = val
        chartcard = self._card(tele, padding=10)
        chartcard.pack(fill="both", expand=True, pady=(8, 0))
        self.canvas = tk.Canvas(chartcard, bg=CARD, highlightthickness=0, height=280)
        self.canvas.pack(fill="both", expand=True)

        # results tab
        res = ttk.Frame(nb, padding=8)
        nb.add(res, text="Results")
        self.summary = ttk.Frame(res, style="Card.TFrame")
        self.summary.pack(fill="x")
        self.summary_tiles = {}
        row1 = ttk.Frame(self.summary, style="Card.TFrame")
        row1.pack(fill="x")
        row2 = ttk.Frame(self.summary, style="Card.TFrame")
        row2.pack(fill="x", pady=(8, 0))
        for i, (key, label) in enumerate([("soak", "GPU SOAK"), ("cpu", "CPU SOAK"), ("ram", "RAM SOAK"),
                                          ("disk", "DISK"), ("net", "NET"), ("gfx", "FURMARK")]):
            card = self._card(row1 if i < 3 else row2, padding=10)
            card.pack(side="left", expand=True, fill="x", padx=(0, 8))
            ttk.Label(card, text=label, style="Sect.TLabel").pack(anchor="w")
            val = ttk.Label(card, text="--", style="Value.TLabel")
            val.pack(anchor="w")
            self.summary_tiles[key] = val
        md = self._card(res)
        md.pack(fill="both", expand=True, pady=(8, 0))
        self.results = tk.Text(md, bg=CARD, fg=TEXT, insertbackground=TEXT, relief="flat",
                               wrap="none", font=self.f_mono, padx=6, pady=6)
        self.results.tag_configure("h1", font=pick_font(["Inter", "Noto Sans", "DejaVu Sans"],
                                                        "DejaVu Sans", 15, "bold"), foreground=TEXT)
        self.results.tag_configure("h2", font=pick_font(["Inter", "Noto Sans", "DejaVu Sans"],
                                                        "DejaVu Sans", 11, "bold"), foreground=ACCENT)
        self.results.tag_configure("dim", foreground=MUTED)
        self.results.pack(fill="both", expand=True)

        # log tab
        logf = ttk.Frame(nb, padding=8)
        nb.add(logf, text="Log")
        logcard = self._card(logf)
        logcard.pack(fill="both", expand=True)
        self.log = tk.Text(logcard, bg="#0c0f14", fg="#aab4c4", insertbackground=TEXT,
                           relief="flat", wrap="none", font=self.f_mono, padx=8, pady=6)
        self.log.tag_configure("warn", foreground=WARN)
        self.log.tag_configure("err", foreground=BAD)
        self.log.tag_configure("head", foreground=ACCENT)
        self.log.pack(fill="both", expand=True)

        # status bar
        bar = ttk.Frame(r, style="Card.TFrame", padding=(14, 8))
        bar.pack(fill="x")
        self.status_dot = tk.Label(bar, text="●", bg=CARD, fg=MUTED, font=self.f_body)
        self.status_dot.pack(side="left")
        self.status = ttk.Label(bar, text="idle", style="Card.TLabel", font=self.f_small)
        self.status.pack(side="left", padx=(6, 12))
        self.status_right = ttk.Label(bar, text="", style="DimCard.TLabel")
        self.status_right.pack(side="right")

    def _center(self):
        self.root.update_idletasks()
        w, h = 1280, 820
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    # ----------------------------------------------------------- helpers ----
    def _log(self, line):
        tag = ()
        if "[warn]" in line or "WARN" in line:
            tag = ("warn",)
        elif "error" in line.lower() or "traceback" in line.lower():
            tag = ("err",)
        elif line.startswith("["):
            tag = ("head",)
        self.log.insert("end", line + "\n", tag)
        if int(self.log.index("end-1c").split(".")[0]) > 800:
            self.log.delete("1.0", "100.0")
        self.log.see("end")

    def _set_status(self, text, color=MUTED):
        self.status.configure(text=text)
        self.status_dot.configure(fg=color)

    def _detect_hardware(self):
        def work():
            parts = []
            try:
                info = subprocess.run([sys.executable, os.path.join(BENCH, "linux_info.py"), "info"],
                                      capture_output=True, text=True, timeout=90).stdout
                for line in info.splitlines():
                    if line.startswith("RESULT_JSON:"):
                        d = json.loads(line.split(":", 1)[1])
                        cpu = (d.get("cpu") or {}).get("model", "")
                        parts.append("%s · %s · %s GiB" % (d.get("distro"), cpu, d.get("memory_gib")))
            except Exception:
                pass
            try:
                import gpu_util
                gpu_util.probe()
                if gpu_util.NAME:
                    parts.append(gpu_util.NAME)
            except Exception:
                pass
            self.q.put(("hw", "  |  ".join(parts) if parts else "hardware detection unavailable"))
        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------- run control ---
    def _apply_preset(self, _evt=None):
        preset = PRESETS.get(self.preset.get())
        if not preset:
            return
        for key, val in (preset.get("groups") or {}).items():
            self.gvars[key].set(bool(val))
        for key, val in (preset.get("spin") or {}).items():
            self.svars[key].set(str(val))

    def start(self):
        if self.running:
            return
        args = []
        for key, _ in GROUPS:
            if not self.gvars[key].get():
                args.append("--skip-" + key)
        args += ["--gpu-soak", self.svars["gpu"].get(),
                 "--combined-seconds", self.svars["combined"].get(),
                 "--fullsoak-seconds", self.svars["fullsoak"].get(),
                 "--gfx-seconds", self.svars["gfx"].get(),
                 "-o", self.results_dir]
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.last_row, self.starts = {}, {}
        self.tele_path, self._cols, self._last_elapsed = None, {}, None
        self.tel = {"t": [], "temp": [], "fan": [], "power": [], "util": []}
        self.exited = False
        self.run_t0 = time.time()
        os.makedirs(self.results_dir, exist_ok=True)
        env = os.environ.copy()
        env.setdefault("BENCH_PY", sys.executable)
        try:
            self.proc = subprocess.Popen([RUNNER] + args, cwd=REPO, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1, start_new_session=True)
        except Exception as exc:
            self._set_status("failed to start: %s" % exc, BAD)
            return
        self.running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status("running · %s" % PRESETS[self.preset][0].split(" (")[0], ACCENT)
        self._log("[gui] %s %s" % (RUNNER, " ".join(args)))
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
            except Exception:
                pass
            self._set_status("stopping...", WARN)

    def _handle_line(self, line):
        self._log(line)
        if line.startswith("[") and "]" in line:
            head = line[1:line.index("]")]
            rest = line[line.index("]") + 1:].strip()
            if head == "warn" and "/" in rest:
                grp = rest.split("/")[0]
                item = self.last_row.get(grp)
                if item:
                    self.tree.set(item, "status", "WARN")
                    self.tree.item(item, tags=("warn",))
            elif head in dict(GROUPS):
                label = rest if len(rest) <= 70 else rest[:67] + "..."
                prev = self.last_row.get(head)
                zebra = "odd" if len(self.tree.get_children()) % 2 else ""
                if prev:
                    self.tree.set(prev, "status", "done")
                    self.tree.item(prev, tags=("ok", zebra))
                item = self.tree.insert("", "end", values=(head, label, "running", "0:00"),
                                        tags=("run", zebra))
                self.tree.see(item)
                self.last_row[head] = item
                self.starts[item] = time.time()

    def _on_exit(self):
        if self.exited:
            return
        self.exited = True
        self.running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        done = 0
        total = len(self.last_row.values()) or 1
        for item in self.last_row.values():
            if self.tree.set(item, "status") == "running":
                self.tree.set(item, "status", "done")
                self.tree.item(item, tags=("ok",))
            done += 1
        self.progress.configure(value=100.0 * done / total)
        rc = self.proc.poll() if self.proc else None
        self._set_status("finished · exit %s" % rc, GOOD if rc == 0 else BAD)
        self._load_results()
        self._auto_collect()

    # ----------------------------------------------------------- polling ----
    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    self._on_exit()
                elif isinstance(item, tuple) and item[0] == "hw":
                    self.hw.configure(text=item[1])
                elif isinstance(item, tuple) and item[0] == "curve":
                    self.curve_btn.configure(text=item[1])
                else:
                    self._handle_line(str(item).rstrip("\n"))
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _tick(self):
        now = time.time()
        running = 0
        total = len(self.starts)
        for item, t0 in list(self.starts.items()):
            if self.tree.exists(item) and self.tree.set(item, "status") == "running":
                secs = int(now - t0)
                self.tree.set(item, "elapsed", "%d:%02d" % (secs // 60, secs % 60))
                running += 1
        if self.running and total:
            self.progress.configure(value=100.0 * (total - running) / max(1, total))
        if self.running:
            self._read_telemetry()
            elapsed = int(now - self.run_t0) if self.run_t0 else 0
            self.status_right.configure(text="run %d:%02d · results: %s" % (
                elapsed // 60, elapsed % 60, os.path.basename(self.results_dir)))
        self._draw_chart()
        self.root.after(1000, self._tick)

    def _read_telemetry(self):
        files = glob.glob(os.path.join(self.results_dir, "*-linux-*.telemetry.csv"))
        if files:
            newest = max(files, key=os.path.getmtime)
            if newest != self.tele_path:
                self.tele_path = newest
                self._cols, self._last_elapsed = {}, None
                try:
                    with open(newest) as fh:
                        self._cols = {n: i for i, n in enumerate(fh.readline().strip().split(","))}
                except OSError:
                    pass
        if not self.tele_path or not os.path.exists(self.tele_path):
            return
        try:
            with open(self.tele_path) as fh:
                lines = fh.read().strip().splitlines()
            if len(lines) < 2:
                return
            vals = lines[-1].split(",")
        except OSError:
            return

        def get(name):
            i = self._cols.get(name)
            if i is None or i >= len(vals):
                return None
            try:
                return float(vals[i])
            except ValueError:
                return None

        temp, fan = get("temp_c"), get("fan_pct")
        power, util = get("power_w"), get("gpu_util_pct")
        rpm, cpu, ram = get("gpu_fan_rpm"), get("cpu_util_pct"), get("ram_used_gib")
        for key, val in (("temp", temp), ("fan", fan), ("power", power), ("util", util),
                         ("cpu", cpu), ("ram", ram)):
            if val is None:
                continue
            text = "%.0f" % val
            if key == "fan" and rpm is not None:
                text += " / %.0f" % rpm
            self.tiles[key].configure(text=text)
        if temp is not None:
            self.tiles["temp"].configure(foreground=BAD if temp >= 80 else (WARN if temp >= 70 else ACCENT))
        elapsed = get("elapsed_s")
        if elapsed is not None and elapsed != self._last_elapsed:
            self._last_elapsed = elapsed
            self.tel["t"].append(elapsed)
            for key, val in (("temp", temp), ("fan", fan), ("power", power), ("util", util)):
                if val is not None:
                    self.tel[key].append(val)
            for key in self.tel:
                if len(self.tel[key]) > 900:
                    self.tel[key] = self.tel[key][-900:]

    # -------------------------------------------------------------- chart ---
    def _draw_chart(self):
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 80 or h < 80:
            return
        c.delete("all")
        pad_l, pad_r, pad_t, pad_b = 44, 48, 26, 22
        x0, y0, x1, y1 = pad_l, h - pad_b, w - pad_r, pad_t
        # grid + axes
        for i in range(5):
            y = y0 - (y0 - y1) * i / 4
            c.create_line(x0, y, x1, y, fill="#232b37")
            c.create_text(x0 - 6, y, anchor="e", text="%d" % (i * 25), fill=MUTED, font=self.f_small)
        n = len(self.tel["t"])
        if n < 2:
            c.create_text(x0 + 8, pad_t + 8, anchor="nw", text="no samples yet", fill=MUTED,
                          font=self.f_small)
            return
        x_at = lambda i: x0 + (x1 - x0) * i / max(1, n - 1)
        y_temp = lambda v: y0 - (y0 - y1) * min(1.0, max(0.0, v / 100.0))
        y_powr = lambda v: y0 - (y0 - y1) * min(1.0, max(0.0, v / 250.0))
        # temp area + line
        area = [x_at(0), y0]
        for i, v in enumerate(self.tel["temp"]):
            area += [x_at(i), y_temp(v)]
        area += [x_at(len(self.tel["temp"]) - 1), y0]
        c.create_polygon(*area, fill="#3b2430", outline="")
        self._series(c, self.tel["temp"], x_at, y_temp, "#ef6b73", 2)
        self._series(c, self.tel["power"], x_at, y_powr, ACCENT, 2)
        self._series(c, self.tel["fan"], x_at, y_temp, GOOD, 1)
        # right axis (power)
        for i in range(5):
            y = y0 - (y0 - y1) * i / 4
            c.create_text(x1 + 8, y, anchor="w", text="%d" % (i * 63), fill="#3f6ea8", font=self.f_small)
        # legend
        for i, (label, color) in enumerate([("temp °C", "#ef6b73"), ("power W", ACCENT), ("fan %", GOOD)]):
            lx = x0 + 6 + i * 110
            c.create_rectangle(lx, pad_t - 14, lx + 8, pad_t - 6, fill=color, outline="")
            c.create_text(lx + 14, pad_t - 10, anchor="w", text=label, fill=MUTED, font=self.f_small)

    @staticmethod
    def _series(canvas, vals, x_at, y_at, color, width):
        if len(vals) < 2:
            return
        pts = []
        for i, v in enumerate(vals):
            pts += [x_at(i), y_at(v)]
        canvas.create_line(*pts, fill=color, width=width, capstyle="round")

    # ------------------------------------------------------------ results ---
    def _load_results(self):
        try:
            js = sorted(glob.glob(os.path.join(self.results_dir, "*-linux-*.json")),
                        key=os.path.getmtime)
            if not js:
                return
            report = js[-1]
            data = {}
            try:
                with open(report) as fh:
                    data = json.load(fh)
            except Exception:
                pass
            cards = self.summary_tiles
            for ph in data.get("phases", []):
                r = ph.get("result") or {}
                if ph["group"] == "gpu" and ph["phase"] == "soak" and r.get("matmul_tflops") is not None:
                    t = r.get("telemetry") or {}
                    txt = "%.1f TF" % r["matmul_tflops"]
                    if t.get("temp_c"):
                        txt += "  %s°C" % t["temp_c"][2]
                    cards["soak"].configure(text=txt)
                elif ph["group"] == "cpu" and ph["phase"] == "soak" and r.get("gflops") is not None:
                    cards["cpu"].configure(text="%.0f GF" % r["gflops"])
                elif ph["group"] == "ram" and ph["phase"] == "soak" and r.get("gbps") is not None:
                    cards["ram"].configure(text="%.1f GB/s" % r["gbps"])
                elif ph["group"] == "disk" and ph["phase"] == "seqwrite" and r.get("mbps") is not None:
                    cards["disk"].configure(text="%.0f MB/s" % r["mbps"])
                elif ph["group"] == "net" and ph["phase"] == "bench" and r.get("download_mbps") is not None:
                    cards["net"].configure(text="%.0f Mb/s" % r["download_mbps"])
                elif ph["group"] == "gfx" and ph["phase"] == "furmark" and r.get("score") is not None:
                    cards["gfx"].configure(text="%s  %.0f fps" % (r["score"], r.get("fps_avg") or 0))
            # markdown text with light styling
            self.results.delete("1.0", "end")
            md = report[:-5] + ".md"
            if os.path.exists(md):
                with open(md, errors="replace") as fh:
                    for line in fh.read().splitlines():
                        if line.startswith("# "):
                            self.results.insert("end", line[2:] + "\n", ("h1",))
                        elif line.startswith("## "):
                            self.results.insert("end", line[3:] + "\n", ("h2",))
                        elif line.startswith("_") and line.endswith("_"):
                            self.results.insert("end", line.strip("_") + "\n", ("dim",))
                        else:
                            self.results.insert("end", line + "\n")
            self.results.insert("end", "\nreport: %s\n" % report, ("dim",))
        except Exception as exc:
            self.results.insert("end", "could not load report: %s\n" % exc)

    def _auto_collect(self):
        if os.path.exists("/usr/local/bin/bench-collect"):
            self._set_status("collecting results to USB...", ACCENT)

            def work():
                try:
                    subprocess.run(["/usr/local/bin/bench-collect", self.results_dir], timeout=600)
                    self._set_status("results collected to the BENCHDATA partition", GOOD)
                except Exception as exc:
                    self._set_status("collect failed: %s" % exc, WARN)
            threading.Thread(target=work, daemon=True).start()

    def collect(self):
        if os.path.exists("/usr/local/bin/bench-collect"):
            subprocess.run(["/usr/local/bin/bench-collect", self.results_dir], timeout=600)
            return
        dest = filedialog.askdirectory(title="Copy results to...")
        if not dest:
            return
        reports = sorted(glob.glob(os.path.join(self.results_dir, "*-linux-*")), key=os.path.getmtime)
        folder = os.path.join(dest, "bench-results-%s" % time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(folder, exist_ok=True)
        for path in reports:
            if os.path.isdir(path):
                shutil.copytree(path, os.path.join(folder, os.path.basename(path)), dirs_exist_ok=True)
            else:
                shutil.copy2(path, folder)
        self._set_status("results copied to %s" % folder, GOOD)

    def toggle_curve(self):
        if not os.path.exists(FAN_CURVE):
            self._set_status("fan_curve.py not found", BAD)
            return
        cmd = "off" if self._curve_on else "set"

        def work():
            try:
                r = subprocess.run([sys.executable, FAN_CURVE, cmd], capture_output=True,
                                   text=True, timeout=90)
                ok = r.returncode == 0
                self._curve_on = ok and cmd == "set"
                self.q.put(("curve", "Fan curve: %s" % ("on" if self._curve_on else "off")))
                out = (r.stdout or r.stderr).strip().splitlines()
                self._set_status(out[-1] if out else "fan curve %s (rc=%s)" % (cmd, r.returncode),
                                 GOOD if ok else WARN)
            except Exception as exc:
                self._set_status("fan curve failed: %s" % exc, WARN)
        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------- demo ----
    def _demo_data(self):
        self.hw.configure(text="Omarchy · Intel Core i7-3770 · 15.6 GiB  |  NVIDIA GeForce GTX 970")
        demo = [("gpu", "info"), ("gpu", "matmul"), ("gpu", "membw"), ("gpu", "pcie"),
                ("gpu", "conv"), ("gpu", "integrity"), ("gpu", "soak"), ("cpu", "matmul")]
        for i, (grp, ph) in enumerate(demo):
            prev = self.last_row.get(grp)
            zebra = "odd" if len(self.tree.get_children()) % 2 else ""
            if prev:
                self.tree.set(prev, "status", "done")
                self.tree.item(prev, tags=("ok", zebra))
            status = "running" if i == len(demo) - 1 else "done"
            tags = (("run", zebra) if status == "running" else ("ok", zebra))
            item = self.tree.insert("", "end", values=(grp, ph, status, "%d:%02d" % (i // 3, (i * 7) % 60)),
                                    tags=tags)
            self.last_row[grp] = item
        self.tel["t"] = list(range(240))
        for i in range(240):
            phase = min(1.0, i / 60)
            self.tel["temp"].append(30 + 36 * phase + (i % 5) * 0.4)
            self.tel["power"].append(20 + 130 * phase + (i % 7) * 1.5)
            self.tel["fan"].append(26 + 33 * phase)
            self.tel["util"].append(min(100, 100 * phase))
        for key, txt, color in [("temp", "66", ACCENT), ("fan", "59 / 2601", GOOD),
                                ("power", "152", ACCENT), ("util", "100", ACCENT),
                                ("cpu", "87", WARN), ("ram", "9.8", ACCENT)]:
            self.tiles[key].configure(text=txt, foreground=color)
        cards = [("soak", "1.8 TF  67°C"), ("cpu", "152 GF"), ("ram", "8.5 GB/s"),
                 ("disk", "1216 MB/s"), ("net", "243 Mb/s"), ("gfx", "3019  50 fps")]
        for key, val in cards:
            self.summary_tiles[key].configure(text=val)
        self.results.insert("end", "GPU soak\n", ("h1",))
        self.results.insert("end", "1.8 TFLOPS over 600s · 60/66/67 °C\n\n", ("dim",))
        self.results.insert("end", "Full-system soak\n", ("h2",))
        self.results.insert("end", "verdict: clean (exit codes [0,0,0,0], xid lines 0)\n")
        self.progress.configure(value=87)
        self._set_status("demo mode", MUTED)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default=os.path.join(REPO, "results"))
    ap.add_argument("--demo", action="store_true", help="populate with sample data")
    ap.add_argument("--self-test", action="store_true", help="open and exit after a few seconds")
    ap.add_argument("--tab", type=int, default=0, help="select notebook tab (0=Run,1=Telemetry,2=Results,3=Log)")
    a = ap.parse_args()
    root = tk.Tk()
    Dashboard(root, a.results_dir, demo=a.demo, self_test=a.self_test, tab=a.tab)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
