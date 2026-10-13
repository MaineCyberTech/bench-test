"""Client/build P3 leftovers: env-var drift, the pinned LHM fetch, ISO provenance.

Stdlib only and side-effect free (no network, no PyInstaller): the LHM fetch is
exercised against a local `file://` zip, so the download+verify path is covered
without touching GitHub.

Covers:
  * SECRET-P3-002 / INFRA-P3-001 - the live launcher must not export config keys
    nothing reads; the honoured keys are exactly the ones with a reader.
  * EVOL-P3-002 - `tools/fetch-lhm.py` pins the LibreHardwareMonitor release,
    verifies its SHA-256 and fails closed; the bundle build fetches it when the
    git-ignored `tools/lhm` is absent.
  * SBOM-P3-001 / SC-P3-001 - the ISO build emits a package inventory + a
    checksum, and `docs/DEPENDENCIES.md` states the unpinned-provenance risks.
"""
import hashlib
import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile

from harness import Checker, REPO, BENCH, skip, tmpdir


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _py_sources():
    for d in ("bench", "server", "packaging"):
        base = os.path.join(REPO, d)
        for root, _dirs, files in os.walk(base):
            for f in files:
                if f.endswith(".py"):
                    yield os.path.join(root, f)


def _env_allow_list(src):
    """The keys accepted by bench-agent-env's parser `case` pattern."""
    m = re.search(r'case "\$key" in\s*\n\s*([A-Z_|]+)\)', src)
    return m.group(1).split("|") if m else []


def run():
    c = Checker("build_left")

    # ---- 1. env name drift: no exported key without a reader -----------------
    envsh_path = os.path.join(REPO, "live", "airootfs", "usr", "local", "bin",
                              "bench-agent-env")
    envsh = _read(envsh_path)
    allowed = _env_allow_list(envsh)
    c.check("bench-agent-env still parses a key allow-list", bool(allowed), allowed)
    c.check("BENCHD_INSECURE is no longer accepted (it had no consumer)",
            "BENCHD_INSECURE" not in allowed, allowed)
    c.check("BENCH_AGENT_NAME is accepted", "BENCH_AGENT_NAME" in allowed, allowed)
    c.check("BENCH_AGENT_INSTANCE is accepted", "BENCH_AGENT_INSTANCE" in allowed, allowed)

    py_src = {p: _read(p) for p in _py_sources()}
    blob = "\n".join(py_src.values())
    c.check("BENCHD_INSECURE is read nowhere in the Python clients",
            "BENCHD_INSECURE" not in blob, "")
    c.check("BENCH_AGENT_NAME is consumed (Reporter.name)",
            'os.environ.get("BENCH_AGENT_NAME")' in blob, "")
    # every honoured key must be consumed by a reader somewhere in the repo
    for key in allowed:
        c.check("%s is consumed by a reader" % key, key in blob, key)
    c.check("agent.py lets BENCH_AGENT_NAME set the runner label",
            'os.environ.get("BENCH_AGENT_NAME")' in py_src.get(
                os.path.join(BENCH, "agent.py"), ""), "")

    # ---- 2. EVOL-P3-002: pinned, verified LibreHardwareMonitor fetch ---------
    fetch_path = os.path.join(REPO, "tools", "fetch-lhm.py")
    c.check("tools/fetch-lhm.py exists", os.path.isfile(fetch_path), fetch_path)
    flm = _load("fetch_lhm", fetch_path)
    c.check("the LHM pin names a release (not 'latest')",
            flm.LHM_VERSION.startswith("v") and "latest" not in flm.LHM_URL,
            (flm.LHM_VERSION, flm.LHM_URL))
    c.check("the LHM pin is a 64-hex SHA-256",
            re.fullmatch(r"[0-9a-f]{64}", flm.LHM_SHA256) is not None, flm.LHM_SHA256)

    fsrc = _read(fetch_path)
    imports = set(re.findall(r"^(?:import|from)\s+([a-zA-Z_][\w]*)", fsrc, re.M))
    c.check("fetch-lhm.py is stdlib only",
            imports.issubset(set(sys.stdlib_module_names)), sorted(imports))

    # download + verify + extract, against a local zip (no network)
    zdir = tmpdir()
    zpath = os.path.join(zdir, "LibreHardwareMonitor.zip")
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("LibreHardwareMonitorLib.dll", b"MZ fake library")
        z.writestr("HidSharp.dll", b"MZ managed dep")
        z.writestr("LibreHardwareMonitorLib.xml", b"<doc/>")
        z.writestr("LibreHardwareMonitor.exe", b"MZ app (skip)")
        z.writestr("LibreHardwareMonitorLib.pdb", b"pdb (skip)")
        z.writestr("de/Microsoft.Win32.TaskScheduler.resources.dll", b"loc (skip)")
    digest = hashlib.sha256(open(zpath, "rb").read()).hexdigest()
    url = pathlib.Path(zpath).as_uri()
    dest = tmpdir()
    flm.fetch(dest, url=url, sha256=digest, log=lambda *_a: None)
    c.check("fetch extracts the library + its managed dependencies",
            os.path.isfile(os.path.join(dest, "LibreHardwareMonitorLib.dll"))
            and os.path.isfile(os.path.join(dest, "HidSharp.dll")), sorted(os.listdir(dest)))
    c.check("fetch skips the exe, symbols and localised resource dirs",
            not os.path.exists(os.path.join(dest, "LibreHardwareMonitor.exe"))
            and not os.path.exists(os.path.join(dest, "LibreHardwareMonitorLib.pdb"))
            and not os.path.isdir(os.path.join(dest, "de")), sorted(os.listdir(dest)))

    bad = tmpdir()
    try:
        flm.fetch(bad, url=url, sha256="0" * 64, log=lambda *_a: None)
        c.check("a sha256 mismatch is refused (fails closed)", False, "no error raised")
    except RuntimeError as e:
        c.check("a sha256 mismatch is refused (fails closed)", "mismatch" in str(e), str(e))
    c.check("nothing is extracted on a mismatch",
            not os.path.isfile(os.path.join(bad, "LibreHardwareMonitorLib.dll")), "")

    gi = _read(os.path.join(REPO, ".gitignore"))
    c.check(".gitignore tracks the fetch script but not the binaries",
            "tools/*" in gi and "!tools/fetch-lhm.py" in gi and "!tools/README.md" in gi, gi)
    ba = _read(os.path.join(REPO, "packaging", "build_app.py"))
    c.check("build_app.py fetches tools/lhm when it is missing",
            "_ensure_lhm" in ba and "fetch-lhm.py" in ba, "")
    c.check("build_app.py warns loudly when LHM cannot be restored",
            "tools/lhm is missing" in ba and "WARNING" in ba, "")

    # ---- 3. SBOM-P3-001 / SC-P3-001: ISO provenance --------------------------
    dep_path = os.path.join(REPO, "docs", "DEPENDENCIES.md")
    c.check("docs/DEPENDENCIES.md exists", os.path.isfile(dep_path), dep_path)
    dep = _read(dep_path)
    for needle in ("SBOM-P3-001", "SC-P3-001", "omarchy", "archlinux:latest",
                   "SHA256SUMS", "packages.txt", "LibreHardwareMonitor"):
        c.check("DEPENDENCIES.md documents %s" % needle, needle in dep, "")

    build = _read(os.path.join(REPO, "live", "Build-LiveUSB.sh"))
    c.check("the ISO build emits a package inventory",
            "packages.txt" in build and "pacman -Q" in build, "")
    c.check("the ISO build emits a SHA256SUMS",
            "SHA256SUMS" in build and "sha256sum" in build, "")

    cp = _read(os.path.join(REPO, "docs", "CONTROL_PLANE.md"))
    c.check("CONTROL_PLANE.md documents the honoured bench-agent.conf keys",
            "BENCH_AGENT_NAME" in cp and "allow-list" in cp, "")

    # shell parse checks (the live scripts run on Linux; bash -n catches syntax)
    bash = shutil.which("bash")
    if bash:
        for label, path in (("Build-LiveUSB.sh", os.path.join(REPO, "live", "Build-LiveUSB.sh")),
                            ("bench-agent-env", envsh_path)):
            r = subprocess.run([bash, "-n", path], capture_output=True, text=True, timeout=60)
            c.check("%s parses under bash -n" % label, r.returncode == 0,
                    (r.returncode, (r.stderr or "")[:200]))
    else:
        skip("the shell parse checks", "no bash on this host")

    return c.fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if run() else 0)
