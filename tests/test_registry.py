"""Group/phase registry (EVOL-P2-001).

`bench/benchreport.py` is the one Python source of truth for the benchmark
groups/phases. This module proves it:

  * the registry is well-formed and `GROUPS` / `GROUP_LABELS` derive from it;
  * `supported_groups()` is registry-driven and platform-aware;
  * the Python consumers (agent/tui/gui) carry no group literal of their own and
    their live lists equal the registry (grep-level + behavioural);
  * the web copy (server/web/app.js, which cannot import Python) stays pinned to
    the registry;
  * a synthetic registry entry flows through `supported_groups()` and the
    dashboard presets without editing any consumer.

Stdlib only.
"""
import importlib.util
import os
import re
import sys

from harness import Checker, BENCH, REPO

# The literal the old code repeated in every consumer; a file that still carries
# it is not deriving its groups from the registry.
_KEYS_LITERAL = '"combined", "fullsoak", "net", "nvenc"'
_GUI_LITERAL = '("combined", "Combined GPU+CPU")'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _src(name):
    with open(os.path.join(BENCH, name), encoding="utf-8") as fh:
        return fh.read()


def _expected_platform(entry, nt):
    """What supported_groups() must decide for one registry entry on this OS."""
    if not entry.get("battery", True):
        return False
    p = entry.get("platform", "all")
    if p == "all":
        return True
    return (p == "linux") != nt


def run():
    c = Checker("registry")
    rep = _load("benchreport_registry", os.path.join(BENCH, "benchreport.py"))

    # ---- the registry itself ----
    c.check("registry exposes PHASES", isinstance(rep.PHASES, list) and bool(rep.PHASES), rep.PHASES)
    keys = rep.group_keys()
    c.check("registry keys are unique", len(keys) == len(set(keys)), keys)
    c.check("GROUPS is the registry order", list(rep.GROUPS) == keys, rep.GROUPS)
    for e in rep.PHASES:
        c.check("entry %s carries key/label/platform/battery" % e.get("key"),
                {"key", "label", "platform", "battery"} <= set(e), e)
        c.check("entry %s platform is known" % e.get("key"),
                e.get("platform") in ("all", "linux", "win"), e.get("platform"))
        c.check("entry %s has a non-empty label" % e.get("key"),
                bool(str(e.get("label", "")).strip()), e.get("label"))
    c.check("GROUP_LABELS covers the registry",
            set(rep.GROUP_LABELS) == set(keys), rep.GROUP_LABELS)
    c.check("group_keys honours an explicit registry",
            rep.group_keys([{"key": "a"}, {"key": "b"}]) == ["a", "b"], "")

    # ---- supported_groups() is registry-driven + platform-aware ----
    nt = os.name == "nt"
    sup = rep.supported_groups()
    c.check("supported_groups is a subset of GROUPS", set(sup) <= set(keys), sup)
    for e in rep.PHASES:
        want = _expected_platform(e, nt)
        c.check("supported_groups %s matches the registry" % e["key"],
                (e["key"] in sup) == want, (e["key"], e.get("platform"), want))
    if nt:
        c.check("windows drops the linux-only fan group, keeps llm",
                "fan" not in sup and "llm" in sup, sup)
    else:
        c.check("linux drops the windows-only llm group, keeps fan",
                "llm" not in sup and "fan" in sup, sup)

    # ---- every Python consumer derives from the registry (grep-level) ----
    for name in ("agent.py", "tui.py"):
        src = _src(name)
        c.check("%s does not repeat the group literal" % name, _KEYS_LITERAL not in src, "")
        c.check("%s derives from group_keys()" % name, "group_keys()" in src, "")
    gui_src = _src("gui.py")
    c.check("gui.py does not repeat the group literal", _GUI_LITERAL not in gui_src, "")
    c.check("gui.py derives from group_keys()", "group_keys()" in gui_src, "")
    c.check("gui.py labels come from group_labels()", "group_labels()" in gui_src, "")
    rep_src = _src("benchreport.py")
    c.check("benchreport GROUPS derives from the registry", "GROUPS = group_keys()" in rep_src, "")
    c.check("benchreport supported_groups accepts a registry",
            "def supported_groups(registry=None)" in rep_src, "")

    # the consumers' live lists equal the registry
    ag = _load("agent_registry", os.path.join(BENCH, "agent.py"))
    tui = _load("tui_registry", os.path.join(BENCH, "tui.py"))
    gui = _load("gui_registry", os.path.join(BENCH, "gui.py"))
    c.check("agent.DEFAULT_GROUPS is the registry", list(ag.DEFAULT_GROUPS) == keys, ag.DEFAULT_GROUPS)
    c.check("tui.GROUPS is the registry", list(tui.GROUPS) == keys, tui.GROUPS)
    c.check("gui.GROUPS keys are the registry", [k for k, _ in gui.GROUPS] == keys, gui.GROUPS)
    c.check("gui group labels come from the registry",
            all(lbl == rep.GROUP_LABELS[k] for k, lbl in gui.GROUPS), gui.GROUPS)
    c.check("gui._ALL_G is the registry key set", set(gui._ALL_G) == set(keys), gui._ALL_G)
    c.check("gui 'Full run' preset is the registry key set",
            set(gui.PRESETS["Full run (defaults)"]["groups"]) == set(keys), "")
    c.check("tui 'Full run' preset is the registry key set",
            set(tui.PRESETS[3][1]["groups"]) == set(keys), tui.PRESETS[3][1]["groups"])

    # ---- the web copy (cannot import Python) is pinned to the registry ----
    js = open(os.path.join(REPO, "server", "web", "app.js"), encoding="utf-8").read()
    web = {}
    for const in ("GROUPS", "ALL_GROUPS"):
        m = re.search(r"const %s = \[(.*?)\];" % const, js)
        c.check("app.js defines %s" % const, bool(m), const)
        web[const] = re.findall(r'"(\w+)"', m.group(1)) if m else None
        c.check("app.js %s equals the registry" % const, web[const] == keys, web[const])
    c.check("app.js GROUPS and ALL_GROUPS agree", web["GROUPS"] == web["ALL_GROUPS"],
            (web["GROUPS"], web["ALL_GROUPS"]))

    # ---- a synthetic registry entry flows through supported_groups()/presets ----
    # The consumers read the registry at import, so patch the *shared* module
    # (the one their `from benchreport import ...` resolves to), load fresh
    # copies, and assert a brand-new phase is selectable everywhere with no
    # consumer edits. Restored in `finally` so other tests see the real list.
    import benchreport as shared
    synth = {"key": "zzztest", "label": "Synthetic phase", "platform": "all", "battery": True}
    dash = {"key": "zzzdash", "label": "Dashboard only", "platform": "all", "battery": False}
    real = list(shared.PHASES)
    try:
        shared.PHASES = real + [synth, dash]
        reg = shared.PHASES
        c.check("synthetic battery phase flows through supported_groups",
                "zzztest" in shared.supported_groups(reg), shared.supported_groups(reg))
        c.check("dashboard-only group is not advertised as a capability",
                "zzzdash" not in shared.supported_groups(reg), shared.supported_groups(reg))
        c.check("dashboard-only group is still a known group key",
                "zzzdash" in shared.group_keys(reg), shared.group_keys(reg))
        ag2 = _load("agent_synth", os.path.join(BENCH, "agent.py"))
        tui2 = _load("tui_synth", os.path.join(BENCH, "tui.py"))
        gui2 = _load("gui_synth", os.path.join(BENCH, "gui.py"))
        c.check("agent picks up a synthetic registry entry",
                "zzztest" in ag2.DEFAULT_GROUPS, ag2.DEFAULT_GROUPS)
        c.check("tui picks up a synthetic registry entry", "zzztest" in tui2.GROUPS, tui2.GROUPS)
        c.check("gui picks up a synthetic registry entry",
                "zzztest" in [k for k, _ in gui2.GROUPS], gui2.GROUPS)
        c.check("gui label for a synthetic entry comes from the registry",
                dict(gui2.GROUPS).get("zzztest") == "Synthetic phase", gui2.GROUPS)
        c.check("a synthetic entry flows into the gui 'Full run' preset",
                gui2.PRESETS["Full run (defaults)"]["groups"].get("zzztest") == 1,
                gui2.PRESETS["Full run (defaults)"]["groups"])
        c.check("a synthetic entry flows into the tui 'Full run' preset",
                "zzztest" in tui2.PRESETS[3][1]["groups"], tui2.PRESETS[3][1]["groups"])
    finally:
        shared.PHASES = real

    return c.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
