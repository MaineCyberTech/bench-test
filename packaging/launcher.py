#!/usr/bin/env python3
"""Entry point for the packaged bench-test apps (GUI / TUI / agent).

The bundle carries the toolkit next to its exe, so this launcher **runs the app
straight from `<bundle>/bench/<app>.py`** instead of freezing that code into the
executable. That is what makes a downloaded app fully updatable in place:

  * the server's `update` command, or the startup check in `auto_update()`,
    refreshes `bench/` and the *dashboard and runners both* change with it;
  * only this launcher and the bundled interpreter need a new download, and
    those are tiny and stable.

Freezing the dashboard instead (the previous shape) meant a refreshed toolkit
only updated the files that get *executed* - the phases and `run_win.py` - while
the GUI/TUI code stayed at whatever version the download was built from.

Which app to run comes from the executable's name (`bench-tui.exe` -> `tui.py`),
overridable with `BENCH_APP=tui|gui|agent` for an odd filename.
"""
import os
import runpy
import sys

APPS = {"bench-tui": "tui.py", "bench-gui": "gui.py", "bench-agent": "agent.py"}


def _bundle_dir():
    """The directory holding `bench/`: next to the exe when frozen, else the repo."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _target(argv0):
    forced = (os.environ.get("BENCH_APP") or "").strip().lower()
    if forced:
        return APPS.get("bench-" + forced) or (forced if forced.endswith(".py") else None)
    name = os.path.basename(argv0 or "").lower()
    for key, script in APPS.items():
        if key in name:
            return script
    return "tui.py"


def main():
    here = _bundle_dir()
    script_name = _target(sys.argv[0] if sys.argv else "")
    if not script_name:
        sys.stderr.write("bench-test: cannot tell which app to run; set BENCH_APP=tui|gui|agent\n")
        return 2
    bench = os.path.join(here, "bench")
    script = os.path.join(bench, script_name)
    if not os.path.isfile(script):
        sys.stderr.write(
            "bench-test: %s not found.\n"
            "The bundle must keep its toolkit: a 'bench' directory next to the executable.\n"
            "If it was moved, re-extract the download, or set BENCH_REPO to a checkout.\n"
            % script)
        return 3
    # the toolkit wins over anything the launcher itself imported, and every import
    # the app makes (benchreport, bootstrap, the phases) resolves from here
    sys.path.insert(0, bench)
    sys.argv = [script] + list(sys.argv[1:])
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
