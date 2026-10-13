"""Dashboard phase-table logic: a new phase must close every row still marked
running (including the previous group's last row). Uses the real classes with a
headless Tk root / a stubbed TUI; skips cleanly if the UI toolkit is missing."""
import collections
import os
import re
import sys

from harness import Checker, BENCH, REPO

LINES = ["[sys] inventory", "[gpu] info", "[gpu] soak", "[cpu] matmul", "[net] bench",
         "[fullsoak] gpu + cpu + ram + disk concurrently"]


def _gui(c):
    import os
    prev_cwd = os.getcwd()
    try:
        import tkinter as tk
        os.chdir(REPO)
        sys.path.insert(0, BENCH)
        import gui
    except Exception as e:
        print("  SKIP gui rows (%s)" % e)
        os.chdir(prev_cwd)
        return
    try:
        root = tk.Tk(); root.withdraw()
    except Exception as e:
        print("  SKIP gui rows (no display: %s)" % e)
        os.chdir(prev_cwd)
        return
    try:
        d = gui.Dashboard(root, os.path.join(REPO, "results"))
        for ln in LINES:
            d._handle_line(ln)
        rows = [(d.tree.set(i, "group"), d.tree.set(i, "status")) for i in d.tree.get_children()]
        last = rows[-1]
        c.check("gui rows: all closed except last",
                all(s == "done" for _, s in rows[:-1]) and last[1] == "running", rows)
        c.check("gui rows: cross-group closed", rows[1][1] == "done" and rows[3][1] == "done", rows)
        root.destroy()
    except Exception as e:
        c.check("gui rows", False, e)
    finally:
        os.chdir(prev_cwd)


def _tui(c):
    try:
        sys.path.insert(0, BENCH)
        import tui
    except Exception as e:
        print("  SKIP tui rows (%s)" % e)
        return
    t = tui.BenchTUI.__new__(tui.BenchTUI)
    t.log = collections.deque(maxlen=2000)
    t.phases, t.last_phase, t.follow = [], {}, False
    t.tracker = None
    t.pump = None
    for ln in LINES:
        t._handle(ln)
    rows = [(p["group"], p["status"]) for p in t.phases]
    c.check("tui rows: all closed except last",
            all(s == "ok" for _, s in rows[:-1]) and rows[-1][1] == "run", rows)
    c.check("tui rows: cross-group closed", rows[1][1] == "ok" and rows[3][1] == "ok", rows)

    # ---- the GPU bar read the wrong key, so it showed "--" everywhere ----
    src = open(os.path.join(BENCH, "tui.py"), encoding="utf-8").read()
    c.check("tui does not look for gpu_util_pct in csv_now",
            'csv_now.get("gpu_util_pct")' not in src, "")
    c.check("tui reads the canonical gpu_util key",
            'csv_now.get("gpu_util")' in src, "")
    t.csv_now = {"gpu_util": 93.0, "temp_c": 63.0}
    c.check("a gpu_util value is what the bar consumes",
            t.csv_now.get("gpu_util") == 93.0, t.csv_now)

    # ---- CPU/RAM sampling must work off Linux too ----
    # it used to read /proc only, so on Windows the CPU bar, the RAM/load line and
    # the per-core sparkline were all blank
    t.sys_cpu, t.sys_cores = 0.0, []
    t.sys_ram, t.sys_swap, t.sys_load = (0.0, 0.0), (0.0, 0.0), 0.0
    t._prev_stat, t._prev_win_cpu = None, None
    if t._sample_system_psutil():
        c.check("tui samples CPU/RAM via psutil (cross-platform)",
                t.sys_ram[1] > 0 and t.sys_cores, (t.sys_cpu, t.sys_ram, len(t.sys_cores)))
    else:
        c.check("tui samples CPU/RAM via psutil (cross-platform)", False,
                "psutil unavailable - the venv should have it")
    if os.name == "nt":
        t.sys_cpu, t.sys_ram = 0.0, (0.0, 0.0)
        t._prev_win_cpu = None
        t._sample_system_win()
        t._sample_system_win()          # GetSystemTimes needs a previous sample
        c.check("tui samples CPU/RAM via the Windows API without psutil",
                t.sys_ram[1] > 0, t.sys_ram)


def _web(c):
    """The web live view: runner tabs, and panels that show both runs.

    Source-level, because the suite has no browser. The behaviour was verified by
    hand against three live runs (tabs for all three runners, switching changes the
    pane, panels show CURRENT RUN + LAST RUN, no console errors).
    """
    js = open(os.path.join(REPO, "server", "web", "app.js"), encoding="utf-8").read()
    css = open(os.path.join(REPO, "server", "web", "style.css"), encoding="utf-8").read()
    html = open(os.path.join(REPO, "server", "web", "index.html"), encoding="utf-8").read()
    c.check("the live view builds a tab per runner",
            "function focusTabsInner" in js and "data-rtab=" in js, "")
    c.check("a tab prefers the runner's live run over its last one",
            "function runnerRun" in js and "STATE.active.find((a) => a.runner_id === rid)" in js, "")
    c.check("a finished run is not called the last run while one is running",
            'a.status !== "running"' in js, "")
    c.check("the runner panel shows the current run", 'rows.push(["current run"' in js, "")
    c.check("the runner panel shows the last run too", 'rows.push(["last run"' in js, "")
    c.check("the last run row carries its headline metrics",
            all(k in js for k in ("gpu_soak_tflops", "cpu_soak_gflops", "ram_soak_gbps", "gfx")), "")
    c.check("clicking a runner panel opens its live view",
            "t.dataset.runner &&" in js and "runnerRun(t.dataset.runner)" in js, "")
    c.check("panel controls keep their own behaviour",
            'e.target.closest("button,input,select,textarea,a,form,label")' in js, "")
    c.check("the panel does not duplicate the card's own last-seen line",
            'rows.push([r.online ? "seen"' not in js and "last seen ${ago(r.last_seen)}" in js, "")
    c.check("the web start payload only sends stable_hold when stable is ticked",
            '? { stable: 1, stable_hold:' in js and
            'stable_hold: +$("#mStableHold").value,\n      gfx_engine' not in js, "")
    # the web preset tunes must not enable the plateau soak behind the operator's
    # back: TUNE_DEFAULT carries stable_hold, and extra_runner_args treats a bare
    # stable_hold as "soak until stable"
    _web_tune = re.search(r"const TUNE_DEFAULT = \{(.*?)\};", js, re.S)
    c.check("TUNE_DEFAULT carries stable_hold (so the payload must gate it)",
            bool(_web_tune) and "stable_hold" in _web_tune.group(1), "")

    # ---- W11: performance (PERF-P1-002) ----
    c.check("the list view asks for a small page, not 300 rows",
            "const HIST_LIMIT = 50" in js and "limit=300" not in js, "")
    c.check("the heavy views are only fetched while their tab is on screen",
            'PERF-P1-002' in js and "renderHistory();" in js and "renderTrends();" in js
            and 'classList.contains("active")' in js, "")
    c.check("polling pauses while the tab is hidden and resumes on show",
            "visibilitychange" in js and "document.hidden" in js and "_pollPaused" in js, "")
    c.check("a fetch is skipped when nothing changed (ETag / cheap version probe)",
            "If-None-Match" in js and 'headers.get("ETag")' in js and "function stateVersion" in js, "")
    c.check("repeated errors back off exponentially, capped",
            "BACKOFF_MAX_MS" in js and "function backoffDelay" in js and "Math.pow(2" in js, "")

    # ---- W11: responsive layout (MOB-P2-001..004) ----
    c.check("the primary tab bar wraps or scrolls on phones",
            "flex-wrap:wrap" in css and "overflow-x:auto" in css and "flex-wrap:nowrap" in css, "")
    c.check("fixed multi-column grids collapse on phones",
            "@media (max-width:640px)" in css and "grid-template-columns:1fr" in css, "")
    c.check("interactive controls get >= 44px touch targets",
            "min-height:44px" in css, "")
    c.check("header and filter bars wrap instead of clipping",
            ".topbar{flex-wrap:wrap" in css.replace(" ", "")
            and ".filters{" in css.replace(" ", "") and "flex-wrap:wrap" in css, "")

    # ---- W11: keyboard and assistive tech (UX-P2-001..004) ----
    c.check("the modal is a dialog with a focus trap and focus restore",
            'setAttribute("role", "dialog")' in js and 'aria-modal' in js
            and 'e.key !== "Tab"' in js and "_lastFocus" in js, "")
    c.check("the tab strip exposes ARIA roles and state",
            'role="tablist"' in html and 'role="tab"' in html
            and 'aria-selected' in html and 'aria-controls' in html, "")
    c.check("live SSE updates are announced",
            'aria-live' in html and "function announce" in js and 'announce(' in js, "")
    c.check("canvas charts expose an accessible alternative",
            'setAttribute("role", "img")' in js and 'setAttribute("aria-label"' in js
            and "function chartAltText" in js, "")
    c.check("cards, headers and rows are keyboard reachable and activatable",
            'tabindex="0"' in js and 'tabindex="0" aria-sort' in html
            and 'closest("th.sortable,[data-detail],[data-runner],[data-rtab]")' in js, "")


def run():
    c = Checker("dashboards")
    _gui(c)
    _tui(c)
    _web(c)
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
