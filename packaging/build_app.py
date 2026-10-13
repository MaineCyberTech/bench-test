#!/usr/bin/env python3
"""Build standalone bench-test app bundles (GUI / TUI / agent) with PyInstaller.

    pip install pyinstaller
    python packaging/build_app.py [--out dist]

Produces `dist/bench-gui`, `dist/bench-tui` and `dist/bench-agent` (one-folder
bundles) that run **without a Python install** — the dashboards/agent are
stdlib-only (tkinter / curses), so the bundle carries its own interpreter.

Each bundle carries the whole `bench/` toolkit next to the exe, and - since the
launcher - **runs the app from that toolkit instead of freezing it in**. Only
`packaging/launcher.py` and the interpreter are frozen, so refreshing `bench/`
updates the dashboard *and* the runners together: that is what makes a downloaded
app fully updatable in place, from the server's `update` command or from the
`auto_update()` check at startup (throttled, `BENCH_APP_UPDATE_HOURS`, default 6).
The toolkit's stdlib dependencies are still bundled (see `stdlib_imports`), because
a missing one fails inside an `except Exception` that swallows it.

On first run `bench/bootstrap.py` provisions a runner interpreter + the compute deps
(numpy/psutil, plus DirectML for AMD/Intel or CUDA PyTorch for NVIDIA) into a
per-user cache. A repo `.venv` or `$BENCH_REPO` still wins when present. Both
`VERSION` and `.bench-version` are baked in: the dashboard reads `VERSION` for the
release and the stamp for the build, so a non-git bundle reports `1.7.0+<commit>`
instead of `0.0.0+unknown`.

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
LAUNCHER = os.path.join(REPO, "packaging", "launcher.py")
# GUI + TUI dashboards and the agent. On Windows the TUI needs the windows-curses
# wheel installed at build time (CPython on Windows ships no _curses).
# `extra` lists what each app imports but the launcher does not, so PyInstaller
# still bundles it (the app code itself runs from `bench/` on disk, on purpose).
APPS = [("bench-gui", "gui.py", True, ["tkinter"]),
        ("bench-tui", "tui.py", False, ["curses", "_curses"]),
        ("bench-agent", "agent.py", False, [])]


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


def write_agent_conf(bundle_dir):
    """Bake the control-plane config into the bundle (like the live USB).

    Reads BENCH_BAKE_AGENT_CONF (content) or BENCH_BAKE_AGENT_CONF_FILE (path);
    CI passes the repo's BENCH_AGENT_CONF secret as the former, so a downloaded
    app auto-registers. NOTE: the app releases are public, so this makes the
    server URL + token world-readable - use a limited/rotatable token."""
    content = os.environ.get("BENCH_BAKE_AGENT_CONF")
    src = os.environ.get("BENCH_BAKE_AGENT_CONF_FILE")
    if not content and src and os.path.isfile(src):
        with open(src, encoding="utf-8") as fh:
            content = fh.read()
    if not content or not content.strip():
        return False
    if not content.endswith("\n"):
        content += "\n"
    with open(os.path.join(bundle_dir, "bench-agent.conf"), "w", encoding="utf-8") as fh:
        fh.write(content)
    return True


def write_version(bundle_dir):
    """Write `.bench-version` **and** `VERSION` so a non-git bundle reports a real
    identity.

    `bench_version()` reads `VERSION` (the release) while `toolkit_version()` reads
    `.bench-version` (the build), so a bundle needs both: with only the stamp the
    dashboard reported `0.0.0+unknown` for its version.
    """
    commit = _git("rev-parse", "--short", "HEAD")
    ref = _git("rev-parse", "--abbrev-ref", "HEAD")
    if commit and _git("status", "--porcelain"):
        commit += "-dirty"
    rel = ""
    try:
        with open(os.path.join(REPO, "VERSION"), encoding="utf-8") as fh:
            rel = fh.read().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        rel = ""
    v = {"mode": "app", "commit": commit, "ref": ref, "sha256": None,
         "version": rel or None,
         "built_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    try:
        with open(os.path.join(bundle_dir, ".bench-version"), "w", encoding="utf-8") as fh:
            json.dump(v, fh, indent=2)
        if rel:
            with open(os.path.join(bundle_dir, "VERSION"), "w", encoding="utf-8") as fh:
                fh.write(rel + "\n")
    except OSError as e:
        print("warning: could not write the version stamp: %s" % e)
    return v


def stdlib_imports(paths):
    """Stdlib module names to bundle inside the exe.

    The bundle runs the app from `bench/` on disk (see launcher.py), so the toolkit
    modules themselves must NOT be frozen - that would shadow the on-disk copies and
    a toolkit refresh would change nothing. Their stdlib dependencies do have to be
    inside the exe, and PyInstaller can only see what the launcher imports.

    Scanning the toolkit source for top-level names is not enough: `import
    urllib.request` needs `http.client`, `ssl` and `email` too, and a missing one
    fails at import time inside a try/except that swallows it. So the whole stdlib
    is requested; names that do not exist on this platform are filtered out here so
    the build log is not full of "Hidden import not found" noise.
    """
    import importlib.util
    out = []
    for name in sorted(set(getattr(sys, "stdlib_module_names", ()))
                       | set(sys.builtin_module_names)):
        try:
            if importlib.util.find_spec(name) is None:
                continue
        except (ImportError, ValueError, AttributeError):
            continue
        out.append(name)
    return out


def _ensure_lhm(log=print):
    """Make tools/lhm available for the bundle, fetching the pinned release if not.

    `tools/` is git-ignored, so a fresh clone (and CI) had no sensor library and
    the bundle was built *silently* without it (EVOL-P3-002). Run the pinned
    fetch+verify script; the caller warns loudly when it is still missing.
    """
    dll = os.path.join(REPO, "tools", "lhm", "LibreHardwareMonitorLib.dll")
    if os.path.isfile(dll):
        return True
    script = os.path.join(REPO, "tools", "fetch-lhm.py")
    if not os.path.isfile(script):
        return False
    log("== tools/lhm is not vendored; fetching the pinned LibreHardwareMonitor release")
    try:
        r = subprocess.run([sys.executable, script], capture_output=True, text=True)
    except Exception as e:
        log("   fetch-lhm failed to start: %s" % e)
        return False
    for line in (r.stdout or "").splitlines():
        log("   " + line)
    if r.returncode != 0:
        log("   fetch-lhm failed: %s" % (r.stderr or "").strip()[-200:])
    return os.path.isfile(dll)


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
    # ship the sensor library with the bundle: without it a bundled runner has to
    # download it at run time, and when that is slow or blocked every LHM column
    # (per-core load, fan RPM, VRAM total, CPU temp/power/clock) comes out blank
    lhm_dir = os.path.join(REPO, "tools", "lhm")
    have_lhm = _ensure_lhm()
    if not have_lhm:
        print("WARNING: tools/lhm is missing - this bundle will NOT include the "
              "LibreHardwareMonitor sensor library; run tools/fetch-lhm.py. "
              "AMD/Intel Windows telemetry will otherwise need a runtime download.",
              flush=True)
    # what the apps import, so the frozen stdlib is complete even though the app
    # code itself is not frozen
    toolkit_src = [os.path.join(BENCH, f) for f in
                   ("tui.py", "gui.py", "agent.py", "benchreport.py", "bootstrap.py",
                    "gpu_util.py", "linux_info.py")]
    stdlib = stdlib_imports(toolkit_src)
    print("== stdlib modules to bundle: %d" % len(stdlib), flush=True)
    for name, script, noconsole, extra in APPS:
        cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--name", name,
               # ship the whole toolkit next to the exe so a local battery needs no
               # checkout; bootstrap.py provisions the interpreter + deps on first run.
               "--add-data", "%s%sbench" % (BENCH, os.pathsep),
               *(["--add-data", "%s%stools" % (os.path.join(REPO, "tools"), os.pathsep)]
                 if have_lhm else []),
               "--contents-directory", ".",
               "--distpath", a.out, "--workpath", os.path.join(a.out, "_work"),
               "--specpath", os.path.join(a.out, "_spec"),
               # the LAUNCHER is what gets frozen; the app code is run from bench/ so
               # a toolkit refresh updates the dashboard too (see launcher.py)
               LAUNCHER]
        for mod in extra:
            cmd += ["--hidden-import", mod]
        for mod in stdlib:
            cmd += ["--hidden-import", mod]
        if noconsole and sys.platform == "win32":
            cmd.append("--noconsole")
        print("== building %s" % name, flush=True)
        if subprocess.run(cmd).returncode != 0:
            print("build failed: %s" % name)
            return 1
        bdir = os.path.join(a.out, name)
        v = write_version(bdir)
        baked = write_agent_conf(bdir)
        print("   %s version: %s (%s)%s" % (
            name, v["commit"] or "?", v["ref"] or "?",
            ", control-plane config baked" if baked else
            " (no control-plane config baked - set BENCH_AGENT_CONF)"))
    print("built %s -> %s" % (", ".join(n for n, _s, _c, _e in APPS), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
