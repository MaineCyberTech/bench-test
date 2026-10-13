"""Versions: one release version in VERSION, reported by the toolkit, the runners
and the control plane, so a fleet view can say what is actually running.

The version is load-bearing for fleet hygiene: an operator reads it to tell which
stick is on which release, so it must exist, be well formed, and actually reach
the places that display it.
"""
import importlib.util
import os
import re

from harness import Checker, BENCH, REPO, Server


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run():
    c = Checker("version")

    vpath = os.path.join(REPO, "VERSION")
    c.check("VERSION exists", os.path.isfile(vpath), vpath)
    v = ""
    if os.path.isfile(vpath):
        with open(vpath, encoding="utf-8") as fh:
            v = fh.read().strip()
    c.check("VERSION is semver", bool(re.match(r"^\d+\.\d+\.\d+$", v)), v)

    rep = _load("benchreport", os.path.join(BENCH, "benchreport.py"))
    c.check("bench_version is tag-derived in a checkout",
            rep.bench_version(REPO).split("+")[0] == v or rep.git_describe(REPO)[0] == "",
            (rep.bench_version(REPO), v))
    c.check("bench_version falls back instead of raising",
            rep.bench_version(os.path.join(REPO, "definitely-not-here")) == "0.0.0+unknown", "")
    c.check("toolkit_version carries the version",
            rep.toolkit_version(REPO).get("version") == rep.bench_version(REPO),
            rep.toolkit_version(REPO).get("version"))
    c.check("version_string starts with the version",
            rep.version_string(REPO).startswith(v), rep.version_string(REPO))
    c.check("version_string carries the build commit",
            "+" in rep.version_string(REPO) or not rep.git_commit(REPO),
            rep.version_string(REPO))

    # a malformed VERSION must be rejected, not passed around as a release
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "VERSION"), "w") as fh:
            fh.write("not-a-version\n")
        c.check("malformed VERSION is rejected",
                rep.bench_version(td) == "0.0.0+invalid", rep.bench_version(td))
        with open(os.path.join(td, "VERSION"), "w") as fh:
            fh.write("2.3.4-rc.1\n")
        c.check("a prerelease VERSION is accepted",
                rep.bench_version(td) == "2.3.4-rc.1", rep.bench_version(td))
        c.check("version_info reports the release, build and protocol",
                rep.version_info(td)["release"] == "2.3.4-rc.1"
                and rep.version_info(td)["protocol"] == rep.PROTOCOL,
                rep.version_info(td))
    c.check("toolkit_version advertises the protocol",
            rep.toolkit_version(REPO).get("protocol") == rep.PROTOCOL,
            rep.toolkit_version(REPO))
    # the version is tag-derived, so a checkout that has a tag reports it and the
    # release only moves when the tag does
    rel, n, cm = rep.git_describe(REPO)
    c.check("git_describe finds the release tag", rel == v or rel == "",
            (rel, n, cm))
    if rel:
        c.check("bench_version follows the tag",
                rep.bench_version(REPO).split("+")[0] == rel, rep.bench_version(REPO))
        c.check("commits past the tag become build metadata",
                (n == 0 and "+" not in rep.bench_version(REPO))
                or rep.bench_version(REPO) == "%s+%d" % (rel, n),
                (rep.bench_version(REPO), n))

    # the control plane reports its own version (it is deployed as a copy)
    srv = Server()
    try:
        _, health = srv.client().call("GET", "/health")
        c.check("health reports the version", health.get("version") == v, health)
        c.check("health reports the protocol",
                health.get("protocol") == rep.PROTOCOL, health.get("protocol"))
        _, state = srv.client().call("GET", "/api/v1/state")
        c.check("state reports the version", state.get("version") == v, state.get("version"))
        c.check("state reports the protocol",
                state.get("protocol") == rep.PROTOCOL, state.get("protocol"))
    finally:
        srv.stop()

    # ...and the surfaces that show it actually reference it
    def src(rel):
        with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
            return fh.read()

    c.check("gui shows the version", "bench_version(REPO)" in src("bench/gui.py"), "")
    c.check("tui shows the version", "bench_version(REPO)" in src("bench/tui.py"), "")
    c.check("web shows the version", "STATE.version" in src("server/web/app.js"), "")
    c.check("web shows the toolkit version",
            "caps.toolkit.version" in src("server/web/app.js"), "")
    for rel, who in (("bench/gui.py", "gui"), ("bench/tui.py", "tui"), ("bench/agent.py", "agent")):
        c.check("%s registers a version" % who, "version=version_string" in src(rel), "")
    c.check("iso release names the version",
            "steps.ver.outputs.version" in src(".github/workflows/build-iso.yml"), "")
    c.check("app release names the version",
            "steps.ver.outputs.version" in src(".github/workflows/build-app.yml"), "")

    return c.fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if run() else 0)
