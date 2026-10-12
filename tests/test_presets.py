"""Preset parity: the GUI, TUI and web app must offer the same presets, with the
same groups and durations, and switching must be deterministic."""
import importlib.util
import os
import re
import sys

from harness import Checker, BENCH, REPO

GROUPS = ["gpu", "cpu", "ram", "disk", "combined", "fullsoak", "net", "nvenc", "llm", "fan", "sys", "gfx"]
DEF = {"gpu": 600, "combined": 300, "fullsoak": 300, "gfx": 60}
CANON = {
    "Quick check (short soaks)": (set(GROUPS) - {"llm", "fan"},
                                  {"gpu": 60, "combined": 60, "fullsoak": 60, "gfx": 30}),
    "System info + 30s soak (all)": ({"sys", "fullsoak"}, {**DEF, "fullsoak": 30}),
    "Full run (defaults)": (set(GROUPS), dict(DEF)),
    "GPU compute": ({"gpu", "sys"}, {**DEF, "gpu": 60}),
    "CPU + RAM + disk": ({"cpu", "ram", "disk", "sys"}, dict(DEF)),
    "Storage + network": ({"disk", "net", "sys"}, dict(DEF)),
    "Video / graphics": ({"nvenc", "gfx", "sys"}, dict(DEF)),
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
    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
