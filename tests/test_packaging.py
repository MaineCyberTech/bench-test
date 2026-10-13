"""Packaging tests - baked control-plane config for the app bundles.

Stdlib only, side-effect free: no PyInstaller, no network. We check that a
`bench-agent.conf` placed next to the toolkit (as build_app.py bakes it) is picked
up by load_conf(), and that write_agent_conf() writes it from the build env.
"""
import importlib.util
import os
import tempfile

from harness import Checker, BENCH, REPO


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run():
    c = Checker("packaging")
    rep = _load("benchreport", os.path.join(BENCH, "benchreport.py"))
    ba = _load("build_app", os.path.join(REPO, "packaging", "build_app.py"))

    # load_conf finds <repo>/bench-agent.conf (the baked location)
    root = tempfile.mkdtemp(prefix="bench-pkg-")
    here = os.path.join(root, "bench")
    os.makedirs(here)
    with open(os.path.join(root, "bench-agent.conf"), "w", encoding="utf-8") as fh:
        fh.write("BENCHD_SERVER=http://lab.test:8722\nBENCHD_TOKEN=tok123\n")
    env_keys = ("BENCH_AGENT_CONF", "BENCHD_SERVER", "BENCHD_TOKEN")
    saved = {k: os.environ.pop(k, None) for k in env_keys}
    # point HOME at a clean dir so a real ~/.config can't shadow the baked copy
    saved.update({k: os.environ.get(k) for k in ("HOME", "USERPROFILE")})
    os.environ["HOME"] = root
    os.environ["USERPROFILE"] = root
    try:
        server, token = rep.load_conf(here=here)
        c.check("load_conf reads the baked bench-agent.conf next to the toolkit",
                server == "http://lab.test:8722" and token == "tok123", (server, token))
        # an explicit config wins over the baked default
        cwd2 = tempfile.mkdtemp(prefix="bench-cwd-")
        with open(os.path.join(cwd2, "bench-agent.conf"), "w", encoding="utf-8") as fh:
            fh.write("BENCHD_SERVER=http://override:9999\nBENCHD_TOKEN=over\n")
        oldcwd = os.getcwd()
        os.chdir(cwd2)
        try:
            s2, t2 = rep.load_conf(here=here)
            c.check("an explicit ./bench-agent.conf overrides the baked one",
                    s2 == "http://override:9999" and t2 == "over", (s2, t2))
        finally:
            os.chdir(oldcwd)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # write_agent_conf bakes content from BENCH_BAKE_AGENT_CONF
    out = tempfile.mkdtemp(prefix="bench-bake-")
    os.environ["BENCH_BAKE_AGENT_CONF"] = "BENCHD_SERVER=http://x:1\nBENCHD_TOKEN=abc"
    try:
        c.check("write_agent_conf reports it baked", ba.write_agent_conf(out) is True, "")
        p = os.path.join(out, "bench-agent.conf")
        c.check("write_agent_conf wrote the file", os.path.isfile(p), p)
        body = open(p, encoding="utf-8").read()
        c.check("baked conf has a trailing newline and the values",
                body.endswith("\n") and "BENCHD_TOKEN=abc" in body, repr(body))
    finally:
        os.environ.pop("BENCH_BAKE_AGENT_CONF", None)

    # nothing to bake -> no file, no crash
    out2 = tempfile.mkdtemp(prefix="bench-bake-")
    c.check("write_agent_conf is a no-op without the secret",
            ba.write_agent_conf(out2) is False
            and not os.path.exists(os.path.join(out2, "bench-agent.conf")), "")

    # Windows AMD/Intel GPU telemetry: the LHM sampler exists and run_win uses it
    ss = os.path.join(BENCH, "Sample-Sensors.ps1")
    c.check("Sample-Sensors.ps1 exists", os.path.isfile(ss), ss)
    with open(os.path.join(BENCH, "run_win.py"), encoding="utf-8") as fh:
        rw = fh.read()
    c.check("run_win.py wires the LibreHardwareMonitor sampler",
            "Sample-Sensors.ps1" in rw and "lhm_thread" in rw, "")

    return c.fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if run() else 0)
