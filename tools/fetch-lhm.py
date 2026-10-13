#!/usr/bin/env python3
"""Fetch + verify the pinned LibreHardwareMonitor release into tools/lhm.

Why this exists (EVOL-P3-002): the app bundle ships `tools/lhm` (the sensor
library used for AMD/Intel Windows telemetry), but `tools/` is git-ignored, so a
fresh clone - and CI - had no library and `packaging/build_app.py` built a bundle
without it *silently*. This script makes the vendored set reproducible: it
downloads the pinned release, verifies its SHA-256 against the constant below
(fail closed), and extracts the library and its managed dependencies into
`tools/lhm`.

    python tools/fetch-lhm.py                 # -> <repo>/tools/lhm
    python tools/fetch-lhm.py --dest DIR      # -> DIR

Stdlib only (runs on the live USB's system Python and in CI). Binaries are NOT
committed; only this script and `tools/README.md` are tracked, so `tools/lhm`
stays out of git while a clone can still reproduce it. See docs/DEPENDENCIES.md.
"""
import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile

# The pin. Bump the version and the digest together; a mismatch fails the fetch.
LHM_VERSION = "v0.9.6"
LHM_URL = ("https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/"
           "releases/download/%s/LibreHardwareMonitor.zip" % LHM_VERSION)
# SHA-256 of the pinned release asset (the GitHub release digest, verified locally).
LHM_SHA256 = "086d9f1b5a99e643edc2cfaaac16051685b551e4c5ac0b32a57c58c0e529c001"
LHM_DLL = "LibreHardwareMonitorLib.dll"

# Root-level files the library needs to load: the managed dependencies ship as
# sibling DLLs (HidSharp, DiskInfoToolkit, System.*, ...). Skip the app exe, the
# symbol files and the localised resource subdirectories - none are needed for
# Add-Type / a library load.
_KEEP_EXT = (".dll", ".config", ".xml")


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _download(url, dest, timeout=180):
    with urllib.request.urlopen(url, timeout=timeout) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh, 1 << 20)


def fetch(dest, url=LHM_URL, sha256=LHM_SHA256, log=print):
    """Download `url`, verify it against `sha256`, extract the library into `dest`.

    Returns `dest`. Raises RuntimeError on a digest mismatch or a malformed
    archive (fail closed - a tampered download is never extracted).
    """
    os.makedirs(dest, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    try:
        log("[lhm] downloading %s" % url)
        _download(url, tmp.name)
        got = sha256_file(tmp.name)
        if got.lower() != (sha256 or "").lower():
            raise RuntimeError("sha256 mismatch: expected %s, got %s" % (sha256, got))
        with zipfile.ZipFile(tmp.name) as z:
            members = [m for m in z.namelist()
                       if "/" not in m and m.lower().endswith(_KEEP_EXT)]
            if LHM_DLL not in members:
                raise RuntimeError("%s not found in the release archive" % LHM_DLL)
            for m in members:
                with z.open(m) as src, open(os.path.join(dest, m), "wb") as out:
                    shutil.copyfileobj(src, out)
        log("[lhm] verified sha256 %s" % got)
        log("[lhm] wrote %d file(s) to %s" % (len(members), dest))
        return dest
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="fetch the pinned LibreHardwareMonitor release (%s)" % LHM_VERSION)
    ap.add_argument("--dest", default=os.path.join(repo_root(), "tools", "lhm"),
                    help="output directory (default: <repo>/tools/lhm)")
    a = ap.parse_args(argv)
    try:
        fetch(a.dest)
    except Exception as e:
        sys.stderr.write("fetch-lhm: %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
