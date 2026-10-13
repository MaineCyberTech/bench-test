"""Stabilized soak: the GPU soak may stop once the temperature plateaus, and the
presets/control plane can reach the runner's tuning knobs.

Two things are easy to regress and hard to notice: the plateau test's sense
(stop vs keep going), and the payload -> argv mapping. Both are covered here.
"""
import importlib.util
import os
import types

from harness import Checker, BENCH, REPO


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _args(seconds, until_stable):
    return types.SimpleNamespace(seconds=seconds, until_stable=until_stable)


def run():
    c = Checker("soak")

    # ---- payload -> argv (benchreport) ----
    rep = _load("benchreport", os.path.join(BENCH, "benchreport.py"))
    got = rep.extra_runner_args({"cpu_soak": 45, "ram_soak": 30, "disk_seconds": 30,
                                 "gpu_size": 4096, "cpu_size": 2048, "ram_size_mb": 512,
                                 "passes": 1, "steady_gb": 2, "stable": 1})
    want = ["--cpu-soak", "45", "--ram-soak", "30", "--disk-seconds", "30",
            "--gpu-size", "4096", "--cpu-size", "2048", "--ram-size-mb", "512",
            "--passes", "1", "--steady-gb", "2", "--soak-until-stable"]
    c.check("extra_runner_args maps every knob", got == want, got)
    c.check("extra_runner_args empty without tune", rep.extra_runner_args({}) == [], "")
    c.check("extra_runner_args ignores absent keys",
            rep.extra_runner_args({"gpu_soak": 60, "stable": 0}) == [], "")
    c.check("stable flag only when set",
            rep.extra_runner_args({"stable": 1}) == ["--soak-until-stable"]
            and rep.extra_runner_args({"stable": 0}) == [], "")

    # ---- both runners must accept the flag (parity) ----
    sh = open(os.path.join(BENCH, "Run-LinuxBench.sh"), encoding="utf-8").read()
    win = open(os.path.join(BENCH, "run_win.py"), encoding="utf-8").read()
    c.check("linux runner accepts --soak-until-stable",
            "--soak-until-stable)" in sh and "--until-stable" in sh, "")
    c.check("windows runner accepts --soak-until-stable",
            '"--soak-until-stable"' in win and "-SoakUntilStable" in win, "")
    c.check("windows battery forwards the flag",
            "$SoakUntilStable" in open(os.path.join(BENCH, "Run-WorkstationBench.ps1"),
                                       encoding="utf-8").read(), "")

    # ---- the plateau test itself (gpu_bench) ----
    try:
        gb = _load("gpu_bench", os.path.join(BENCH, "gpu_bench.py"))
    except Exception as e:
        print("  SKIP plateau logic (%s)" % e)
        return c.fails

    flat = [[70.0, 40, 250, 2600] for _ in range(8)]
    climb = [[60.0 + i, 40, 250, 2600] for i in range(8)]
    c.check("plateau: flat temps are stable", gb._temp_stable(flat) is True, "")
    c.check("plateau: climbing temps are not stable", gb._temp_stable(climb) is False, "")
    c.check("plateau: needs a full window", gb._temp_stable(flat[:7]) is False, "")

    now = __import__("time").time
    t0 = now() - 50
    c.check("soak: stops early once stable", gb._soak_continue(t0, _args(180, True), flat) is False, "")
    c.check("soak: keeps going while climbing", gb._soak_continue(t0, _args(180, True), climb) is True, "")
    c.check("soak: honours the ceiling",
            gb._soak_continue(now() - 200, _args(180, True), flat) is False, "")
    c.check("soak: no early stop when not requested",
            gb._soak_continue(t0, _args(180, False), flat) is True, "")
    c.check("soak: never stops before the minimum",
            gb._soak_continue(now() - 10, _args(180, True), flat) is True, "")

    return c.fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if run() else 0)
