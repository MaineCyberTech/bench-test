#!/usr/bin/env python3
"""bootstrap.py - give the packaged app a runner interpreter + compute deps.

The dashboards/agent are **stdlib-only** and ship as PyInstaller bundles that run
without a Python install. Running a *battery* still needs an interpreter with
numpy/psutil (and, for a GPU, onnxruntime-directml or CUDA torch). This module
supplies that:

* a per-user cache directory (kept between runs; deleted by ``clean()``),
* a Python interpreter - the machine's own if it has one (>=3.10), else a pinned
  relocatable CPython downloaded from python-build-standalone,
* a virtualenv in the cache with the base packages pre-installed, and
* vendor details so the GPU phases can pull the right backend.

Stdlib only (imports nothing third-party), so it is safe on the live USB's system
Python and inside a frozen bundle. Nothing here runs at import time.

    import bootstrap
    py = bootstrap.ensure_env(log=print)   # -> path to the env python
    bootstrap.clean()                      # remove the cached interpreter + env
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

# --- layout -----------------------------------------------------------------

APP = "bench-test"
# base compute/telemetry deps installed into the env (the runners add torch /
# onnxruntime-directml themselves, into this same interpreter via BENCH_PY).
BASE_PACKAGES = ["numpy", "psutil", "cryptography"]
GPU_PACKAGES = {
    # Windows AMD/Intel: DirectML backend (small). NVIDIA torch is left to
    # Invoke-GpuBench.ps1, which installs the driver-matched CUDA build.
    "dml": ["onnxruntime-directml", "onnx"],
}

# python-build-standalone (relocatable CPython) for machines with no Python.
STANDALONE_TAG = "20261009"
STANDALONE_VERSION = "3.12.15"
STANDALONE_BASE = ("https://github.com/astral-sh/python-build-standalone/releases/"
                   "download/%s/" % STANDALONE_TAG)
_STANDALONE_TRIPLE = {
    ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
    ("Windows", "ARM64"): "aarch64-pc-windows-msvc",
    ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
}


def cache_root():
    """Per-user cache dir (created on demand): %LOCALAPPDATA%\\bench-test on
    Windows, $XDG_DATA_HOME/bench-test (~/.local/share) elsewhere."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP)


def _win(*rel):
    return os.path.join(*rel)


def base_python_path():
    """The standalone interpreter we manage (may not exist yet)."""
    root = cache_root()
    if os.name == "nt":
        return os.path.join(root, "python", "python.exe")
    return os.path.join(root, "python", "bin", "python3")


def env_python_path():
    """The virtualenv interpreter we manage (may not exist yet)."""
    root = cache_root()
    if os.name == "nt":
        return os.path.join(root, "env", "Scripts", "python.exe")
    return os.path.join(root, "env", "bin", "python3")


def _run_hidden(cmd, **kw):
    if os.name == "nt":
        kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.run(cmd, **kw)


def _version_ok(py):
    try:
        out = _run_hidden([py, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                          capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return False
        major, minor = (int(x) for x in out.stdout.strip().split("."))
        return (major, minor) >= (3, 10)
    except Exception:
        return False


def find_system_python():
    """A usable base interpreter already on this machine, or ''."""
    cands = []
    if os.environ.get("BENCH_BASE_PY"):
        cands.append(os.environ["BENCH_BASE_PY"])
    if os.environ.get("BENCH_PY"):
        cands.append(os.environ["BENCH_PY"])
    cands.append(base_python_path())
    if not getattr(sys, "frozen", False):
        cands.append(sys.executable)
    if os.name == "nt":
        for name in ("python", "python3"):
            c = shutil.which(name)
            if c:
                cands.append(c)
        launcher = shutil.which("py")
        if launcher:
            try:
                out = _run_hidden([launcher, "-3", "-c", "import sys;print(sys.executable)"],
                                  capture_output=True, text=True, timeout=15)
                if out.returncode == 0 and out.stdout.strip():
                    cands.append(out.stdout.strip())
            except Exception:
                pass
    else:
        for name in ("python3", "python"):
            c = shutil.which(name)
            if c:
                cands.append(c)
    for c in cands:
        if c and os.path.isfile(c) and _version_ok(c):
            return c
    return ""


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _verify_sha256(path, want, name=""):
    """Verify a download against a published digest, failing closed.

    Returns True when a digest was published and matched, False when none was
    available (the caller then relies on HTTPS), and raises RuntimeError on a
    mismatch so a tampered download is never extracted.
    """
    if not want:
        return False
    got = _sha256(path)
    if got.lower() != str(want).strip().lower():
        raise RuntimeError("sha256 mismatch for %s: expected %s, got %s"
                           % (name or os.path.basename(path), want, got))
    return True


def _safe_extract(tar_path, dest):
    """Extract `tar_path` into `dest`, refusing anything that escapes it.

    A downloaded tarball is untrusted: absolute paths, `..` traversal and links
    whose target leaves `dest` are rejected outright (fail closed) instead of
    being written. Uses tarfile's 'data' filter where the interpreter provides
    it, and always validates members first so the older-interpreter fallback is
    safe too.
    """
    dest = os.path.realpath(dest)
    with tarfile.open(tar_path) as tf:
        for m in tf.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if target != dest and not target.startswith(dest + os.sep):
                raise RuntimeError("unsafe archive member: %s" % m.name)
            if m.issym():
                link = os.path.realpath(os.path.join(os.path.dirname(target), m.linkname))
            elif m.islnk():
                link = os.path.realpath(os.path.join(dest, m.linkname))
            else:
                link = None
            if link is not None and link != dest and not link.startswith(dest + os.sep):
                raise RuntimeError("unsafe archive link: %s -> %s" % (m.name, m.linkname))
        try:
            tf.extractall(dest, filter="data")
        except TypeError:          # `filter=` is only understood by Python >= 3.12
            tf.extractall(dest)


def _download_standalone(log=print):
    triple = _STANDALONE_TRIPLE.get((platform.system(), platform.machine()))
    if not triple:
        raise RuntimeError("no standalone Python build for %s/%s"
                           % (platform.system(), platform.machine()))
    name = "cpython-%s+%s-%s-install_only.tar.gz" % (STANDALONE_VERSION, STANDALONE_TAG, triple)
    url = STANDALONE_BASE + name
    root = cache_root()
    py_dir = os.path.join(root, "python")
    if os.path.exists(base_python_path()):
        return base_python_path()
    log("[env] downloading Python %s (%s)" % (STANDALONE_VERSION, triple))
    os.makedirs(root, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp.name, "wb") as fh:
            shutil.copyfileobj(r, fh, 1024 * 1024)
        # integrity check when the release publishes a .sha256: a mismatch is
        # fatal (fail closed); only a *missing* checksum falls back to HTTPS
        want = ""
        try:
            with urllib.request.urlopen(url + ".sha256", timeout=30) as r:
                want = r.read().decode().split()[0]
        except Exception:
            want = ""
        _verify_sha256(tmp.name, want, name)
        if os.path.isdir(py_dir):
            shutil.rmtree(py_dir, ignore_errors=True)
        # install_only tarballs contain a top-level "python/" directory
        _safe_extract(tmp.name, root)
        if not os.path.isfile(base_python_path()):
            raise RuntimeError("standalone Python extracted but %s is missing" % base_python_path())
        log("[env] Python ready: %s" % base_python_path())
        return base_python_path()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def ensure_python(log=print):
    """A base interpreter: the machine's own, else a downloaded standalone."""
    py = find_system_python()
    if py:
        return py
    return _download_standalone(log)


# --- vendor -----------------------------------------------------------------

def detect_vendor():
    """'nvidia' | 'amd' | 'intel' | '' (best effort, no third-party deps)."""
    if _has("nvidia-smi"):
        return "nvidia"
    if os.name == "nt":
        try:
            ps = ("(Get-CimInstance Win32_VideoController | "
                  "Select-Object -ExpandProperty AdapterCompatibility) -join '|'")
            out = _run_hidden(["powershell", "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, timeout=20).stdout.lower()
            if "amd" in out or "advanced micro" in out or "radeon" in out:
                return "amd"
            if "intel" in out:
                return "intel"
        except Exception:
            pass
    else:
        try:
            out = _run_hidden(["lspci"], capture_output=True, text=True, timeout=10).stdout.lower()
            if "vga" in out or "3d controller" in out:
                if "advanced micro" in out or "radeon" in out or "[amd" in out:
                    return "amd"
                if "intel" in out:
                    return "intel"
        except Exception:
            pass
    return ""


def _has(tool):
    return shutil.which(tool) is not None


def packages_for(vendor=None, include_torch=False):
    pkgs = list(BASE_PACKAGES)
    if os.name == "nt":
        # CPython on Windows has no _curses; the TUI dashboard needs this.
        pkgs.append("windows-curses")
    vendor = vendor or detect_vendor()
    if os.name == "nt" and vendor in ("amd", "intel"):
        pkgs += GPU_PACKAGES["dml"]
    # NVIDIA torch is intentionally left to Invoke-GpuBench.ps1 unless asked.
    if include_torch and vendor == "nvidia":
        pkgs.append("torch")
    return pkgs


# --- env --------------------------------------------------------------------

_IMPORT_NAME = {"onnxruntime-directml": "onnxruntime", "torch": "torch"}


def _import_name(pkg):
    return _IMPORT_NAME.get(pkg, pkg.split("=")[0].split("[")[0].replace("-", "_"))


def _env_has(py, packages):
    imports = [_import_name(p) for p in packages]
    try:
        code = "import %s" % ",".join(imports)
        return _run_hidden([py, "-c", code], capture_output=True, timeout=60).returncode == 0
    except Exception:
        return False


def ensure_env(log=print, vendor=None, packages=None):
    """Create the cached virtualenv and install the needed packages.

    Idempotent and cheap when the env already exists. Returns the env python."""
    env_py = env_python_path()
    base = ensure_python(log)
    if not os.path.isfile(env_py):
        log("[env] creating virtualenv at %s" % os.path.dirname(os.path.dirname(env_py)))
        os.makedirs(cache_root(), exist_ok=True)
        r = _run_hidden([base, "-m", "venv", os.path.join(cache_root(), "env")],
                        capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(env_py):
            raise RuntimeError("venv creation failed: %s" % (r.stderr or r.stdout))
    pkgs = packages if packages is not None else packages_for(vendor)
    if not _env_has(env_py, pkgs):
        log("[env] installing: %s" % ", ".join(pkgs))
        _run_hidden([env_py, "-m", "pip", "install", "--upgrade", "pip"],
                    capture_output=True)
        r = _run_hidden([env_py, "-m", "pip", "install", "--upgrade"] + pkgs,
                        capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("pip install failed:\n%s" % (r.stderr or r.stdout)[-800:])
    return env_py


def python_path(repo=None, ensure=False, log=print):
    """Best interpreter to run the battery with.

    Order: BENCH_PY -> repo/.venv -> our cached env -> (ensure) create env ->
    system python -> standalone python."""
    if os.environ.get("BENCH_PY") and os.path.isfile(os.environ["BENCH_PY"]):
        return os.environ["BENCH_PY"]
    if repo:
        for rel in (("Scripts", "python.exe"), ("bin", "python3"), ("bin", "python")):
            p = os.path.join(repo, ".venv", *rel)
            if os.path.isfile(p):
                return p
    if os.path.isfile(env_python_path()):
        return env_python_path()
    if ensure:
        return ensure_env(log=log)
    return find_system_python() or ""


def status():
    """Small dict for the dashboards' 'Environment' panel."""
    return {
        "cache_root": cache_root(),
        "python": base_python_path(),
        "python_present": os.path.isfile(base_python_path()),
        "env": env_python_path(),
        "env_present": os.path.isfile(env_python_path()),
        "vendor": detect_vendor(),
    }


def clean(log=print, scratch=()):
    """Delete the cached interpreter + env (and any given scratch dirs)."""
    for name in ("env", "python"):
        p = os.path.join(cache_root(), name)
        if os.path.isdir(p):
            log("[env] removing %s" % p)
            shutil.rmtree(p, ignore_errors=True)
    for d in scratch:
        if d and os.path.isdir(d):
            log("[env] removing scratch %s" % d)
            shutil.rmtree(d, ignore_errors=True)
    try:
        if os.path.isdir(cache_root()) and not os.listdir(cache_root()):
            os.rmdir(cache_root())
    except OSError:
        pass


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="bench-test runner environment bootstrap")
    ap.add_argument("action", choices=["status", "ensure", "clean"], nargs="?", default="status")
    ap.add_argument("--vendor", default=None)
    args = ap.parse_args()
    if args.action == "status":
        print(json.dumps(status(), indent=2))
    elif args.action == "ensure":
        print(ensure_env(vendor=args.vendor))
    else:
        clean()
