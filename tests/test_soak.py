"""Stabilized soak: the GPU soak runs until the temperature plateaus, then holds
for a while, and the presets/control plane can reach the runner's tuning knobs.

Two things are easy to regress and hard to notice: the decision logic's sense
(keep soaking vs stop, and the hold being *after* the plateau), and the
payload -> argv mapping. Both are covered here.
"""
import importlib.util
import os
import time
import types

from harness import Checker, BENCH, REPO, skip


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _args(seconds, until_stable, hold=60.0):
    return types.SimpleNamespace(seconds=seconds, until_stable=until_stable, stable_hold=hold)


def run():
    c = Checker("soak")

    # ---- payload -> argv (benchreport) ----
    rep = _load("benchreport", os.path.join(BENCH, "benchreport.py"))
    got = rep.extra_runner_args({"cpu_soak": 45, "ram_soak": 30, "disk_seconds": 30,
                                 "gpu_size": 4096, "cpu_size": 2048, "ram_size_mb": 512,
                                 "passes": 1, "steady_gb": 2, "stable": 1, "stable_hold": 60,
                                 "gfx_engine": "furmark"})
    want = ["--cpu-soak", "45", "--ram-soak", "30", "--disk-seconds", "30",
            "--gpu-size", "4096", "--cpu-size", "2048", "--ram-size-mb", "512",
            "--passes", "1", "--steady-gb", "2", "--stable-hold", "60",
            "--gfx-engine", "furmark", "--soak-until-stable"]
    c.check("extra_runner_args maps every knob", got == want, got)
    c.check("extra_runner_args empty without tune", rep.extra_runner_args({}) == [], "")
    c.check("extra_runner_args ignores absent keys",
            rep.extra_runner_args({"gpu_soak": 60, "stable": 0}) == [], "")
    c.check("stable flag only when set",
            rep.extra_runner_args({"stable": 1}) == ["--soak-until-stable"]
            and rep.extra_runner_args({"stable": 0}) == [], "")
    c.check("stable_hold implies until-stable",
            rep.extra_runner_args({"stable_hold": 120})
            == ["--stable-hold", "120", "--soak-until-stable"], "")
    c.check("tune_summary shows the hold",
            rep.tune_summary({"stable": 1, "stable_hold": 90}) == "until stable +90s",
            rep.tune_summary({"stable": 1, "stable_hold": 90}))
    c.check("tune_summary shows the gfx engine",
            rep.tune_summary({"gfx_engine": "furmark"}) == "gfx furmark",
            rep.tune_summary({"gfx_engine": "furmark"}))

    # ---- both runners must accept the flags (parity) ----
    sh = open(os.path.join(BENCH, "Run-LinuxBench.sh"), encoding="utf-8").read()
    win = open(os.path.join(BENCH, "run_win.py"), encoding="utf-8").read()
    c.check("linux runner accepts --soak-until-stable",
            "--soak-until-stable)" in sh and "--until-stable" in sh, "")
    c.check("linux runner accepts --stable-hold", "--stable-hold" in sh, "")
    c.check("windows runner accepts --soak-until-stable",
            '"--soak-until-stable"' in win and "-SoakUntilStable" in win, "")
    c.check("windows runner accepts --stable-hold",
            '"--stable-hold"' in win and "-StableHold" in win, "")
    c.check("linux runner accepts --gfx-engine", "--gfx-engine" in sh, "")
    c.check("windows runner accepts --gfx-engine",
            '"--gfx-engine"' in win and "-GfxEngine" in win, "")
    c.check("windows battery has a FurMark path",
            "Invoke-FurMarkBench.ps1" in open(os.path.join(BENCH, "Run-WorkstationBench.ps1"),
                                              encoding="utf-8").read(), "")
    c.check("windows battery forwards the flag",
            "$SoakUntilStable" in open(os.path.join(BENCH, "Run-WorkstationBench.ps1"),
                                       encoding="utf-8").read(), "")

    # ---- the plateau test itself (gpu_bench) ----
    try:
        gb = _load("gpu_bench", os.path.join(BENCH, "gpu_bench.py"))
    except Exception as e:
        skip("plateau logic", e)
        return c.fails

    flat = [[70.0, 40, 250, 2600] for _ in range(8)]
    climb = [[60.0 + i, 40, 250, 2600] for i in range(8)]
    c.check("plateau: flat temps are stable", gb._temp_stable(flat) is True, "")
    c.check("plateau: climbing temps are not stable", gb._temp_stable(climb) is False, "")
    c.check("plateau: needs a full window", gb._temp_stable(flat[:7]) is False, "")

    t0 = time.time() - 50
    keep, ss = gb._soak_decision(t0, _args(300, True), flat, None)
    c.check("soak: a plateau starts the hold, it does not stop the soak",
            keep is True and ss is not None, (keep, ss))
    c.check("soak: keeps going through the hold",
            gb._soak_decision(t0, _args(300, True), flat, ss) == (True, ss), "")
    c.check("soak: stops once the hold has elapsed",
            gb._soak_decision(t0, _args(300, True), flat, ss - 60)[0] is False, "")
    c.check("soak: a longer hold keeps it running",
            gb._soak_decision(t0, _args(300, True, 300), flat, ss - 60)[0] is True, "")
    c.check("soak: moving temps reset the hold",
            gb._soak_decision(t0, _args(300, True), climb, ss) == (True, None), "")
    c.check("soak: honours the ceiling",
            gb._soak_decision(time.time() - 400, _args(300, True), flat, None)[0] is False, "")
    c.check("soak: no early stop when not requested",
            gb._soak_decision(t0, _args(300, False), flat, None) == (True, None), "")
    c.check("soak: never stops before the minimum",
            gb._soak_decision(time.time() - 10, _args(300, True), flat, None) == (True, None), "")

    return c.fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if run() else 0)
