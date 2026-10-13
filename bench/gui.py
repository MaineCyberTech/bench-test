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

def _repo_dir():
    """The toolkit checkout, resolved even when frozen (PyInstaller) or run from
    elsewhere: $BENCH_REPO, next to the executable, or the cwd."""
    cands = [os.environ.get("BENCH_REPO")]      # explicit override wins
    if getattr(sys, "frozen", False):
        cands.append(os.path.dirname(os.path.abspath(sys.executable)))
    cands.append(os.getcwd())
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "bench")):
            return os.path.abspath(c)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


REPO = _repo_dir()
BENCH = os.path.join(REPO, "bench")
FAN_CURVE = os.path.join(BENCH, "fan_curve.py")
sys.path.insert(0, BENCH)
try:
    from benchreport import (Reporter, PhaseTracker, ReporterPump, build_summary, build_summary_dir,
                             telemetry_summary, newest_report, load_identity, run_exec, system_info,
                             read_inventory, load_conf, CSV_ALIASES, power_action,
                             supported_groups,
                             update_toolkit, iso_fetch, iso_write, iso_write_slot,
                             self_update, restart_self,
                             toolkit_version, is_live, update_needs_restart, hardware_line,
                             DASH_FIELDS, auto_update, should_restart_after_update,
                             venv_python, upload_telemetry_csv, persist_root,
                             upload_run_files, extra_runner_args, tune_summary,
                             bench_version, version_string, drop_stray_script_arg)
except Exception:  # reporting is optional
    Reporter = None

try:
    import bootstrap  # stdlib-only local env helper
except Exception:
    bootstrap = None

# --- runner: Linux shell script, or the Windows battery via run_win.py ---
# When frozen (PyInstaller) sys.executable is the bundle exe, not python, so the
# runner must be launched with a real interpreter carrying numpy/psutil (the
# sampler records CPU/RAM/disk/net telemetry only with psutil). bootstrap
# provisions that interpreter + a cached env without needing a checkout or a
# system Python; a repo .venv still wins if present.
if os.name == "nt":
    RUNNER = None
    RUNNER_PREFIX = [os.path.join(BENCH, "run_win.py")]

    def runner_command(args):
        py = (bootstrap.python_path(REPO, ensure=True) if bootstrap else "") \
            or venv_python(REPO) or sys.executable
        return [py] + RUNNER_PREFIX + args, py
else:
    RUNNER = os.path.join(BENCH, "Run-LinuxBench.sh")
    RUNNER_PREFIX = []

    def runner_command(args):
        return [RUNNER] + args, ""


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
    ("nvenc", "Video encode"), ("llm", "LLM inference"), ("fan", "Fan response"),
    ("sys", "Inventory"), ("gfx", "Graphics (FurMark)"),
]

_ALL_G = {k: 1 for k in ("gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc",
                         "llm", "fan", "sys", "gfx")}


def _g(*on):
    return {k: (1 if k in on else 0) for k in _ALL_G}


_DEF_DUR = {"gpu": 600, "combined": 300, "fullsoak": 300, "gfx": 60}

# Knobs the runner already accepts but the four spin boxes above do not expose.
# A preset carries them in "tune"; the runner's long defaults apply otherwise
# (cpu 120s, ram 120s, gpu matrix 12288, steady 10GB, disk 2048MB) - the reason a
# "Quick check" used to take over half an hour.
#
# disk_size_mb is the one that bit hardest: the randread/randwrite/randmix phases
# are size-bound, not seconds-bound, so the default 2048MB cost 226s of the
# desktop's 1368s stabilized run. 512MB does the same job in ~57s.
_TUNE = {"cpu_soak": 45, "ram_soak": 30, "disk_seconds": 30, "disk_size_mb": 512,
         "gpu_size": 4096, "cpu_size": 2048, "ram_size_mb": 512, "passes": 1,
         "steady_gb": 2, "stable": 1, "stable_hold": 60, "gfx_engine": "furmark"}

# "Quick check" has to be quick: with no tune it inherits the runner's long
# defaults (cpu 120s, ram 120s, 10 GiB steady write, gpu matrix 12288) and ran
# ~14 min on the live stick.
_QUICK_TUNE = {"cpu_soak": 60, "ram_soak": 30, "disk_seconds": 15, "disk_size_mb": 256,
               "gpu_size": 4096, "cpu_size": 2048, "ram_size_mb": 512, "passes": 1,
               "steady_gb": 2}

PRESETS = {
    "Select a preset...": None,
    "Quick check (short soaks)": {"groups": _g("gpu", "cpu", "ram", "disk", "combined", "fullsoak",
                                               "net", "nvenc", "sys", "gfx"),
                                  "spin": {"gpu": 60, "combined": 60, "fullsoak": 60, "gfx": 30},
                                  "tune": _QUICK_TUNE},
    "Stabilized full soak (15 min)": {"groups": _g("gpu", "cpu", "ram", "disk", "combined", "fullsoak",
                                                   "net", "nvenc", "fan", "sys", "gfx"),
                                      "spin": {"gpu": 180, "combined": 45, "fullsoak": 90, "gfx": 30},
                                      "tune": dict(_TUNE)},
    "System info + 60s soak (all)": {"groups": _g("sys", "fullsoak"),
                                     "spin": {**_DEF_DUR, "fullsoak": 60}},
    "Full run (defaults)": {"groups": _ALL_G, "spin": dict(_DEF_DUR)},
    "GPU compute": {"groups": _g("gpu", "sys"), "spin": {**_DEF_DUR, "gpu": 60}},
    "CPU + RAM + disk": {"groups": _g("cpu", "ram", "disk", "sys"), "spin": dict(_DEF_DUR)},
    "Storage + network": {"groups": _g("disk", "net", "sys"), "spin": dict(_DEF_DUR)},
    "Video / graphics": {"groups": _g("nvenc", "gfx", "sys"), "spin": dict(_DEF_DUR)},
    "Graphics only (FurMark/Valley)": {"groups": _g("gfx", "sys"),
                                       "spin": {**_DEF_DUR, "gfx": 30},
                                       "tune": {"gfx_engine": "furmark"}},
    "LLM inference (Ollama)": {"groups": _g("llm", "sys"), "spin": dict(_DEF_DUR)},
    "Fan response": {"groups": _g("fan", "sys"), "spin": dict(_DEF_DUR)},
    "Soak only (long)": {"groups": _g("gpu", "fullsoak", "sys"),
                         "spin": {**_DEF_DUR, "gpu": 1800, "fullsoak": 1800}},
    "Full-system soak (long)": {"groups": _g("fullsoak", "sys"),
                                "spin": {**_DEF_DUR, "fullsoak": 1800}},
}

STATUS_COLOR = {"running": ACCENT, "done": GOOD, "WARN": WARN, "warn": WARN,
                "error": BAD, "failed": BAD}


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
    def __init__(self, root, results_dir, demo=False, self_test=False, tab=0,
                 server="", token="", name="", instance=None):
        self.root = root
        self.results_dir = results_dir
        self.proc = None
        self.q = queue.Queue()
        self.last_row = {}
        self.starts = {}
        self.tele_path = None
        self._cols = {}
        self._last_elapsed = None
        # {csv basename: {elapsed, ...}} already streamed live, so the end-of-run
        # bulk upload does not store the same samples a second time
        self._tel_sent = {}
        self.tel = {"t": [], "temp": [], "fan": [], "power": [], "util": []}
        self.running = False
        self.exited = False
        self._curve_on = False
        self.run_t0 = None
        self.collected_ok = False
        self._stopped = False
        self._in_tk_error = False
        self.vram_total = None
        self.rep = None
        self.pump = None
        self.tracker = None
        self.server, self.token, self.name = server, token, name
        self.instance = instance if instance is not None else os.environ.get("BENCH_AGENT_INSTANCE", "")

        self._fonts()
        self._styles()
        self._build()
        # With pythonw there is no console: Tk's default handler tries to print a
        # callback traceback to a None stderr, which loses the exception (and used
        # to kill the periodic loop / a pending command). Report it instead.
        self.root.report_callback_exception = self._tk_error
        self._detect_curve()
        self._auto_update()
        if tab:
            try:
                self.nb.select(tab)
            except Exception:
                pass
        self._setup_reporting()
        self._detect_hardware()
        if demo:
            self._demo_data()
        if self_test:
            self.root.after(3000, self.root.destroy)
        self.root.after(200, self._poll)
        self.root.after(1000, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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
        ttk.Label(head, text="  %s workstation benchmark" % ("Windows" if os.name == "nt" else "Linux"),
                  style="Dim.TLabel").pack(side="left")
        self.hw = ttk.Label(head, text="detecting hardware...", style="Dim.TLabel")
        self.hw.pack(side="right")
        self.ver = ttk.Label(head, text="v%s" % bench_version(REPO), style="Dim.TLabel")
        self.ver.pack(side="right", padx=(0, 10))

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
        # extra tuning knobs carried by a preset / a control-plane start command
        # (cpu/ram soak, sizes, passes, steady GB, soak-until-stable). They have no
        # spin boxes of their own; the preset or the control plane supplies them.
        self._tune = {}

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

        # the extra knobs a preset carries (cpu/ram soak, sizes, passes, ...) have
        # no spin box of their own; show them so the profile is not a black box
        self.tune_lbl = ttk.Label(side, text="", style="Card.TLabel", font=self.f_small,
                                  wraplength=210, justify="left")
        self.tune_lbl.pack(anchor="w", pady=(0, 8))

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
        self.env_btn = ttk.Button(btns, text="Clean up environment", style="Ghost.TButton",
                                  command=self.clean_env)
        self.env_btn.pack(fill="x", pady=2)

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
        self._tile_rows = []
        row = None
        for i, (key, label, _dec) in enumerate(DASH_FIELDS):
            if i % 8 == 0:                       # wrap into rows of 8
                row = ttk.Frame(tiles, style="Card.TFrame")
                row.pack(fill="x", pady=(0, 6))
                self._tile_rows.append(row)
            card = self._card(row, padding=8)
            card.pack(side="left", expand=True, fill="x", padx=(0, 6))
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
        try:
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
        except Exception:
            pass            # never let logging raise (it runs inside Tk callbacks)

    def _tk_error(self, etype, value, tb):
        """Tk callback exceptions: report them instead of dying silently.

        Must be safe to call from within Tk itself: it may not touch widgets
        (that can raise and re-enter this handler -> a busy loop), so the message
        only goes to the pump / stderr.
        """
        if self._in_tk_error:
            return
        self._in_tk_error = True
        try:
            import traceback
            last = traceback.format_exception(etype, value, tb)[-1].strip()
            msg = "[gui] callback error: %s: %s" % (etype.__name__, value)
            try:
                if self.pump:
                    self.pump.push_log(msg)
                    self.pump.push_log("[gui] %s" % last)
            except Exception:
                pass
            try:
                sys.stderr.write(msg + "\n" + last + "\n")
            except Exception:
                pass
        finally:
            self._in_tk_error = False

    def _diag(self, line):
        """Our own diagnostics: show locally *and* stream to the control plane,
        so a broken UI thread or a failed registration is visible in the run's
        console instead of only on screen."""
        self._log(line)
        try:
            if self.pump:
                self.pump.push_log(line)
        except Exception:
            pass

    def _set_status(self, text, color=MUTED):
        self.status.configure(text=text)
        self.status_dot.configure(fg=color)

    def _detect_hardware(self):
        def work():
            si = {}
            try:
                si = system_info() or {}
            except Exception:
                si = {}
            try:
                import gpu_util
                gpu_util.probe()
                if gpu_util.NAME and not si.get("gpu"):
                    si["gpu"] = gpu_util.NAME
                if gpu_util.VRAM_TOTAL_MIB:
                    self.vram_total = gpu_util.VRAM_TOTAL_MIB
            except Exception:
                pass
            if not self.vram_total:
                try:
                    digits = "".join(c for c in str(si.get("vram") or "") if c.isdigit())
                    if digits:
                        self.vram_total = int(digits)
                except Exception:
                    pass
            text = hardware_line(si)
            self.q.put(("hw", text or "hardware detection unavailable"))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------- reporting ------
    def _setup_reporting(self):
        if not (self.server and self.token and Reporter):
            return
        try:
            import platform as _p
            ident = load_identity(self.server)
            # name=None lets Reporter fall back to $BENCH_AGENT_NAME or the
            # hostname (an explicit --name still wins).
            self.rep = Reporter(self.server, self.token, name=self.name or None,
                                kind="gui", os_name=_p.system(), log=self._log,
                                version=version_string(REPO),
                                runner_id=ident.get("runner_id"), api_key=ident.get("api_key"),
                                persist=True, instance=self.instance)
            self.sysinfo = system_info()
            caps = {"os": _p.system(), "platform": _p.platform(), "kind": "gui",
                    "groups": supported_groups(), "toolkit": toolkit_version(REPO),
                    "live": is_live(),
                    "gpu": self.sysinfo.get("gpu"), "driver": self.sysinfo.get("driver"),
                    "cpu": self.sysinfo.get("cpu"), "ram_gib": self.sysinfo.get("ram_gib")}
            self._connect_reporting(caps)
        except Exception as exc:
            self._log("[report] disabled: %s" % exc)
            self.rep = None

    def _connect_reporting(self, caps):
        # retry until the server is reachable (the network can be up late at boot)
        def work():
            while self.rep and not self.rep.register(caps):
                time.sleep(5)
            if self.rep and self.rep.runner_id:
                try:
                    self.pump = ReporterPump(self.rep, on_command=self._remote_command,
                                             log=self._log)
                    self.pump.start()
                except Exception as exc:
                    self._diag("[report] pump failed: %r" % exc)
                    return
                self._diag("[report] connected to %s" % self.server)
                self._diag("[gui] hw: %s" % self.hw)
        threading.Thread(target=work, daemon=True).start()

    def _remote_command(self, cmd):
        # called from the pump thread - marshal onto the Tk thread
        self.root.after(0, lambda: self._apply_remote(cmd))

    def _apply_remote(self, cmd):
        kind = cmd.get("kind")
        payload = cmd.get("payload") or {}
        if kind == "start":
            if self.running:
                self.rep.ack(cmd.get("id"), "failed", {"error": "already running"})
                return
            self._apply_profile(payload)
            self.rep.ack(cmd.get("id"), "done", {"started": True})
            self.start(profile=payload)
        elif kind == "stop":
            self.rep.ack(cmd.get("id"), "done", {"stopping": True})
            self.stop()
        elif kind == "fan_curve":
            self.toggle_curve()
            self.rep.ack(cmd.get("id"), "done", {"toggled": True})
        elif kind in ("shutdown", "reboot"):
            if self.running:
                self.stop()
            self.rep.ack(cmd.get("id"), "done", power_action(reboot=(kind == "reboot")))
        elif kind in ("update", "update_toolkit", "iso_fetch", "iso_write", "iso_write_slot"):
            self._update_remote(cmd.get("id"), kind, payload)
        elif kind == "restart":
            self.rep.ack(cmd.get("id"), "done", {"restart": "scheduled"})
            self.root.after(0, self._restart_runner)
        elif kind in ("exec", "shell"):
            self._exec_remote(cmd.get("id"), payload)
        else:
            self.rep.ack(cmd.get("id"), "failed", {"error": "unsupported: %s" % kind})

    def _restart_runner(self):
        """Restart cleanly: start the successor, then close this window.

        Must run on the Tk thread (widgets), and must not use os._exit from a
        worker thread (that leaves a ghost window and can strand the successor).
        """
        if self.running:
            self._diag("[gui] restart deferred: a run is in progress")
            return
        try:
            subprocess.Popen([sys.executable] + list(sys.argv), cwd=REPO, close_fds=True)
        except Exception as exc:
            self._diag("[gui] restart failed: %r" % exc)
            return
        self._set_status("restarting…", ACCENT)
        self.root.after(400, self._quit_clean)

    def _quit_clean(self):
        try:
            self._stop_soft()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def _stop_soft(self):
        """Best-effort shutdown: stop the pump/threads without touching a run."""
        try:
            if self.pump:
                self.pump.stop()
        except Exception:
            pass

    def _update_remote(self, cid, kind, payload):
        if self.running:
            # the toolkit copy is what the running battery executes from (and
            # where its results land), so switching it mid-run breaks the run
            self.rep.ack(cid, "failed", {
                "error": "a run is in progress - update once it finishes"})
            return

        def work():
            try:
                if kind == "iso_fetch":
                    res = iso_fetch(payload.get("tag") or "", payload.get("gpu") or "nvidia-legacy",
                                    payload.get("dest"), log=self._log)
                elif kind == "iso_write":
                    res = iso_write(payload.get("iso"), payload.get("device"),
                                    bool(payload.get("confirm")), log=self._log)
                elif kind == "iso_write_slot":
                    res = iso_write_slot(payload.get("iso"), bool(payload.get("confirm")),
                                         log=self._log)
                elif kind == "update_toolkit":
                    res = update_toolkit(payload.get("ref") or "main", payload.get("sha256"),
                                         log=self._log, root=REPO)
                else:
                    res = self_update(REPO, log=self._log)
            except Exception as e:
                res = {"error": "%s: %s" % (type(e).__name__, e)}
            self.rep.ack(cid, "done" if res.get("ok") else "failed", res)
            if should_restart_after_update(res, running=self.running,
                                           requested=bool(payload.get("restart"))):
                self.root.after(0, self._restart_runner)
        threading.Thread(target=work, daemon=True).start()

    def _exec_remote(self, cid, payload):
        def work():
            res = run_exec(self.rep, payload.get("cmd", ""),
                           timeout=int(payload.get("timeout") or 120), cwd=REPO)
            self.rep.ack(cid, "done" if res.get("rc") == 0 else "failed", res)
        threading.Thread(target=work, daemon=True).start()

    def _apply_profile(self, profile):
        for key, val in (profile.get("groups") or {}).items():
            if key in self.gvars:
                self.gvars[key].set(bool(val))
        for g in (profile.get("skip") or []):
            if g in self.gvars:
                self.gvars[g].set(False)
        for key, val in {"gpu": profile.get("gpu_soak"), "combined": profile.get("combined"),
                         "fullsoak": profile.get("fullsoak"), "gfx": profile.get("gfx")}.items():
            if val not in (None, "") and key in self.svars:
                self.svars[key].set(str(val))
        # keep every other knob the control plane sent (cpu/ram soak, sizes, passes,
        # steady GB, soak-until-stable) - extra_runner_args picks out the known keys
        self._tune = dict(profile or {})
        self._show_tune()

    # -------------------------------------------------------- run control ---
    def _apply_preset(self, _evt=None):
        preset = PRESETS.get(self.preset.get())
        if not preset:
            return
        groups = preset.get("groups")
        if groups is None:
            groups = {k: 1 for k in self.gvars}
        for key, val in groups.items():
            if key in self.gvars:
                self.gvars[key].set(bool(val))
        for key, val in (preset.get("spin") or {}).items():
            if key in self.svars:
                self.svars[key].set(str(val))
        self._tune = dict(preset.get("tune") or {})
        self._show_tune()

    def _show_tune(self):
        lbl = getattr(self, "tune_lbl", None)
        if lbl is None:
            return
        s = tune_summary(getattr(self, "_tune", None))
        lbl.configure(text=("tune: " + s) if s else "")

    def start(self, profile=None):
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
        args += extra_runner_args(getattr(self, "_tune", None))
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.last_row, self.starts = {}, {}
        self.tele_path, self._cols, self._last_elapsed = None, {}, None
        self._tel_sent = {}
        self.tel = {"t": [], "temp": [], "fan": [], "power": [], "util": []}
        self.exited = False
        self._stopped = False
        self.run_t0 = time.time()
        os.makedirs(self.results_dir, exist_ok=True)
        env = os.environ.copy()
        try:
            cmd, py = runner_command(args)
            if py:
                env.setdefault("BENCH_PY", py)
                env["PATH"] = os.path.dirname(py) + os.pathsep + env.get("PATH", "")
            self.proc = subprocess.Popen(cmd, cwd=REPO, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1, start_new_session=True,
                                         creationflags=(subprocess.CREATE_NO_WINDOW
                                                        if os.name == "nt" else 0))
        except Exception as exc:
            self._set_status("failed to start: %s" % exc, BAD)
            return
        self.running = True
        if self.rep and self.rep.runner_id:
            self.tracker = PhaseTracker(self.rep)
            si = getattr(self, "sysinfo", None) or {}
            # record the effective profile (including the tune knobs) so a locally
            # started run is reproducible from history, not just a remote one
            prof = profile or {
                "preset": self.preset.get(),
                "groups": {k: (1 if v.get() else 0) for k, v in self.gvars.items()},
                "gpu_soak": self.svars["gpu"].get(), "combined": self.svars["combined"].get(),
                "fullsoak": self.svars["fullsoak"].get(), "gfx": self.svars["gfx"].get(),
                **(getattr(self, "_tune", None) or {}),
            }
            self.rep.start_run(prof.get("label") or self.preset.get() or "run", prof,
                               gpu=si.get("gpu"), driver=si.get("driver"), system=si,
                               preset=prof.get("preset") or self.preset.get())
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status("running · %s" % (self.preset.get() or "run").split(" (")[0], ACCENT)
        self._log("[gui] %s" % " ".join(cmd))
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def clean_env(self):
        """Remove the cached runner interpreter + env (keeps results)."""
        if bootstrap is None:
            self._set_status("environment helper unavailable", BAD)
            return
        self.env_btn.configure(state="disabled")
        try:
            st = bootstrap.status()
            if not st.get("env_present") and not st.get("python_present"):
                self._set_status("nothing cached yet", ACCENT)
            else:
                bootstrap.clean(log=self._log)
                self._set_status("environment cleaned", ACCENT)
        except Exception as exc:
            self._set_status("clean failed: %s" % exc, BAD)
        finally:
            self.env_btn.configure(state="normal")

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                self.q.put(line)
        except Exception:
            pass
        self.q.put(None)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self._stopped = True
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                                   capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                pass
            self._set_status("stopping...", WARN)

    def _handle_line(self, line):
        self._log(line)
        if self.tracker:
            self.tracker.feed(line)
        if self.pump:
            self.pump.push_log(line)
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
                # phases run one at a time, so when a new one starts any row still
                # marked "running" has finished (this also closes a group's last row
                # when the run moves on to the next group)
                for item in self.tree.get_children():
                    if self.tree.set(item, "status") == "running":
                        tags = [t for t in self.tree.item(item, "tags") if t != "run"] + ["ok"]
                        self.tree.set(item, "status", "done")
                        self.tree.item(item, tags=tags)
                zebra = "odd" if len(self.tree.get_children()) % 2 else ""
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
        if self.tracker:
            self.tracker.close()
            self.tracker = None
        if self.rep and self.rep.run_id:
            try:
                summary = build_summary_dir(self.results_dir, since=self.run_t0)
                csvs = glob.glob(os.path.join(self.results_dir, "*.telemetry.csv"))
                if csvs:
                    ts = telemetry_summary(max(csvs, key=os.path.getmtime))
                    if ts:
                        summary["telemetry"] = ts
            except Exception as e:
                # never let a summary build failure pass for an empty summary: an
                # earlier NameError here was swallowed into {} and the run looked
                # like it had measured nothing
                summary = {"summary_error": "%s: %s" % (type(e).__name__, e)}
            si = getattr(self, "sysinfo", None) or {}
            inv = read_inventory(self.results_dir) or si
            upload_telemetry_csv(self.rep, self.results_dir, sent=self._tel_sent)
            upload_run_files(self.rep, self.results_dir)
            self.rep.finish("done" if rc == 0 else ("stopped" if self._stopped else "failed"),
                            rc or 0, summary, gpu=inv.get("gpu"), driver=inv.get("driver"),
                            system=inv)
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
                elif isinstance(item, tuple) and item[0] == "status":
                    self._set_status(item[1], item[2])
                else:
                    self._handle_line(str(item).rstrip("\n"))
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _tick(self):
        try:
            self._tick_once()
        except Exception as exc:            # never let one bad tick stop the loop
            import traceback
            self._diag("[gui] tick error: %r" % exc)
            self._diag(traceback.format_exc().strip().splitlines()[-1])
        finally:
            self.root.after(1000, self._tick)

    def _tick_once(self):
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
        # NB: re-arming is done by _tick()'s finally - do NOT schedule here too,
        # or the pending-timer count doubles every second and swamps the loop.

    def _read_telemetry(self):
        files = glob.glob(os.path.join(self.results_dir, "*.telemetry.csv"))
        if files:
            newest = max(files, key=os.path.getmtime)
            if newest != self.tele_path:
                self.tele_path = newest
                self._cols, self._last_elapsed = {}, None
                try:
                    with open(newest) as fh:
                        for i, n in enumerate(fh.readline().strip().split(",")):
                            canon = CSV_ALIASES.get(n.strip().lower(), n.strip().lower())
                            if canon and canon not in self._cols:
                                self._cols[canon] = i
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

        def raw(name):
            i = self._cols.get(name)
            return vals[i].strip() if (i is not None and i < len(vals)) else None

        # every dashboard field, from the shared spec (decimals "-" = text)
        for key, _label, dec in DASH_FIELDS:
            v = raw(key) if dec == "-" else get(key)
            if v not in (None, ""):
                self.tiles[key].configure(text=(v if dec == "-" else "%.*f" % (dec, v)))
        temp, fan, power, util = get("temp_c"), get("fan_pct"), get("power_w"), get("gpu_util")
        rpm = get("gpu_fan_rpm")
        if fan is not None and rpm is not None:
            self.tiles["fan_pct"].configure(text="%.0f / %.0f" % (fan, rpm))
        if temp is not None:
            self.tiles["temp_c"].configure(foreground=BAD if temp >= 80 else (WARN if temp >= 70 else ACCENT))
        if get("throttle"):
            self.tiles["throttle"].configure(foreground=BAD, text="yes")
        elapsed = get("elapsed")
        if elapsed is not None and elapsed != self._last_elapsed:
            self._last_elapsed = elapsed
            self.tel["t"].append(elapsed)
            for key, val in (("temp", temp), ("fan", fan), ("power", power), ("util", util)):
                if val is not None:
                    self.tel[key].append(val)
            if self.pump:
                sample = {k: v for k, v in
                          ((n, get(n)) for n in self._cols) if v is not None}
                if sample:
                    self.pump.push_sample(sample)
                    self._tel_sent.setdefault(
                        os.path.basename(self.tele_path or ""), set()).add(elapsed)
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
        self._series(c, self.tel["util"], x_at, y_temp, GOOD, 1)
        self._series(c, self.tel["fan"], x_at, y_temp, WARN, 1)
        # right axis (power)
        for i in range(5):
            y = y0 - (y0 - y1) * i / 4
            c.create_text(x1 + 8, y, anchor="w", text="%d" % (i * 63), fill="#3f6ea8", font=self.f_small)
        # legend
        for i, (label, color) in enumerate([("temp °C", "#ef6b73"), ("power W", ACCENT),
                                            ("gpu %", GOOD), ("fan %", WARN)]):
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
            # prefer a real aggregate report; never treat infra dumps as one
            infra = (".info.json", ".sysinfo.json", ".sensors.json", ".pcie.json",
                     ".health.json", ".env.json", ".manifest.json", ".llm.json",
                     ".nvenc.json")
            js = sorted(glob.glob(os.path.join(self.results_dir, "*-linux-*.json")),
                        key=os.path.getmtime)
            if not js:
                js = sorted([p for p in glob.glob(os.path.join(self.results_dir, "*.json"))
                             if not p.endswith(infra)], key=os.path.getmtime)
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
                if "group" not in ph:
                    continue
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
            if not os.path.exists(md):
                cands = sorted(glob.glob(os.path.join(self.results_dir, "*.md")),
                               key=os.path.getmtime)
                md = cands[-1] if cands else md
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
                    r = subprocess.run(["/usr/local/bin/bench-collect", self.results_dir], timeout=600,
                                       capture_output=True, text=True)
                    out = ((r.stdout or "") + (r.stderr or "")).strip()
                    try:
                        with open(os.path.join(self.results_dir, "collect.log"), "a") as fh:
                            fh.write(out + "\n")
                    except Exception:
                        pass
                    lines = [ln for ln in out.splitlines() if ln.strip()]
                    last = (lines[-1] if lines else "no output").replace("[collect] ", "")
                    if r.returncode == 0:
                        self.collected_ok = True
                        self._set_status("saved to USB: %s" % last[:70], GOOD)
                    else:
                        self._set_status("USB save failed - %s" % last[:60], WARN)
                except Exception as exc:
                    self._set_status("collect failed: %s" % exc, WARN)
            threading.Thread(target=work, daemon=True).start()

    def _on_close(self):
        # closing the window cancels an in-flight run so it never gets stuck
        if self.running and self.proc and self.proc.poll() is None:
            self.stop()
            for _ in range(30):
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
            try:
                self._on_exit()
            except Exception:
                pass
        if self.pump:
            self.pump.stop()
        # never close with uncollected results: final synchronous attempt
        if os.path.exists("/usr/local/bin/bench-collect") and not self.collected_ok:
            try:
                r = subprocess.run(["/usr/local/bin/bench-collect", self.results_dir],
                                   timeout=60, capture_output=True, text=True)
                self.collected_ok = (r.returncode == 0)
            except Exception:
                pass
        self.root.destroy()

    def collect(self):
        if os.path.exists("/usr/local/bin/bench-collect"):
            try:
                r = subprocess.run(["/usr/local/bin/bench-collect", self.results_dir], timeout=600,
                                   capture_output=True, text=True)
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                self.results.insert("end", "\n" + out + "\n")
                self.results.see("end")
            except Exception as exc:
                self.results.insert("end", "collect failed: %s\n" % exc)
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

    def _auto_update(self):
        """Check for a newer checkout at startup and restart into it if pulled."""
        def work():
            def report(msg):
                try:
                    self.root.after(0, lambda: self._diag(msg))
                except Exception:
                    pass
            res = auto_update(REPO, log=report)
            if res.get("updated"):
                report("[update] restarting into the newer checkout")
                try:
                    self.root.after(0, self._restart_runner)
                except Exception:
                    pass
        threading.Thread(target=work, daemon=True).start()

    def _detect_curve(self):
        """Reflect the real fan-curve state in the button at startup.

        On Windows the curve is a scheduled nvfancontrol task; on Linux it is the
        resident fan_curve.py process.
        """
        def work():
            state = "off"
            try:
                if os.name == "nt":
                    r = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "(Get-ScheduledTask -TaskName 'NVIDIA GPU Fan Curve' "
                         "-ErrorAction SilentlyContinue).State"],
                        capture_output=True, text=True, timeout=30,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    state = "on" if any(s in (r.stdout or "") for s in ("Ready", "Running")) else "off"
                else:
                    r = subprocess.run(["pgrep", "-f", "fan_curve.py"], capture_output=True,
                                       text=True, timeout=10)
                    state = "on" if r.returncode == 0 else "off"
            except Exception:
                pass
            self._curve_on = (state == "on")
            self.q.put(("curve", "Fan curve: %s" % state))
        threading.Thread(target=work, daemon=True).start()

    def toggle_curve(self):
        if os.name == "nt":
            return self._toggle_curve_windows()
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
                self.q.put(("status", out[-1] if out else "fan curve %s (rc=%s)" % (cmd, r.returncode),
                            GOOD if ok else WARN))
            except Exception as exc:
                self.q.put(("status", "fan curve failed: %s" % exc, WARN))
        threading.Thread(target=work, daemon=True).start()

    def _toggle_curve_windows(self):
        """Windows fan curve: install/remove the nvfancontrol scheduled task.

        Installing needs elevation, so the script prompts for UAC itself; report
        the real outcome rather than assuming it worked.
        """
        ps1 = os.path.join(BENCH, "Install-GpuFanCurve.ps1")
        if not os.path.exists(ps1):
            self._set_status("Install-GpuFanCurve.ps1 not found", BAD)
            return
        turning_off = self._curve_on
        args = ["-Uninstall"] if turning_off else []

        def work():
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                    "-File", ps1] + args,
                                   capture_output=True, text=True, timeout=300,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = ((r.stdout or "") + (r.stderr or "")).strip()
                ok = r.returncode == 0
                self._curve_on = ok and not turning_off
                self.q.put(("curve", "Fan curve: %s" % ("on" if self._curve_on else "off")))
                denied = "denied" in out.lower() or "elevation" in out.lower() or "canceled" in out.lower()
                if ok:
                    msg = "fan curve %s" % ("removed" if turning_off else "installed (nvfancontrol task)")
                elif denied:
                    msg = "fan curve needs admin (UAC prompt declined)"
                else:
                    msg = "fan curve failed (rc=%s)" % r.returncode
                self.q.put(("status", msg, GOOD if ok else WARN))
                if self.pump:
                    self.pump.push_log("[fan] %s" % (out.splitlines()[-1] if out else msg))
            except Exception as exc:
                self.q.put(("status", "fan curve failed: %s" % exc, WARN))
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
        for key, txt, color in [("temp_c", "66", ACCENT), ("fan_pct", "59 / 2601", GOOD),
                                ("power_w", "152", ACCENT), ("gpu_util", "100", ACCENT),
                                ("cpu_util", "87", WARN), ("ram_used", "9.8", ACCENT),
                                ("sm_mhz", "2751", ACCENT), ("vram_used_mib", "9012", ACCENT),
                                ("pstate", "P0", ACCENT), ("throttle", "0", GOOD)]:
            if key in self.tiles:
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
    if "drop_stray_script_arg" in globals():     # absent on an older toolkit
        sys.argv = drop_stray_script_arg(me=__file__)
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir",
                    default=os.path.join(persist_root() if is_live() else REPO, "results"))
    ap.add_argument("--demo", action="store_true", help="populate with sample data")
    ap.add_argument("--self-test", action="store_true", help="open and exit after a few seconds")
    ap.add_argument("--tab", type=int, default=0, help="select notebook tab (0=Run,1=Telemetry,2=Results,3=Log)")
    ap.add_argument("--server", default=load_conf()[0],
                    help="benchd control-plane URL (report + remote control)")
    ap.add_argument("--token", default=load_conf()[1], help="control-plane token")
    ap.add_argument("--name", default="", help="runner name reported to the control plane")
    ap.add_argument("--instance", default="",
                    help="instance label (e.g. tty1) when several dashboards share a host")
    a = ap.parse_args()
    root = tk.Tk()
    Dashboard(root, a.results_dir, demo=a.demo, self_test=a.self_test, tab=a.tab,
              server=a.server, token=a.token, name=a.name, instance=a.instance)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
