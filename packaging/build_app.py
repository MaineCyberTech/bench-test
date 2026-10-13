#!/usr/bin/env python3
"""Build standalone bench-test app bundles (GUI / TUI / agent) with PyInstaller.

    pip install pyinstaller
    python packaging/build_app.py [--out dist]

Produces `dist/bench-gui`, `dist/bench-tui` and `dist/bench-agent` (one-folder
bundles) that run **without a Python install** — the dashboards/agent are
stdlib-only (tkinter / curses), so the bundle carries its own interpreter.

Each bundle also carries the whole `bench/` toolkit next to the exe, so a *local*
battery runs with no checkout: on first run `bench/bootstrap.py` provisions a
runner interpreter + the compute deps (numpy/psutil, plus DirectML for AMD/Intel
or CUDA PyTorch for NVIDIA) into a per-user cache. A repo `.venv` or `$BENCH_REPO`
still wins when present. A baked `.bench-version` gives the app a real toolkit
identity (otherwise a non-git bundle reports an empty version).

CI builds these for Windows and Linux and attaches them to a release
(`.github/workflows/build-app.yml`).
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO, "bench")
# GUI + TUI dashboards and the agent. On Windows the TUI needs the windows-curses
# wheel installed at build time (CPython on Windows ships no _curses).
APPS = [("bench-gui", "gui.py", True), ("bench-tui", "tui.py", False),
        ("bench-agent", "agent.py", False)]


def _curses_ok():
    try:
        import curses  # noqa: F401
        return True
    except Exception:
        return False


def _git(*args):
    try:
        r = subprocess.run(["git", "-C", REPO] + list(args),
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def write_version(bundle_dir):
    """Write `.bench-version` so a non-git bundle reports a real identity."""
    commit = _git("rev-parse", "--short", "HEAD")
    ref = _git("rev-parse", "--abbrev-ref", "HEAD")
    if commit and _git("status", "--porcelain"):
        commit += "-dirty"
    v = {"mode": "app", "commit": commit, "ref": ref, "sha256": None,
         "built_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    try:
        with open(os.path.join(bundle_dir, ".bench-version"), "w", encoding="utf-8") as fh:
            json.dump(v, fh, indent=2)
    except OSError as e:
        print("warning: could not write .bench-version: %s" % e)
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(REPO, "dist"))
    a = ap.parse_args()
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not installed: pip install pyinstaller")
        return 2
    if not _curses_ok():
        print("curses unavailable: install the TUI dependency first "
              "(pip install windows-curses on Windows, or the system curses)")
        return 2
    os.makedirs(a.out, exist_ok=True)
    for name, script, noconsole in APPS:
        cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--name", name, "--paths", BENCH,
               "--hidden-import", "benchreport", "--hidden-import", "bootstrap",
               # ship the whole toolkit next to the exe so a local battery needs no
               # checkout; bootstrap.py provisions the interpreter + deps on first run.
               "--add-data", "%s%sbench" % (BENCH, os.pathsep),
               "--contents-directory", ".",
               "--distpath", a.out, "--workpath", os.path.join(a.out, "_work"),
               "--specpath", os.path.join(a.out, "_spec"),
               os.path.join(BENCH, script)]
        if noconsole and sys.platform == "win32":
            cmd.append("--noconsole")
        print("== building %s" % name, flush=True)
        if subprocess.run(cmd).returncode != 0:
            print("build failed: %s" % name)
            return 1
        v = write_version(os.path.join(a.out, name))
        print("   %s version: %s (%s)" % (name, v["commit"] or "?", v["ref"] or "?"))
    print("built %s -> %s" % (", ".join(n for n, _s, _c in APPS), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
