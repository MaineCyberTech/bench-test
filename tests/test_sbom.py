"""SBOM + dependency-manifest tests.

Covers the W15 remediation: a declared dependency manifest that matches what
bench/bootstrap.py actually installs, and a CycloneDX SBOM (packaging/sbom.py)
that lists those deps, the bundled third-party binaries and the toolkit version.

Stdlib only and side-effect free: no network, no pip, no git required (the SBOM
degrades to a version-only document when git is unavailable).
"""
import importlib.util
import json
import os
import subprocess
import sys

from harness import Checker, BENCH, REPO

SBOM = os.path.join(REPO, "packaging", "sbom.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _bootstrap_packages():
    """Every package name bootstrap.py can install (base + extras + torch)."""
    bs = _load("bootstrap", os.path.join(BENCH, "bootstrap.py"))
    pkgs = list(bs.BASE_PACKAGES)
    for group in bs.GPU_PACKAGES.values():
        pkgs += list(group)
    pkgs += ["windows-curses"]  # packages_for() adds it on Windows
    pkgs += ["torch"]           # packages_for(include_torch=True) / Invoke-GpuBench
    return pkgs


def run():
    c = Checker("sbom")
    sb = _load("sbom", SBOM)

    # ---- the SBOM is a valid CycloneDX JSON document ----
    doc = sb.build_sbom(REPO)
    text = json.dumps(doc)
    parsed = json.loads(text)  # round-trips
    c.check("SBOM serialises to valid JSON", isinstance(parsed, dict), type(parsed))
    c.check("SBOM is CycloneDX 1.4",
            doc.get("bomFormat") == "CycloneDX" and doc.get("specVersion") == "1.4",
            (doc.get("bomFormat"), doc.get("specVersion")))
    c.check("SBOM has a URN serial number",
            str(doc.get("serialNumber", "")).startswith("urn:uuid:"),
            doc.get("serialNumber"))

    # ---- it declares the expected components ----
    names = {str(comp.get("name", "")).lower() for comp in doc.get("components", [])}
    for pkg in _bootstrap_packages():
        c.check("SBOM declares python dep %s" % pkg, pkg.lower() in names, sorted(names))
    for binary in ("librehardwaremonitor", "nvfancontrol"):
        c.check("SBOM declares bundled binary %s" % binary,
                binary in names, sorted(names))

    # ---- it carries the toolkit version + build ref/commit ----
    version = sb.read_version(REPO)
    c.check("read_version returns the VERSION file",
            bool(version) and version != "0.0.0", version)
    meta = doc.get("metadata", {})
    comp = meta.get("component", {})
    c.check("metadata.component is the toolkit",
            comp.get("name") == "bench-test", comp.get("name"))
    c.check("the toolkit component carries the version",
            str(comp.get("version", "")).startswith(version),
            comp.get("version"))
    props = {p.get("name"): p.get("value") for p in meta.get("properties", [])}
    c.check("metadata records the build ref", "bench.build.ref" in props, sorted(props))
    c.check("metadata records the build commit", "bench.build.commit" in props, sorted(props))
    c.check("metadata records the build version",
            props.get("bench.build.version") == version, props.get("bench.build.version"))

    # ---- every package bootstrap.py references is in the manifests ----
    declared = {n.lower() for n, _m in sb.declared_python_deps(REPO)}
    for pkg in _bootstrap_packages():
        c.check("requirements declares %s" % pkg, pkg.lower() in declared, sorted(declared))
    for fname in ("requirements.txt", "requirements-compute.txt"):
        c.check("%s exists" % fname, os.path.isfile(os.path.join(REPO, fname)),
                os.path.join(REPO, fname))

    # ---- the CLI emits the same document (stdout + --out) ----
    r = subprocess.run([sys.executable, SBOM], capture_output=True, text=True, timeout=60)
    c.check("sbom.py CLI exits 0", r.returncode == 0, r.stderr[:200])
    try:
        cli = json.loads(r.stdout)
        c.check("sbom.py CLI prints valid JSON",
                cli.get("bomFormat") == "CycloneDX", r.stdout[:120])
    except Exception as e:  # noqa: BLE001
        c.check("sbom.py CLI prints valid JSON", False, str(e))

    import tempfile
    out = os.path.join(tempfile.mkdtemp(prefix="bench-sbom-"), "sbom.json")
    r = subprocess.run([sys.executable, SBOM, "--out", out],
                       capture_output=True, text=True, timeout=60)
    c.check("sbom.py --out writes a file", r.returncode == 0 and os.path.isfile(out),
            (r.returncode, r.stderr[:160]))
    if os.path.isfile(out):
        with open(out, encoding="utf-8") as fh:
            body = fh.read()
        c.check("the --out document is valid JSON",
                json.loads(body).get("bomFormat") == "CycloneDX", body[:120])

    return c.fails


if __name__ == "__main__":
    import sys as _s
    _s.exit(1 if run() else 0)
