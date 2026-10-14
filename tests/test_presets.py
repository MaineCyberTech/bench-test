"""Preset parity: the GUI, TUI and web app must offer the same presets, with the
same groups and durations, and switching must be deterministic."""
import importlib.util
import os
import re
import sys

from harness import Checker, BENCH, REPO
import benchreport as _registry

# The canonical group list is the benchreport registry (EVOL-P2-001): the preset
# definitions are checked against it instead of a seventh hardcoded copy. The
# registry -> dashboard/web parity itself is enforced by tests/test_registry.py.
GROUPS = list(_registry.GROUPS)
DEF = {"gpu": 600, "combined": 300, "fullsoak": 300, "gfx": 60}
CANON = {
    "Quick check (short soaks)": (set(GROUPS) - {"llm", "fan"},
                                  {"gpu": 60, "combined": 60, "fullsoak": 60, "gfx": 30}),
    "Stabilized full soak (15 min)": (set(GROUPS) - {"llm"},
                                      {"gpu": 180, "combined": 45, "fullsoak": 90, "gfx": 30}),
    "System info + 60s soak (all)": ({"sys", "fullsoak"}, {**DEF, "fullsoak": 60}),
    "Full run (defaults)": (set(GROUPS), dict(DEF)),
    "GPU compute": ({"gpu", "sys"}, {**DEF, "gpu": 60}),
    "CPU + RAM + disk": ({"cpu", "ram", "disk", "sys"}, dict(DEF)),
    "Storage + network": ({"disk", "net", "sys"}, dict(DEF)),
    "Video / graphics": ({"nvenc", "gfx", "sys"}, dict(DEF)),
    "Graphics only (FurMark/Valley)": ({"gfx", "sys"}, {**DEF, "gfx": 30}),
    "LLM inference (Ollama)": ({"llm", "sys"}, dict(DEF)),
    "Fan response": ({"fan", "sys"}, dict(DEF)),
    "Soak only (long)": ({"gpu", "fullsoak", "sys"}, {**DEF, "gpu": 1800, "fullsoak": 1800}),
    "Full-system soak (long)": ({"fullsoak", "sys"}, {**DEF, "fullsoak": 1800}),
    "Burn-in (1 h)": ({"gpu", "cpu", "ram", "disk", "combined", "sys"},
                      {"gpu": 1200, "combined": 1200, "fullsoak": 300, "gfx": 60}),
}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run():
    c = Checker("presets")
    gui = _load("gui", os.path.join(BENCH, "gui.py"))
    tui = _load("tui", os.path.join(BENCH, "tui.py"))

    g = {k: v for k, v in gui.PRESETS.items() if v}
    t = dict(tui.PRESETS)
    c.check("gui preset count", len(g) == len(CANON), (len(g), len(CANON)))
    c.check("tui preset count", len(t) == len(CANON), (len(t), len(CANON)))

    for name, (want_g, want_d) in CANON.items():
        p = g.get(name)
        if p:
            got_g = {k for k, v in p["groups"].items() if v}
            got_d = {k: int(v) for k, v in p["spin"].items()}
            c.check("gui %s" % name, got_g == want_g and got_d == want_d, (sorted(got_g), got_d))
            # all keys present -> switching is deterministic
            c.check("gui %s keys" % name, set(p["groups"]) == set(GROUPS) and set(p["spin"]) == set(DEF), "")
        else:
            c.check("gui has %s" % name, False, "missing")
        spec = t.get(name)
        if spec:
            c.check("tui %s" % name, set(spec["groups"]) == want_g
                    and {k: int(v) for k, v in spec["dur"].items()} == want_d, spec)
        else:
            c.check("tui has %s" % name, False, "missing")

    # web (parse app.js line-by-line)
    js = open(os.path.join(REPO, "server", "web", "app.js"), encoding="utf-8").read()
    defspin = {k: int(v) for k, v in
               re.findall(r"(\w+):\s*(\d+)", re.search(r"const DEF_SPIN = \{([^}]*)\}", js).group(1))}
    block = re.search(r"const PRESETS = \{(.*?)\n\};", js, re.S).group(1)
    web = {}
    for line in block.splitlines():
        m = re.match(r'\s*"([^"]+)":\s*\{(.*)\},?\s*$', line)
        if not m:
            continue
        name, body = m.group(1), m.group(2)
        gm = re.search(r"gset\((.*?)\)", body)
        groups = set(GROUPS) if gm and "ALL_GROUPS" in gm.group(1) else (
            set(re.findall(r'"(\w+)"', gm.group(1))) if gm else set())
        sm = re.search(r"spin:\s*\{(.*?)\}", body)
        spin = dict(defspin)
        spin.update({k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", sm.group(1))})
        web[name] = (groups, spin)
    c.check("web preset count", len(web) == len(CANON), (len(web), len(CANON)))
    for name, (want_g, want_d) in CANON.items():
        c.check("web %s" % name, web.get(name) == (want_g, want_d), web.get(name))

    # the runners' own group lists must match too — a missing entry silently
    # drops that group's phases (benchreport) or mis-advertises capabilities
    rep = _load("benchreport", os.path.join(BENCH, "benchreport.py"))
    ag = _load("agent", os.path.join(BENCH, "agent.py"))
    c.check("benchreport.GROUPS canonical", rep.GROUPS == GROUPS, rep.GROUPS)
    c.check("agent.DEFAULT_GROUPS canonical", ag.DEFAULT_GROUPS == GROUPS, ag.DEFAULT_GROUPS)
    c.check("tui.GROUPS canonical", tui.GROUPS == GROUPS, tui.GROUPS)
    c.check("gui group keys canonical", {k for k, _ in gui.GROUPS} == set(GROUPS), gui.GROUPS)
    # every preset must actually do something beyond `sys`, and be distinct
    import benchreport as rep

    def want_groups(spec):
        g = spec["groups"]
        if isinstance(g, dict):
            return [k for k, v in g.items() if v]
        return [k for k in GROUPS if k in g]

    seen = {}
    for name, p in dict(tui.PRESETS).items():
        want = want_groups(p)
        c.check("preset %s does something beyond sys" % name,
                any(g != "sys" for g in want), want)
        key = (tuple(sorted(want)), tuple(sorted((k, int(v)) for k, v in p["dur"].items())))
        c.check("preset %s is distinct" % name, key not in seen,
                "%s == %s" % (name, seen.get(key)))
        seen[key] = name
    for name, p in {k: v for k, v in gui.PRESETS.items() if v}.items():
        c.check("gui preset %s does something beyond sys" % name,
                any(g != "sys" for g in want_groups(p)), want_groups(p))
    c.check("supported_groups matches the platform",
            ("fan" not in rep.supported_groups()) if os.name == "nt"
            else ("llm" not in rep.supported_groups()), rep.supported_groups())
    # a toggle for a group the platform cannot run is a silent no-op
    want_ui = [g for g in GROUPS if g in rep.supported_groups()]
    c.check("gui group toggles are capability-filtered",
            [k for k, _ in gui.UI_GROUPS] == want_ui, gui.UI_GROUPS)
    c.check("tui group toggles are capability-filtered",
            tui.UI_GROUPS == want_ui, tui.UI_GROUPS)
    c.check("tui group hotkeys are unique and reach every offered toggle",
            len(tui._GROUP_KEYS) == len(tui.UI_GROUPS)
            and len(set(tui._GROUP_KEYS)) == len(tui._GROUP_KEYS), tui._GROUP_KEYS)

    # ---- the preset trim: disk size must actually reach the runner ----
    # The randread/randwrite/randmix phases are size-bound, not seconds-bound, so
    # without disk_size_mb the runner's 2048MB default applies and cost 226s of the
    # desktop's 1368s stabilized run. 512MB does the same job in ~57s.
    c.check("extra_runner_args passes --disk-size-mb",
            rep.extra_runner_args({"disk_size_mb": 512}) == ["--disk-size-mb", "512"],
            rep.extra_runner_args({"disk_size_mb": 512}))
    c.check("disk_size_mb is absent from the args when unset",
            rep.extra_runner_args({}) == [], rep.extra_runner_args({}))
    gtune = {k: ((v or {}).get("tune") or {}) for k, v in gui.PRESETS.items() if v}
    ttune = {n: (s.get("tune") or {}) for n, s in tui.PRESETS}
    for name in ("Quick check (short soaks)", "Stabilized full soak (15 min)"):
        for label, tunes in (("gui", gtune), ("tui", ttune)):
            ds = tunes.get(name, {}).get("disk_size_mb")
            c.check("%s %s trims disk_size_mb below the runner default" % (label, name),
                    isinstance(ds, int) and ds < 2048, ds)
        line = next((ln for ln in block.splitlines() if '"%s":' % name in ln), "")
        m = re.search(r"disk_size_mb:\s*(\d+)", line)
        c.check("web %s trims disk_size_mb below the runner default" % name,
                bool(m) and int(m.group(1)) < 2048, m and m.group(1))
    # the three dashboard definitions must not drift apart
    for name in CANON:
        c.check("gui/tui tune parity for %s" % name,
                gtune.get(name, {}) == ttune.get(name, {}),
                (gtune.get(name), ttune.get(name)))

    # ---- the GUI/TUI derive from the canonical table, not a private copy ----
    canon = rep.presets()
    c.check("gui PRESETS are the canonical table",
            {k: v for k, v in gui.PRESETS.items() if v} == canon,
            (sorted(gui.PRESETS), sorted(canon)))
    want_tui = {name: {"groups": [k for k, v in spec["groups"].items() if v],
                       "dur": {k: str(v) for k, v in spec["spin"].items()},
                       "tune": dict(spec.get("tune") or {})}
                for name, spec in canon.items()}
    c.check("tui PRESETS are the canonical table (reshaped)",
            dict(tui.PRESETS) == want_tui, dict(tui.PRESETS))

    # the web table is a copy; pin its explicit tune overrides too (TUNE_DEFAULT
    # only fills the keys a preset does not name)
    web_tune = {}
    for line in block.splitlines():
        m = re.match(r'\s*"([^"]+)":\s*\{(.*)\},?\s*$', line)
        if not m:
            continue
        tm = re.search(r"tune:\s*\{(.*?)\}", m.group(2))
        body = tm.group(1).replace("...TUNE_DEFAULT", "") if tm else ""
        web_tune[m.group(1)] = {k: (int(v) if v.isdigit() else v.strip('"'))
                                for k, v in re.findall(r'(\w+):\s*(\d+|"[^"]*")', body)}
    for name, spec in canon.items():
        want = dict(spec.get("tune") or {})
        c.check("web tune parity for %s" % name, web_tune.get(name, {}) == want,
                (web_tune.get(name), want))

    # ---- a preset NAME must carry its tune to the runner (agent expansion) ----
    # The control plane can send {"preset": "Quick check (short soaks)"} with no
    # explicit fields; the agent used to apply only explicit fields, so the name
    # ran the runner's long defaults (a "Quick check" ran 600s soaks) and the
    # same preset measured N=4096 on one machine and N=10240 on another.
    def preset_args(name):
        spec = canon[name]
        args = ["--skip-" + g for g in GROUPS if not spec["groups"][g]]
        spin = spec["spin"]
        args += ["--gpu-soak", str(spin["gpu"]), "--combined-seconds", str(spin["combined"]),
                 "--fullsoak-seconds", str(spin["fullsoak"]), "--gfx-seconds", str(spin["gfx"])]
        args += rep.extra_runner_args(spec["tune"])
        return args

    for name in ("Quick check (short soaks)", "Stabilized full soak (15 min)", "Burn-in (1 h)"):
        c.check("agent expands preset %r into its groups + durations + tune" % name,
                ag.build_runner_args({"preset": name}) == preset_args(name),
                ag.build_runner_args({"preset": name}))
    payload = {"preset": "Quick check (short soaks)"}
    expanded = ag.expand_preset(payload)
    c.check("expansion fills the profile the run records",
            expanded.get("gpu_soak") == 60 and expanded.get("cpu_soak") == 60
            and expanded.get("gpu_size") == 4096
            and set(expanded.get("skip") or []) == {"llm", "fan"}, expanded)
    c.check("expansion does not mutate the caller's payload",
            payload == {"preset": "Quick check (short soaks)"}, payload)
    # explicit payload fields still win over the preset
    over = ag.build_runner_args({"preset": "Quick check (short soaks)",
                                 "gpu_soak": 99, "cpu_soak": 5})
    c.check("an explicit duration overrides the preset",
            over[over.index("--gpu-soak") + 1] == "99"
            and over[over.index("--cpu-soak") + 1] == "5", over)
    c.check("the preset's other tune still applies around the override",
            over[over.index("--disk-size-mb") + 1] == "256"
            and over[over.index("--gpu-size") + 1] == "4096", over)
    c.check("the preset still plateau-soaks with no override",
            "--soak-until-stable" in ag.build_runner_args(
                {"preset": "Stabilized full soak (15 min)"}), "")
    c.check("an explicit stable:0 turns the preset's plateau soak off",
            "--soak-until-stable" not in ag.build_runner_args(
                {"preset": "Stabilized full soak (15 min)", "stable": 0}), "")
    gmap = ag.build_runner_args({"preset": "Quick check (short soaks)", "groups": {"llm": 1}})
    c.check("an explicit group turns a preset group back on",
            "--skip-llm" not in gmap and "--skip-fan" in gmap, gmap)
    c.check("an unknown preset name is left alone (never guessed)",
            ag.build_runner_args({"preset": "no such preset"}) == []
            and ag.expand_preset({"preset": "no such preset"})
            == {"preset": "no such preset"}, "")
    c.check("no preset name -> no expansion",
            ag.build_runner_args({}) == [] and ag.expand_preset({}) == {}, "")

    # ---- Burn-in (1 h): a stability hunt, not a score run ----
    burn = canon["Burn-in (1 h)"]
    burn_tune = burn.get("tune") or {}
    c.check("Burn-in runs the mixed load (gpu + cpu + ram + disk + combined)",
            {"gpu", "cpu", "ram", "disk", "combined"}
            <= {k for k, v in burn["groups"].items() if v},
            sorted(k for k, v in burn["groups"].items() if v))
    c.check("Burn-in includes sys for the WHEA/TDR/ECC/disk verdicts + event-log window",
            burn["groups"].get("sys") == 1, burn["groups"])
    c.check("Burn-in has long fixed soaks and no plateau flag",
            burn["spin"]["gpu"] >= 600 and burn["spin"]["combined"] >= 600
            and not burn_tune.get("stable") and not burn_tune.get("stable_hold")
            and "--soak-until-stable" not in ag.build_runner_args({"preset": "Burn-in (1 h)"}),
            (burn["spin"], burn_tune))

    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
