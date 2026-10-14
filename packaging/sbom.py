#!/usr/bin/env python3
"""sbom.py - emit a CycloneDX 1.4 software bill of materials for bench-test.

Stdlib only, so it runs anywhere the toolkit does (including the live USB's
system Python and inside a frozen bundle). It inventories:

* the declared Python runtime/compute dependencies (requirements.txt and
  requirements-compute.txt - the same packages bench/bootstrap.py installs),
* the third-party binaries the toolkit downloads/loads (LibreHardwareMonitor,
  nvfancontrol), and
* the toolkit itself, with its release version and the git build ref/commit.

    python packaging/sbom.py                 # -> stdout
    python packaging/sbom.py --out sbom.json  # -> file (LF, 2-space JSON)

The document is intended for a release artifact and for supply-chain review;
the license policy that consumes it lives in docs/DEPENDENCIES.md.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOOLKIT_NAME = "bench-test"
TOOLKIT_VENDOR = "Maine Cyber Technologies, LLC"
VERSION_FILE = "VERSION"
REQUIREMENTS = ("requirements.txt", "requirements-compute.txt")

# SPDX ids for the declared Python packages (well-known upstream licenses).
_DEP_LICENSES = {
    "numpy": ["BSD-3-Clause"],
    "psutil": ["BSD-3-Clause"],
    "cryptography": ["Apache-2.0", "BSD-3-Clause"],
    "windows-curses": ["MIT"],
    "onnxruntime-directml": ["MIT"],
    "onnx": ["Apache-2.0"],
    "torch": ["BSD-3-Clause"],
}

# Third-party binaries the toolkit fetches and/or bundles. Kept in one place so
# THIRD_PARTY_NOTICES.md and this SBOM agree.
BUNDLED_BINARIES = [
    {
        "name": "LibreHardwareMonitor",
        "version": "latest",  # Get-Sensors.ps1 tracks releases/latest (unpinned)
        "type": "library",
        "licenses": ["MPL-2.0"],
        "purl": "pkg:github/LibreHardwareMonitor/LibreHardwareMonitor",
        "description": "Sensor library (LibreHardwareMonitorLib.dll) used by "
                       "bench/Get-Sensors.ps1 / Sample-Sensors.ps1 for CPU/GPU "
                       "temperature, power, clocks, fan RPM and VRAM on "
                       "AMD/Intel Windows.",
        "source": "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor",
    },
    {
        "name": "nvfancontrol",
        "version": "0.5.1",
        "type": "application",
        "licenses": ["GPL-3.0-or-later"],
        "purl": "pkg:github/foucault/nvfancontrol@0.5.1",
        "description": "NVIDIA GPU fan-curve daemon installed by "
                       "bench/Install-GpuFanCurve.ps1 (RTX build, pinned tag). "
                       "Downloaded at run time; not redistributed by the toolkit.",
        "source": "https://github.com/foucault/nvfancontrol",
    },
]


def read_version(repo):
    """The release version from VERSION (first line), or '0.0.0' if absent."""
    try:
        with open(os.path.join(repo, VERSION_FILE), encoding="utf-8") as fh:
            return fh.read().strip().splitlines()[0].strip() or "0.0.0"
    except (OSError, IndexError):
        return "0.0.0"


def _git(repo, *args):
    """stdout of a git command in `repo`, or '' (not a checkout / no git)."""
    try:
        r = subprocess.run(["git", "-C", repo] + list(args),
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def git_info(repo):
    """{'commit','ref','dirty'} from git, empty strings when unavailable."""
    commit = _git(repo, "rev-parse", "--short", "HEAD")
    ref = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    dirty = bool(_git(repo, "status", "--porcelain"))
    return {"commit": commit, "ref": ref, "dirty": dirty}


def _dep_name(line):
    """Package name from a requirements line (strips comments/markers/extras)."""
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return ""
    name = re.split(r"[<>=!~;\[\s]", line, 1)[0].strip()
    return name


def declared_python_deps(repo):
    """[(name, manifest)] for every package in the requirements manifests."""
    out = []
    seen = set()
    for fname in REQUIREMENTS:
        path = os.path.join(repo, fname)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                name = _dep_name(line)
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    out.append((name, fname))
    return out


def _licenses(ids):
    return [{"license": {"id": i}} for i in ids]


def _python_component(name, manifest):
    comp = {
        "type": "library",
        "name": name,
        "purl": "pkg:pypi/%s" % name.lower(),
        "properties": [
            {"name": "bench.manifest", "value": manifest},
            {"name": "bench.pinned", "value": "false"},
        ],
    }
    lic = _DEP_LICENSES.get(name.lower())
    if lic:
        comp["licenses"] = _licenses(lic)
    return comp


def _binary_component(spec):
    return {
        "type": spec["type"],
        "name": spec["name"],
        "version": spec["version"],
        "purl": spec["purl"],
        "description": spec["description"],
        "licenses": _licenses(spec["licenses"]),
        "externalReferences": [
            {"type": "website", "url": spec["source"]},
        ],
    }


def build_sbom(repo=None):
    """Return the CycloneDX 1.4 document as a dict."""
    repo = repo or REPO
    version = read_version(repo)
    git = git_info(repo)
    build = git["commit"] or ""
    if build and git["dirty"]:
        build += "-dirty"

    toolkit = {
        "type": "application",
        "name": TOOLKIT_NAME,
        "version": version,
        "bom-ref": "pkg:generic/%s@%s" % (TOOLKIT_NAME, version),
        "purl": "pkg:generic/%s@%s" % (TOOLKIT_NAME, version),
        "supplier": {"name": TOOLKIT_VENDOR},
    }
    if build:
        toolkit["version"] = "%s+%s" % (version, build)

    components = [_python_component(n, m) for n, m in declared_python_deps(repo)]
    components += [_binary_component(b) for b in BUNDLED_BINARIES]

    serial_seed = "%s|%s|%s" % (TOOLKIT_NAME, version, build or "nogit")
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "serialNumber": "urn:uuid:%s" % uuid.uuid5(uuid.NAMESPACE_URL, serial_seed),
        "version": 1,
        "metadata": {
            "timestamp": datetime.datetime.now(
                datetime.timezone.utc).replace(microsecond=0).isoformat(),
            "tools": [{
                "vendor": TOOLKIT_VENDOR,
                "name": "packaging/sbom.py",
                "version": version,
            }],
            "component": toolkit,
            "properties": [
                {"name": "bench.build.version", "value": version},
                {"name": "bench.build.commit", "value": git["commit"] or "unknown"},
                {"name": "bench.build.ref", "value": git["ref"] or "unknown"},
                {"name": "bench.build.dirty", "value": "true" if git["dirty"] else "false"},
            ],
        },
        "components": components,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Emit a CycloneDX 1.4 SBOM for bench-test (stdlib only).")
    ap.add_argument("--out", default=None,
                    help="write the SBOM here (default: stdout)")
    ap.add_argument("--repo", default=REPO,
                    help="repository root to inventory (default: the toolkit root)")
    args = ap.parse_args(argv)

    text = json.dumps(build_sbom(args.repo), indent=2) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("wrote %s" % args.out)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
