"""Packaging tests - baked control-plane config for the app bundles.

Stdlib only, side-effect free: no PyInstaller, no network. We check that a
`bench-agent.conf` placed next to the toolkit (as build_app.py bakes it) is picked
up by load_conf(), and that write_agent_conf() writes it from the build env.
"""
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile

from harness import Checker, BENCH, REPO, skip


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
    env_keys = ("BENCH_AGENT_CONF", "BENCHD_SERVER", "BENCHD_TOKEN", "BENCHD_JOIN_TOKEN")
    saved = {k: os.environ.pop(k, None) for k in env_keys}
    # point HOME at a clean dir so a real ~/.config can't shadow the baked copy
    saved.update({k: os.environ.get(k) for k in ("HOME", "USERPROFILE")})
    os.environ["HOME"] = root
    os.environ["USERPROFILE"] = root
    try:
        server, token = rep.load_conf(here=here)
        c.check("load_conf reads the baked bench-agent.conf next to the toolkit",
                server == "http://lab.test:8722" and token == "tok123", (server, token))
        # a baked release carries the registration-only join token instead
        with open(os.path.join(root, "bench-agent.conf"), "w", encoding="utf-8") as fh:
            fh.write("BENCHD_SERVER=http://lab.test:8722\nBENCHD_JOIN_TOKEN=join456\n")
        server, token = rep.load_conf(here=here)
        c.check("load_conf honours a baked BENCHD_JOIN_TOKEN (the one-click join config)",
                server == "http://lab.test:8722" and token == "join456", (server, token))
        with open(os.path.join(root, "bench-agent.conf"), "w", encoding="utf-8") as fh:
            fh.write("BENCHD_SERVER=http://lab.test:8722\nBENCHD_TOKEN=tok123\n")
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
    c.check("run_win ties the sampler to its own lifetime (-ParentPid)",
            "-ParentPid" in rw, "")
    with open(os.path.join(BENCH, "tui.py"), encoding="utf-8") as fh:
        tui = fh.read()
    c.check("TUI kills the run tree on close (taskkill + teardown)",
            "_kill_tree" in tui and "taskkill" in tui and "def teardown" in tui, "")

    # ---- the PS phase telemetry must not invent zeros ----
    # nvidia-smi prints N/A for a sensor it cannot read; stripping non-digits leaves ''
    # and [double]('') is 0, so an unreadable sensor was recorded as a real zero and
    # dragged the phase averages down. The parse yields $null now, which Measure-Object
    # skips, and the AMD/Intel fallback (util counter only) leaves the rest unknown.
    ps = shutil.which("powershell") or shutil.which("pwsh")
    parse_re = re.compile(
        r"^[ \t]*(\$p = \$line -split ',' \| ForEach-Object \{[^\n]*\})[ \t]*$", re.M)
    for f in ("Invoke-GpuBench.ps1", "Invoke-CombinedStress.ps1",
              "Invoke-FullSystemSoak.ps1", "Invoke-LongSoak.ps1"):
        with open(os.path.join(BENCH, f), encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        c.check("%s records an unreadable sensor as blank, not 0" % f,
                "$t -eq '' -or $t -eq '.'" in src and "temp = 0; power = 0" not in src, "")
    if ps:
        with open(os.path.join(BENCH, "Invoke-GpuBench.ps1"), encoding="utf-8") as fh:
            gsrc = fh.read()
        m = parse_re.search(gsrc)
        c.check("the GPU bench's parse line is the one under test", bool(m), "")
        snippet = ("$line = 'N/A, 62, 45 %, 1234 MiB, 0, [Not Supported]'\n"
                   + (m.group(1) if m else "") + "\n$p -join ','")
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", snippet],
                           capture_output=True, text=True, timeout=120)
        c.check("the shipped parse keeps real zeros and drops N/A",
                r.stdout.strip() == ",62,45,1234,0,",
                (r.stdout.strip(), r.stderr.strip()[:140]))
    else:
        skip("the shipped-parse behaviour check", "no powershell/pwsh on this host")

    # a slowdown reason nvidia-smi marks "Not Active" is not throttling
    for f in ("Invoke-GpuBench.ps1", "Invoke-CombinedStress.ps1", "Invoke-LongSoak.ps1",
              "Invoke-FullSystemSoak.ps1"):
        with open(os.path.join(BENCH, f), encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        c.check("%s ignores throttle reasons marked Not Active" % f,
                "'Not Active' } |" in src, "")

    # the full-system soak was missed by both fixes: its throttle read kept the Not
    # Active lines and dropped the power-cap reasons, and it had no AMD/Intel util
    # fallback, so on a card it could not read it reported invented 0/0 telemetry
    with open(os.path.join(BENCH, "Invoke-FullSystemSoak.ps1"), encoding="utf-8",
              errors="replace") as fh:
        fss = fh.read()
    c.check("the full-system soak reads the power-cap and HW-slowdown reasons",
            "SW Power Cap" in fss and "HW Slowdown" in fss, "")
    m = re.search(r"^\$throttle = .*$", fss, re.M)
    c.check("the full-system soak throttle read is the one under test", bool(m), "")
    if m and ps:
        snippet = ("function nvidia-smi { @('SW Power Cap : Not Active', "
                   "'HW Slowdown : Active', 'HW Thermal Slowdown : Not Active') }\n"
                   + m.group(0) + "\n$throttle -join '|'\n")
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", snippet],
                           capture_output=True, text=True, timeout=120)
        c.check("the shipped throttle read keeps the active reason and drops Not Active",
                r.stdout.strip() == "HW Slowdown : Active",
                (r.stdout.strip(), r.stderr.strip()[:140]))
    else:
        print("  SKIP the full-soak throttle behaviour check (no powershell/pwsh here)")

    # a failed engine-counter read must not be recorded as util 0 (it dragged the
    # phase average); the sample is appended only when the counter returned a reading
    for f in ("Invoke-GpuBench.ps1", "Invoke-CombinedStress.ps1", "Invoke-FullSystemSoak.ps1"):
        with open(os.path.join(BENCH, f), encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        c.check("%s does not record a failed engine-counter read as util 0" % f,
                "$u = $null" in src and "$u = 0" not in src
                and "if ($null -ne $u)" in src, "")
    c.check("the full-system soak emits each telemetry sample as it is taken",
            "$s = @(Receive-Job $mon" in fss, "")
    c.check("the full-system soak omits a metric with no readings instead of 0",
            "if ($vals.Count -eq 0) { continue }" in fss, "")
    c.check("the full-system soak reports free RAM min from the min, not the max",
            "free RAM min $($stat.freeMiB.min)" in fss and "$stat.freeMiB.max" not in fss, "")
    m = re.search(r"^\$stat = @\{\}\n(?:.*\n)*?^\}", fss, re.M)
    c.check("the shipped full-soak stats block is the one under test", bool(m), "")
    if m and ps:
        snippet = ("$s = @("
                   "[pscustomobject]@{gpu=$null; temp=$null; power=$null; sm=$null; fan=$null; cpu=50; freeMiB=900},"
                   "[pscustomobject]@{gpu=42; temp=$null; power=$null; sm=$null; fan=$null; cpu=60; freeMiB=800})\n"
                   + m.group(0) + "\n"
                   "if ($null -eq $stat.temp) { 'TEMP-OMITTED' } else { 'TEMP-PRESENT' }\n"
                   "'free-min={0} free-avg={1} cpu-avg={2} gpu-avg={3}' -f "
                   "$stat.freeMiB.min, $stat.freeMiB.avg, $stat.cpu.avg, $stat.gpu.avg\n")
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", snippet],
                           capture_output=True, text=True, timeout=120)
        out = r.stdout.strip()
        c.check("a metric with no readings is omitted, and free RAM keeps its minimum",
                "TEMP-OMITTED" in out
                and "free-min=800 free-avg=850 cpu-avg=55 gpu-avg=42" in out,
                (out, r.stderr.strip()[:140]))
    else:
        print("  SKIP the full-soak stats behaviour check (no powershell/pwsh here)")

    # ---- a failed transfer is not a 0 Mbps measurement ----
    # curl exits 0 on an HTTP error; the speed it printed for the error body was
    # recorded as a real rate and the summary copied the 0.0. A failed transfer
    # now yields null (with download_error/upload_error), never 0.
    with open(os.path.join(BENCH, "Invoke-NetBench.ps1"), encoding="utf-8",
              errors="replace") as fh:
        netps = fh.read()
    c.check("the net phase's failed download is null, not 0 Mbps",
            "$downBps = $null" in netps
            and "$downMbps = if ($null -ne $downBps) { [math]::Round($downBps * 8 / 1e6, 1) } else { $null }" in netps, "")
    c.check("the net phase's failed upload is null, not 0 Mbps",
            "'%{speed_upload} %{time_total} %{http_code}'" in netps
            and "$upMbps = if ($null -ne $upBps) { [math]::Round($upBps * 8 / 1e6, 1) } else { $null }" in netps, "")
    c.check("the net report keeps download_error and gains upload_error",
            "download_error = $downErr" in netps and "upload_error = $upErr" in netps, "")
    m = re.search(r"^\$downBps = \$null[^\n]*\n(?:.*\n)*?^\$downMBs = [^\n]*", netps, re.M)
    c.check("the shipped net download block is the one under test", bool(m), "")
    if m and ps:
        snippet = ("function curl.exe { '0.0 1.234 403' }\n$DownMB = 100\n"
                   + m.group(0) + "\n'{0}|{1}' -f $downBps, $downErr\n")
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", snippet],
                           capture_output=True, text=True, timeout=120)
        c.check("a 403 download records no rate and names the error",
                r.stdout.strip() == "|HTTP 403 for 10MB",
                (r.stdout.strip(), r.stderr.strip()[:140]))
    else:
        print("  SKIP the net failure-path behaviour check (no powershell/pwsh here)")

    # the Python net phase: a failed transfer is null, not 0.0 (the same pattern),
    # and the request-size field no longer reads like the PS side's download_mbs
    nb = _load("net_bench", os.path.join(BENCH, "net_bench.py"))
    mbps, nbytes, req_mb = nb.download_mbps("file:///nonexistent/__down?bytes=%d", 10)
    c.check("net_bench records a failed download as None, not 0",
            mbps is None and nbytes == 0 and req_mb == 10, (mbps, nbytes, req_mb))
    with open(os.path.join(BENCH, "net_bench.py"), encoding="utf-8") as fh:
        nbsrc = fh.read()
    c.check("net_bench names the request size unambiguously (no download_mb/MB-s clash)",
            '"download_request_mb"' in nbsrc and '"download_mb"' not in nbsrc, "")

    # the GPU bench looked for TDR/WHEA over the last minute only, so a reset during an
    # hour-long soak was invisible
    with open(os.path.join(BENCH, "Invoke-GpuBench.ps1"), encoding="utf-8") as fh:
        gsrc = fh.read()
    c.check("the GPU bench scans for TDR/WHEA from the start of the run",
            "$runStart = Get-Date" in gsrc and "StartTime = $runStart" in gsrc
            and "AddMinutes(-1)" not in gsrc, "")

    # Run-LinuxBench must not report success when nothing ran at all
    with open(os.path.join(BENCH, "Run-LinuxBench.sh"), encoding="utf-8") as fh:
        lsrc = fh.read()
    c.check("Run-LinuxBench fails when no phase produced a result",
            "no phase produced a result" in lsrc and "exit 1" in lsrc, "")
    bash = shutil.which("bash")
    if bash:
        guard = lsrc[lsrc.index("# Exit non-zero only if nothing ran"):]
        env = dict(os.environ, LOGDIR=tempfile.gettempdir())
        empty = os.path.join(tempfile.mkdtemp(prefix="bench-guard-"), "empty.jsonl")
        open(empty, "w").close()
        r = subprocess.run([bash, "-s"], input=guard, capture_output=True, text=True,
                           timeout=60, env=dict(env, JSONL=empty))
        c.check("the guard exits 1 when the records file has nothing in it",
                r.returncode == 1 and "no phase produced" in r.stderr,
                (r.returncode, r.stderr[:120]))
        good = os.path.join(tempfile.mkdtemp(prefix="bench-guard-"), "ok.jsonl")
        with open(good, "w") as fh:
            fh.write('{"group": "gpu", "phase": "soak"}\n')
        r = subprocess.run([bash, "-s"], input=guard, capture_output=True, text=True,
                           timeout=60, env=dict(env, JSONL=good))
        c.check("the guard exits 0 when at least one phase ran",
                r.returncode == 0, (r.returncode, r.stderr[:120]))
    else:
        skip("the Run-LinuxBench guard behaviour check", "no bash here")

    # ---- the serial dashboard: matches the baud form, needs the device ----
    # ConditionKernelCommandLine has no prefix form (verified against systemd 257:
    # `console:ttyS0` and `console=ttyS0*` both evaluate false) and the exact form
    # misses `console=ttyS0,115200`, so the drop-in uses the bare word plus the device.
    with open(os.path.join(REPO, "live", "airootfs", "etc", "systemd", "system",
                           "bench-tui@ttyS0.service.d", "10-serial.conf"),
              encoding="utf-8") as fh:
        drop = fh.read()
    c.check("the serial dashboard starts for console=ttyS0,115200 too",
            "ConditionKernelCommandLine=console\n" in drop
            and "ConditionKernelCommandLine=console=ttyS0" not in drop, "")
    c.check("and it requires a serial device to exist",
            "ConditionPathExists=/dev/ttyS0" in drop, "")

    # ---- the benchd unit hardens what is safe without breaking self-update ----
    with open(os.path.join(REPO, "server", "benchd.service"), encoding="utf-8") as fh:
        unit = fh.read()
    for directive in ("NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=full",
                      "ProtectHome=true", "RestrictSUIDSGID=true",
                      "ProtectKernelTunables=true", "ProtectKernelModules=true",
                      "ProtectControlGroups=true"):
        c.check("benchd.service sets %s" % directive, directive in unit, "")
    c.check("benchd.service scopes its writes to the data dir",
            "ReadWritePaths=-/opt/bench-test/server/data" in unit, "")
    c.check("the unit still runs as root (the User= migration is documented, not applied)",
            "User=" not in unit, "")
    with open(os.path.join(REPO, "docs", "CONTROL_PLANE.md"), encoding="utf-8") as fh:
        cp = fh.read()
    c.check("the User= migration path is documented",
            "User=benchd" in cp and "ProtectSystem=strict" in cp, "")

    # ---- the LLM phase must not hang on a wedged Ollama server ----
    # `ollama list` blocks forever when the server is stuck, which is how a full run
    # sat on a handheld for eight hours with the machine idle and no model line printed.
    with open(os.path.join(BENCH, "Invoke-LlmBench.ps1"), encoding="utf-8",
              errors="replace") as fh:
        llm = fh.read()
    c.check("the LLM phase bounds every ollama CLI call",
            "Invoke-OllamaCli" in llm and "Wait-Job $job -Timeout $TimeoutSec" in llm, "")
    c.check("and it probes the server before generating", "/api/tags" in llm, "")
    if ps:
        m = re.search(r"^function Invoke-OllamaCli \{.*?^\}", llm, re.S | re.M)
        c.check("the bounded CLI helper is the one under test", bool(m), "")
        # run the helper with the interpreter we actually found (pwsh on Linux CI,
        # powershell on Windows): hardcoding 'powershell' made the check pass or
        # fail by accident wherever that name does not exist.
        snippet = (m.group(0) + "\n"
                   "try { Invoke-OllamaCli -Exe '%s' -CliArgs "
                   "@('-NoProfile','-Command','Start-Sleep -Seconds 30') -TimeoutSec 2; "
                   "'NO-TIMEOUT' } catch { 'TIMED-OUT' }" % ps.replace("'", "''"))
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", snippet],
                           capture_output=True, text=True, timeout=180)
        c.check("a wedged CLI call times out instead of hanging",
                "TIMED-OUT" in r.stdout and "NO-TIMEOUT" not in r.stdout,
                r.stdout.strip()[-160:])
    else:
        skip("the bounded-CLI behaviour check", "no powershell/pwsh on this host")

    # ---- the Linux LLM phase (Ollama): group-gated, bounded, reason-only ----
    # The llm group was Windows-only by implementation; the same bounded probe +
    # per-model eval_count/eval_duration logic now runs on Linux behind the llm
    # group flag (--skip-llm). Every failure is a recorded reason, never a zero.
    lm = re.search(r"# >>> llm bench >>>\n(.*?)\n# <<< llm bench <<<", lsrc, re.S)
    c.check("the shipped Linux LLM block is the one under test", bool(lm), "")
    c.check("the Linux LLM phase is behind the llm group flag",
            "DO_LLM=1" in lsrc and re.search(r"--skip-llm\)\s*DO_LLM=0", lsrc)
            and 'if [ "$DO_LLM" = 1 ]' in lsrc, "")
    c.check("the Linux LLM phase bounds every call (probe, CLI, request)",
            "timeout=PROBE_TIMEOUT" in lsrc and "timeout=CLI_TIMEOUT" in lsrc
            and "timeout=REQUEST_TIMEOUT" in lsrc, "")
    c.check("and it probes /api/tags and derives tok/s from the eval counters",
            "/api/tags" in lsrc and "eval_count" in lsrc and "eval_duration" in lsrc, "")
    if lm and bash:
        # a dead server is a recorded skip with the reason (the probe fails
        # before any CLI call), never a hang and never a zero
        _s = socket.socket()
        _s.bind(("127.0.0.1", 0))
        _port = _s.getsockname()[1]
        _s.close()
        d = tempfile.mkdtemp(prefix="bench-llm-")
        jl = os.path.join(d, "records.jsonl")
        env = dict(os.environ, BENCH_OLLAMA_URL="http://127.0.0.1:%d" % _port,
                   JSONL=jl, LOGDIR=d, PY=sys.executable, DO_LLM="1")
        r = subprocess.run([bash, "-s"], input=lm.group(1), capture_output=True,
                           text=True, timeout=90, env=env)
        rec = {}
        if os.path.isfile(jl):
            with open(jl) as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            rec = json.loads(lines[-1]) if lines else {}
        c.check("an absent Ollama server is a recorded skip with the reason",
                r.returncode == 0 and rec.get("result", {}).get("skipped") is True
                and "not responding" in rec.get("result", {}).get("reason", ""),
                (r.returncode, rec, r.stderr[:120]))
        # no models: a recorded skip with the reason instead of a zero
        py = re.search(r"<<'PYEOF'\n(.*?)\nPYEOF", lm.group(1), re.S)
        c.check("the Linux LLM python body is the one under test", bool(py), "")
        if py:
            d2 = tempfile.mkdtemp(prefix="bench-llm-empty-")
            os.makedirs(os.path.join(d2, "api"))
            with open(os.path.join(d2, "api", "tags"), "w") as fh:
                fh.write('{"models": []}')
            jl2 = os.path.join(d2, "records.jsonl")
            env2 = dict(os.environ, BENCH_OLLAMA_URL="file:///" + d2.replace(os.sep, "/"),
                        PATH="")
            r2 = subprocess.run([sys.executable, "-", jl2], input=py.group(1),
                                capture_output=True, text=True, timeout=60, env=env2)
            with open(jl2) as fh:
                rec2 = json.loads([ln for ln in fh.read().splitlines() if ln.strip()][-1])
            c.check("no Ollama models is a recorded skip with the reason, not a zero",
                    r2.returncode == 0 and rec2["result"].get("skipped") is True
                    and "no ollama models installed" in rec2["result"].get("reason", ""),
                    (r2.returncode, rec2, r2.stderr[:120]))
        else:
            skip("the Linux LLM no-models check", "no LLM python body found")
    else:
        skip("the Linux LLM failure-mode behaviour checks", "no bash here")

    # ---- the idle baseline: skippable, its own phase, idle_* in the summary ----
    # Every soak/headroom number had no floor: the machine at rest was never
    # captured. It now runs before the first load group on both platforms.
    c.check("the Linux runner captures an idle baseline by default",
            "DO_IDLE=1" in lsrc and "[idle] baseline" in lsrc, "")
    c.check("the idle baseline is skippable and tunable",
            re.search(r"--skip-idle\)\s*DO_IDLE=0", lsrc)
            and re.search(r"--idle-seconds\)\s*IDLE_SECONDS=", lsrc), "")
    c.check("the idle baseline runs before the first load group",
            "# >>> idle baseline >>>" in lsrc
            and lsrc.index("# >>> idle baseline >>>") < lsrc.index('if [ "$DO_GPU" = 1 ]'), "")
    c.check("the Linux summary carries the ambient values as idle_* fields",
            '"idle_" + k' in lsrc and "Idle baseline" in lsrc, "")
    m = re.search(r"# >>> idle baseline >>>\n(.*?)\n# <<< idle baseline <<<", lsrc, re.S)
    c.check("the shipped Linux idle block is the one under test", bool(m), "")
    if m and bash:
        d = tempfile.mkdtemp(prefix="bench-idle-")
        csvp = os.path.join(d, "t.telemetry.csv")
        with open(csvp, "w", newline="") as fh:
            fh.write("elapsed_s,temp_c,fan_pct,power_w,sm_mhz,cpu_package_c,cpu_mhz,cpu_pkg_w\n")
            fh.write("0.0,45.0,30.0,8.0,210,50.0,3000,20.0\n")
            fh.write("1.0,46.0,31.0,10.0,300,52.0,3100,22.0\n")
            fh.write("2.0,47.0,,,,,,\n")
        jl = os.path.join(d, "records.jsonl")
        env = dict(os.environ, DO_IDLE="1", IDLE_SECONDS="0", TELEMETRY=csvp,
                   JSONL=jl, PY=sys.executable)
        r = subprocess.run([bash, "-s"], input=m.group(1), capture_output=True,
                           text=True, timeout=60, env=env)
        with open(jl) as fh:
            rec = json.loads([ln for ln in fh.read().splitlines() if ln.strip()][-1])
        c.check("the idle phase records its own group and min/avg/max values",
                r.returncode == 0 and rec.get("group") == "idle"
                and rec.get("phase") == "baseline"
                and rec["result"]["temp_c"] == [45.0, 46.0, 47.0]
                and rec["result"]["fan_pct"] == [30.0, 30.5, 31.0]
                and rec["result"]["power_w"] == [8.0, 9.0, 10.0]
                and rec["result"]["sm_mhz"] == [210.0, 255.0, 300.0]
                and rec["result"]["cpu_temp_c"] == [50.0, 51.0, 52.0],
                (r.returncode, rec, r.stderr[:120]))
        c.check("a blank idle cell is skipped, not averaged as zero",
                rec.get("result", {}).get("temp_c_n") == 3
                and rec.get("result", {}).get("fan_pct_n") == 2, rec)
    else:
        skip("the Linux idle baseline behaviour check", "no bash here")

    # the Windows side: run_win.py captures the baseline itself (the battery
    # script is untouched) and writes the idle_* fields into its own report
    rwmod = _load("run_win_idle", os.path.join(BENCH, "run_win.py"))
    d = tempfile.mkdtemp(prefix="bench-win-idle-")
    csvp = os.path.join(d, "t.telemetry.csv")
    with open(csvp, "w", newline="") as fh:
        fh.write("elapsed_s,temp_c,fan_pct,power_w,sm_mhz,cpu_temp_c\r\n")
        fh.write("0.0,45.0,30,8.0,210,50.0\r\n")
        fh.write("1.0,46.0,31,10.0,300,52.0\r\n")
        fh.write("2.0,47.0,32,12.0,330,54.0")      # in-flight row: no newline yet
    st = rwmod.idle_stats_from_csv(csvp)
    c.check("run_win idle stats are min/avg/max with the in-flight row dropped",
            st["temp_c"] == [45.0, 45.5, 46.0] and st["fan_pct"] == [30.0, 30.5, 31.0]
            and st["power_w"] == [8.0, 9.0, 10.0] and st["sm_mhz"] == [210.0, 255.0, 300.0]
            and st["temp_c_n"] == 2 and "cpu_temp_c" in st, st)
    with open(csvp, "w", newline="") as fh:
        fh.write("elapsed_s,temp_c,fan_pct,power_w,sm_mhz,cpu_temp_c\r\n")
        fh.write("0.0,45.0,,8.0,210,50.0\r\n")
        fh.write("1.0,46.0,31,10.0,300,52.0\r\n")
    st2 = rwmod.idle_stats_from_csv(csvp)
    c.check("a blank Windows idle cell is skipped, not averaged as zero",
            st2["fan_pct"] == [31.0, 31.0, 31.0] and st2["fan_pct_n"] == 1
            and st2["temp_c_n"] == 2, st2)
    out = tempfile.mkdtemp(prefix="bench-win-idle-out-")
    rwmod.idle_baseline(csvp, out, "idlebox", "20260101-000000", 0)
    with open(os.path.join(out, "idlebox-win-20260101-000000.idle.json")) as fh:
        repj = json.load(fh)
    c.check("the Windows idle phase writes the idle_* summary fields",
            repj.get("group") == "idle" and repj.get("phase") == "baseline"
            and repj.get("idle_temp_c") == [45.0, 45.5, 46.0]
            and repj.get("idle_fan_pct") == [31.0, 31.0, 31.0]
            and repj.get("idle_power_w") == [8.0, 9.0, 10.0]
            and repj.get("idle_sm_mhz") == [210.0, 255.0, 300.0]
            and repj.get("idle_samples") == 2, repj)
    old_argv = sys.argv
    sys.argv = ["run_win.py", "--skip-idle", "--idle-seconds", "5"]
    try:
        wa = rwmod.parse_args()
    finally:
        sys.argv = old_argv
    c.check("run_win --skip-idle / --idle-seconds parse",
            wa.skip_idle is True and wa.idle_seconds == "5", (wa.skip_idle, wa.idle_seconds))
    c.check("run_win runs the idle baseline before the battery (source)",
            "if not a.skip_idle" in rw and "idle_baseline(csv_path" in rw, "")

    # ---- a bad remote start must not kill the runner ----
    # Both dashboards dispatched a remote start straight into _apply_profile/start with
    # no error handling, so a payload of the wrong shape (groups as a list while the
    # presets use {group: 1}) raised inside the command loop and the process exited -
    # both desktops went silent mid-command, no run row ever created.
    for f in ("gui.py", "tui.py"):
        with open(os.path.join(BENCH, f), encoding="utf-8") as fh:
            src = fh.read()
        i = src.index('if kind == "start":')
        branch = src[i:i + 700]
        c.check("%s fails a bad start payload instead of dying (source)" % f,
                "bad start payload" in branch and "except Exception as e" in branch
                and "return" in branch, "")

    # ---- the GPU engine counter must not spawn PowerShell per read ----
    # Measured: one Get-Counter call is ~2.5s, and the old code spawned one every 5s.
    with open(os.path.join(BENCH, "run_win.py"), encoding="utf-8") as fh:
        rw = fh.read()
    fn = rw[rw.index("def gpu_engine_thread"):]
    fn = fn[:fn.index("\ndef _fnum")]
    c.check("the engine counter loops inside one long-lived PowerShell child (source)",
            "while ($true)" in fn and "subprocess.Popen" in fn
            and "subprocess.run" not in fn, "")
    c.check("nvidia-smi is cached at 1 Hz, not 2",
            '"--gpu-interval", default="1.0"' in rw, "")

    # ---- an unreadable sensor must not be averaged as zero ----
    # Measure-Object counts a $null property as 0: {72, 73, $null} averaged 48.3.
    with open(os.path.join(BENCH, "Invoke-GpuBench.ps1"), encoding="utf-8") as fh:
        gsrc = fh.read()
    m = re.search(r"^function Get-TelemetryStats.*?^\}", gsrc, re.S | re.M)
    c.check("the shipped Get-TelemetryStats is the one under test", bool(m), "")
    if m and ps:
        head = m.group(0) + "\n"
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", head +
                            "$s = @([pscustomobject]@{util=90; temp=72; power=200; sm=1800; mem=8000; fan=40},"
                            "[pscustomobject]@{util=91; temp=73; power=201; sm=1810; mem=8010; fan=41},"
                            "[pscustomobject]@{util=$null; temp=$null; power=$null; sm=$null; mem=$null; fan=$null})\n"
                            "$r = Get-TelemetryStats $s\n"
                            "'{0}|{1}' -f $r.temp.avg, $r.temp.max"],
                           capture_output=True, text=True, timeout=120)
        c.check("an unreadable sample does not drag the average down",
                r.stdout.strip() == "72.5|73", (r.stdout.strip(), r.stderr.strip()[:140]))
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", head +
                            "$s = @([pscustomobject]@{util=90; temp=$null; power=200; sm=1800; mem=8000; fan=40})\n"
                            "$r = Get-TelemetryStats $s\n"
                            "if ($null -eq $r.temp) { 'OMITTED' } else { 'PRESENT' }"],
                           capture_output=True, text=True, timeout=120)
        c.check("a property with no readings is omitted, not reported as 0",
                r.stdout.strip() == "OMITTED", (r.stdout.strip(), r.stderr.strip()[:140]))
    else:
        print("  SKIP the telemetry-average behaviour check (no powershell/pwsh here)")

    # ---- the app runs from a per-user work dir, never the download folder ----
    # A surviving child (a telemetry PowerShell loop, an ollama server started by
    # the LLM phase) keeps its working directory open, which locks the folder the
    # user later wants to delete. The launcher chdirs, and the battery is spawned
    # there too, so the bundle folder is never any process's cwd.
    with open(os.path.join(BENCH, "benchreport.py"), encoding="utf-8") as fh:
        rep_src = fh.read()
    c.check("benchreport.work_dir exists", "def work_dir(" in rep_src, "")
    with open(os.path.join(REPO, "packaging", "launcher.py"), encoding="utf-8") as fh:
        launcher = fh.read()
    c.check("launcher runs the app from the per-user work dir",
            "work_dir()" in launcher and "os.chdir" in launcher, "")
    for f in ("gui.py", "tui.py", "agent.py"):
        with open(os.path.join(BENCH, f), encoding="utf-8") as fh:
            src = fh.read()
        c.check("%s spawns the battery from the work dir" % f, "cwd=work_dir()" in src, "")

    return c.fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if run() else 0)
