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

    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
