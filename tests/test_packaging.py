"""Packaging tests - baked control-plane config for the app bundles.

Stdlib only, side-effect free: no PyInstaller, no network. We check that a
`bench-agent.conf` placed next to the toolkit (as build_app.py bakes it) is picked
up by load_conf(), and that write_agent_conf() writes it from the build env.
"""
import importlib.util
import os
import re
import shutil
import subprocess
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
        print("  SKIP the shipped-parse behaviour check (no powershell/pwsh on this host)")

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
        print("  SKIP the Run-LinuxBench guard behaviour check (no bash here)")

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
        snippet = (m.group(0) + "\n"
                   "try { Invoke-OllamaCli -Exe 'powershell' -CliArgs "
                   "@('-NoProfile','-Command','Start-Sleep -Seconds 30') -TimeoutSec 2; "
                   "'NO-TIMEOUT' } catch { 'TIMED-OUT' }")
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", snippet],
                           capture_output=True, text=True, timeout=180)
        c.check("a wedged CLI call times out instead of hanging",
                "TIMED-OUT" in r.stdout and "NO-TIMEOUT" not in r.stdout,
                r.stdout.strip()[-160:])
    else:
        print("  SKIP the bounded-CLI behaviour check (no powershell/pwsh on this host)")

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

    return c.fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if run() else 0)
