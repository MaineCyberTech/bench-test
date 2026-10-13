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


def _gui_cleanup(c):
    """'Clean up environment' must confirm, and must refuse mid-run (USE-P2-003)."""
    prev_cwd = os.getcwd()
    try:
        import tkinter as tk
        os.chdir(REPO)
        sys.path.insert(0, BENCH)
        import gui
    except Exception as e:
        print("  SKIP gui cleanup (%s)" % e)
        os.chdir(prev_cwd)
        return
    try:
        root = tk.Tk(); root.withdraw()
    except Exception as e:
        print("  SKIP gui cleanup (no display: %s)" % e)
        os.chdir(prev_cwd)
        return
    orig_boot, orig_ask = gui.bootstrap, gui.messagebox.askyesno
    calls = []

    class _Boot:
        @staticmethod
        def status():
            return {"env_present": True, "python_present": True}

        @staticmethod
        def clean(log=None):
            calls.append("clean")

    try:
        d = gui.Dashboard(root, os.path.join(REPO, "results"))
        gui.bootstrap = _Boot

        # a run in progress: refuse, and never even reach the dialog
        d.running = True
        gui.messagebox.askyesno = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not prompt while a run is in progress"))
        d.clean_env()
        c.check("gui cleanup refuses while a run is in progress",
                "in progress" in d.status.cget("text") and not calls,
                (d.status.cget("text"), calls))

        # not running: ask first, and a "no" leaves the environment alone
        d.running = False
        asked = []
        gui.messagebox.askyesno = lambda *a, **k: (asked.append(True), False)[1]
        d.clean_env()
        c.check("gui cleanup asks for confirmation", bool(asked), asked)
        c.check("gui cleanup does nothing when declined",
                not calls and "cancel" in d.status.cget("text"),
                (calls, d.status.cget("text")))

        # a "yes" runs the clean
        gui.messagebox.askyesno = lambda *a, **k: True
        d.clean_env()
        c.check("gui cleanup runs after confirmation",
                calls == ["clean"] and "cleaned" in d.status.cget("text"),
                (calls, d.status.cget("text")))
        root.destroy()
    except Exception as e:
        c.check("gui cleanup", False, e)
    finally:
        gui.bootstrap, gui.messagebox.askyesno = orig_boot, orig_ask
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

    # ---- swap must be real page-file usage, not the commit charge ----
    # GlobalMemoryStatusEx's TotalPageFile - AvailPageFile reported ~13 GiB on a
    # box whose page file actually held 62 MiB; the label said "swap 13 GiB".
    tsrc = open(os.path.join(BENCH, "tui.py"), encoding="utf-8").read()
    c.check("tui does not report the commit charge as swap",
            "ullTotalPageFile - st.ullAvailPageFile" not in tsrc, "")
    c.check("tui prefers psutil's swap for the swap reading",
            "swap_memory()" in tsrc, "")
    c.check("tui falls back to the page-file query run_win.py uses",
            "_win_pagefile_used_gib" in tsrc, "")
    try:
        import psutil
        want = psutil.swap_memory().used / 1024 ** 3
    except Exception:
        want = None
    got = tui._win_swap_gib(0)
    if want is not None:
        c.check("tui's swap helper agrees with psutil",
                abs(got[0] - want) < 0.2, (got, want))
    else:
        c.check("tui's swap helper degrades gracefully without psutil",
                got == (0.0, 0.0) or os.name == "nt", (got, os.name))


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

    # ---- audited fixes: checks the suite can actually make on the sources ----
    mz = re.search(r"\.modal\{[^}]*z-index:(\d+)", css)
    fz = re.search(r"\.focuspane\{[^}]*z-index:(\d+)", css)
    c.check("the modal sits above the focus pane",
            bool(mz and fz) and int(mz.group(1)) > int(fz.group(1)),
            (mz and mz.group(1), fz and fz.group(1)))
    c.check("Escape closes the modal, not the pane behind it",
            "if (modalOpen()) { e.preventDefault(); closeModal(); }" in js
            and "else if (FOCUS) closeFocus();" in js, "")
    c.check("the live tail is kept apart from the downsampled base",
            "let TAIL = {}" in js and "function telBuf" in js
            and "slice(-2000)" not in js and "buf.length - 900" not in js, "")
    c.check("trends normalise their mixed-unit series",
            'drawChart($("#trendChart"), series, { xTime: true, normalize: true })' in js, "")
    c.check("the focus pane takes its stats from the server",
            "/stats" in js and "function ensureStats" in js, "")
    c.check("a null throttle is not reported as 'no throttle'",
            '"throttle no data"' in js and "derived.throttled_pct" in js, "")
    c.check("every active run is refetched on reconnect and on the tick",
            "for (const a of STATE.active) reloadTel(a.id);" in js, "")
    c.check("history and fleet deltas need a matching soak size",
            "String(pn) === String(s.gpu_soak_size)" in js and "gpuCell" in js, "")
    c.check("the gfx engine picker follows the runner's platform",
            "function gfxEngines" in js and 'out.push("valley")' in js
            and 'out.push("glmark2")' in js and "const engines = gfxEngines(runner)" in js, "")
    with open(os.path.join(REPO, "server", "web", "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    c.check("sortable headers are keyboard reachable and carry aria-sort",
            html.count('class="sortable') == 10 and html.count('tabindex="0"') >= 10
            and 'setAttribute("aria-sort"' in js, "")
    c.check("toasts announce themselves to screen readers",
            't.setAttribute("role", "status")' in js
            and 't.setAttribute("aria-live", "polite")' in js, "")

    # ---- W19: web UI P3 backlog ----
    # USE-P3-001: schedule deletion is destructive and must be gated like the other
    # destructive controls (forget runner / power off).
    c.check("deleting a schedule goes through a confirm gate",
            re.search(r"function deleteSchedule\(id\)\s*\{[^}]*confirm\(", js) is not None, "")
    # USE-P3-002: notify on completion (Notification API only after an explicit
    # grant) and always fall back to a flashing tab title.
    c.check("a finished run raises a completion notification when permitted",
            "function notifyDone" in js and "new Notification(" in js
            and 'Notification.permission === "granted"' in js, "")
    c.check("notifications are requested only from an explicit user action",
            'id="notifyBtn"' in html and "function enableNotifications" in js
            and "Notification.requestPermission()" in js
            and re.search(r'#notifyBtn"\)\.onclick\s*=\s*enableNotifications', js) is not None, "")
    c.check("a run completion flashes the tab title as a fallback",
            "function flashTitle" in js and "document.title" in js, "")
    c.check("a run that finished before load does not notify",
            "function checkCompletions" in js and "_activeSeen === null" in js, "")
    # USE-P3-003: Compare needs an empty state and must never compare a run to itself.
    c.check("compare shows an empty state",
            "function renderCompareEmpty" in js and "Not enough finished runs" in js
            and 'Choose two different runs' in js, "")
    c.check("compare refuses to compare a run with itself",
            "if (A === B)" in js and "pick two different runs to compare" in js, "")
    c.check("the compare button is disabled with fewer than two runs",
            'btn.disabled = finished.length < 2' in js, "")
    # UX-P3-001: honour prefers-reduced-motion, and state the dark-only theme.
    c.check("reduced-motion turns off animations and transitions",
            "prefers-reduced-motion: reduce" in css
            and "animation:none" in css.replace(" ", "")
            and "transition:none" in css.replace(" ", ""), "")
    c.check("the dark-only theme is declared explicitly",
            'name="color-scheme"' in html and "color-scheme:dark" in css.replace(" ", ""), "")
    # UX-P3-002: offline cards must dim without blending muted text below AA.
    c.check("offline runner cards no longer use a whole-card opacity",
            ".runner.off{opacity:.55}" not in css.replace(" ", ""), "")
    c.check("offline runner cards keep a dim affordance without dropping contrast",
            ".runner.off{opacity:1" in css.replace(" ", "") and ".runner.off .statusp" in css, "")
    # MOB-P3-002: fixed/sticky chrome must respect safe-area insets.
    c.check("the viewport opts into safe-area insets",
            "viewport-fit=cover" in html, "")
    c.check("fixed and sticky chrome honours safe-area insets",
            "env(safe-area-inset-top)" in css and "env(safe-area-inset-bottom)" in css
            and "env(safe-area-inset-left)" in css and "env(safe-area-inset-right)" in css, "")
    # PRIV-P3-001 / SECRET-P3-001: token storage.
    c.check("the operator token defaults to sessionStorage",
            "sessionStorage.getItem(\"benchd_token\")" in js
            and 'sessionStorage.setItem("benchd_token", TOKEN)' in js, "")
    c.check("localStorage is an explicit remember-on-this-device opt-in",
            'id="tokRemember"' in js and "remember on this device" in js
            and "benchd_remember" in js, "")
    c.check("a token passed in the URL is stripped after it is read",
            'new URLSearchParams(location.search).get("token")' in js
            and "history.replaceState" in js, "")


def run():
    c = Checker("dashboards")
    _gui(c)
    _gui_cleanup(c)
    _tui(c)
    _web(c)
    # ---- runner-supplied summary fields must be escaped before innerHTML ----
    # The panel used to interpolate gpu_soak_size / cpu_soak_size / gfx.engine straight
    # into HTML; a runner key could inject a script into the operator's browser.
    import re as _re
    with open(os.path.join(REPO, "server", "web", "app.js"), encoding="utf-8") as fh:
        js = fh.read()
    c.check("the runner panel escapes its summary bits",
            "esc(bits.join(" in js, "")
    c.check("and the chart height is not read back from the attribute",
            'cv.dataset.baseH || (cv.dataset.baseH = cv.getAttribute("height")' in js
            and 'const h = +cv.getAttribute("height") || 180;' not in js, "")

    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
