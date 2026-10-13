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

    # ---- W22b: regression tests for the W19 responsive / a11y additions ----
    # MOB-P3-003 / UX-P3-003: the main tab strip must stay usable on a phone and by
    # keyboard. Assert the wiring end to end (handler -> element -> behaviour),
    # not just that a token appears somewhere in the file.
    c.check("the tab strip scrolls instead of clipping on phones",
            ".tabs{flex-wrap:nowrap;overflow-x:auto" in css.replace(" ", ""), "")
    _kbd = re.search(r'document\.addEventListener\("keydown", \(e\) => \{\s*'
                     r'const tab = e\.target\.closest.*?e\.key !== "Enter"', js, re.S)
    _kbd = _kbd.group(0) if _kbd else ""
    c.check("the roving-tabindex keydown handler is wired to the real tabs",
            '#tabs [role="tab"]' in _kbd and 'tabs[j].focus()' in _kbd
            and 'tabs[j].click()' in _kbd and 'e.preventDefault()' in _kbd, "")
    c.check("the tabs it drives exist with role and roving tabindex",
            'id="tabs"' in html and 'role="tablist"' in html
            and html.count('role="tab"') >= 6 and 'tabindex="-1"' in html, "")
    # the destructive schedule delete must stay behind its confirm gate: prove the
    # DELETE is unreachable when confirm() returns false (control flow, not text).
    _del = re.search(r"async function deleteSchedule\(id\) \{(.*?)\n\}", js, re.S)
    _del = _del.group(1) if _del else ""
    c.check("the schedule delete gate blocks the DELETE until confirmed",
            re.search(r'if \(!confirm\(.*?\)\) return;.*?api\("/schedules/', _del, re.S)
            is not None, "")
    c.check("schedule rows carry the hook the delete handler consumes",
            'data-sched-del=' in js and "t.dataset.schedDel) return deleteSchedule(" in js, "")

    # ---- W22b: PERF-P3-002 / SEARCH-P3-001 ----
    # History and Fleet cap the rows they fetch; they must say so and use the total
    # the API already returns instead of silently truncating.
    c.check("history shows the loaded count against the API total",
            "showing ${rows.length} of ${total} run" in js
            and "const total = typeof d.total" in js, "")
    c.check("history flags when its page was capped",
            "const loaded = d.runs.length" in js and "newest ${loaded} loaded" in js, "")
    c.check("fleet shows the loaded count against the API total",
            "showing ${runs.length} of ${total} runs" in js
            and "total = typeof d.total" in js, "")
    c.check("fleet flags when its page was capped",
            "newest ${runs.length} loaded" in js, "")
    # the runs endpoint has no search param, so the box must be labelled as a page
    # filter and must not be sent to the server as a query.
    c.check("history search is labelled as a page filter",
            'id="histSearch"' in html and "filter this page" in html
            and "not the whole history" in html, "")
    c.check("history search is not passed to the API (no query param)",
            '"/runs?limit=" + HIST_LIMIT' in js and "&q=" not in js and "?q=" not in js, "")
    # Behavioural: the total the History/Fleet affordances print really comes from
    # the API. Seed three runs and confirm the list returns a total the page limit
    # does not hide (the UI reads exactly this field).
    try:
        from harness import Server
        srv = Server()
        try:
            cl = srv.client()
            for i in range(3):
                cl.call("POST", "/api/v1/runs",
                        {"runner_id": "w22b-runner", "host": "w22b-host", "label": "r%d" % i})
            _, page = cl.call("GET", "/api/v1/runs?limit=2&host=w22b-host")
            c.check("the runs API returns the total the UI shows",
                    page.get("total") == 3 and len(page["runs"]) == 2, page)
        finally:
            srv.stop()
    except Exception as e:
        c.check("the runs API returns the total the UI shows", False, e)

    # ---- W24b: web P2 backlog ----
    # USE-P2-001: a command POST only proves it was enqueued; the runner's ack is
    # the real outcome. The UI must track each issued command and surface
    # queued -> acked / failed / expired (with the error), never claim success on
    # enqueue. The ack arrives on the SSE `command` event (the GET .../commands
    # endpoint only returns still-queued rows), so that handler must parse it.
    c.check("issued commands are tracked by the id the POST returns",
            "function trackCommand" in js and "trackCommand(res, runnerId" in js
            and "res.status || \"queued\"" in js, "")
    c.check("the SSE command event is parsed, not just used as a refresh nudge",
            re.search(r'es\.addEventListener\("command",\s*\(e\)\s*=>', js) is not None
            and "onCommandEvent(JSON.parse(e.data))" in js
            and "function onCommandEvent" in js, "")
    c.check("a command outcome maps queued -> acked / failed / expired",
            "function cmdStatus" in js
            and 'return "acked"' in js and '"expired"' in js and '"failed"' in js
            and 's === "failed" || s === "error"' in js, "")
    c.check("a dropped command (server reap) reads as expired",
            "/no ack/i.test(c.error)" in js, "")
    c.check("the command's error is carried through to the UI",
            "d.result.error" in js and "c.error" in js and "function renderCommands" in js
            and 'id="commands"' in html and "renderCommands();" in js, "")
    c.check("a failed command is never shown with the success styling",
            'const pill = st === "acked" ? "done"' in js
            and '(st === "failed" || st === "expired") ? "failed"' in js, "")

    # USE-P2-004: benchd's _issue_command accepts exec/shell for an idle runner
    # (no live-run gate), so the focus-view terminal must stay available after a
    # run ends instead of being hidden behind `live`.
    c.check("the focus console offers the terminal for an idle runner too",
            "USE-P2-004" in js
            and '${live ? `<form class="term" data-exec=' not in js
            and '<form class="term" data-exec="${esc(r.runner_id)}">' in js, "")

    # USE-P2-002: the start form must validate required + numeric + > 0 client-side
    # and disable submit with an inline error until it is valid.
    c.check("the start form validates durations and sizes (> 0, numeric, required)",
            "function validateStartForm" in js and "function startFormErrors" in js
            and "must be greater than 0" in js and "is required and must be a number" in js, "")
    c.check("submit is disabled and an inline error shown until the form is valid",
            'id="mErr"' in js and "go.disabled = errs.length > 0" in js
            and "if (!validateStartForm()) return;" in js, "")
    c.check("every start-form field re-validates as it is edited",
            'addEventListener("input", validateStartForm)' in js
            and 'addEventListener("change", validateStartForm)' in js, "")

    # Behavioural: the commands API really accepts an exec for a runner with no
    # live run, and the ack endpoint records the outcome the UI consumes.
    try:
        from harness import Server
        srv = Server()
        try:
            cl = srv.client()
            st, q = cl.call("POST", "/api/v1/runners/w24b-runner/commands",
                            {"kind": "exec", "payload": {"cmd": "true"}})
            c.check("the commands API accepts an exec for an idle runner (no live run)",
                    st == 200 and q.get("status") == "queued" and q.get("id"), (st, q))
            st2, a = cl.call("POST", "/api/v1/runners/w24b-runner/commands/%s/ack" % q["id"],
                             {"status": "failed", "result": {"error": "boom"}})
            c.check("the ack endpoint records the outcome the UI surfaces",
                    st2 == 200 and a.get("ok") is True, (st2, a))
        finally:
            srv.stop()
    except Exception as e:
        c.check("the commands API accepts an exec for an idle runner (no live run)", False, e)


def run():
    c = Checker("dashboards")
    _gui(c)
    _tui(c)
    _web(c)
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
