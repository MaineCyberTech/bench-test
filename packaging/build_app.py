#!/usr/bin/env python3
"""Build standalone bench-test app bundles (GUI / TUI / agent) with PyInstaller.

    pip install pyinstaller
    python packaging/build_app.py [--out dist]

Produces `dist/bench-gui`, `dist/bench-tui` and `dist/bench-agent` (one-folder
bundles) that run **without a Python install** — the dashboards/agent are
stdlib-only (tkinter / curses), so the bundle carries its own interpreter.

Running a *local* battery still needs the toolkit (and numpy/PyTorch for the
compute phases): point the app at a checkout with `--repo DIR` / `$BENCH_REPO`,
or put the bundle next to a `bench/` directory. Connecting to a control plane and
viewing/controlling runs needs nothing else.

CI builds these for Windows and Linux and attaches them to a release
(`.github/workflows/build-app.yml`).
"""
import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "bench")
APPS = [("bench-gui", "gui.py", True), ("bench-tui", "tui.py", False),
        ("bench-agent", "agent.py", False)]  # (name, script, no-console)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(REPO, "dist"))
    a = ap.parse_args()
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not installed: pip install pyinstaller")
        return 2
    os.makedirs(a.out, exist_ok=True)
    for name, script, noconsole in APPS:
        cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--name", name, "--paths", BENCH, "--hidden-import", "benchreport",
               "--distpath", a.out, "--workpath", os.path.join(a.out, "_work"),
               "--specpath", os.path.join(a.out, "_spec"),
               os.path.join(BENCH, script)]
        if noconsole and sys.platform == "win32":
            cmd.append("--noconsole")
        print("== building %s" % name, flush=True)
        if subprocess.run(cmd).returncode != 0:
            print("build failed: %s" % name)
            return 1
    print("built %s -> %s" % (", ".join(n for n, _s, _c in APPS), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
