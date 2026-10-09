#!/usr/bin/env python3
"""Portable GPU fan-curve tool for the bench-test toolkit.

NVIDIA: nvidia-settings (requires an X display and root, same as the repo's
        Windows nvfancontrol setup).
AMD:    amdgpu sysfs (pwm1_enable / pwm1), no X needed.

    fan_curve.py set [--curve "40:40 55:55 70:75 80:95"]   apply now + save config
    fan_curve.py run [--curve ...] [--interval 5]          stay resident (daemon)
    fan_curve.py off                                       restore automatic control
    fan_curve.py status                                    vendor, live temp/fan, config
    fan_curve.py check [--seconds 90]                      fan-response ramp test

The saved curve is written to /etc/bench-fan-curve.conf so run reports can record
which cooling profile was active.
"""
import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gpu_util

CONF = os.environ.get("BENCH_FAN_CONF", "/etc/bench-fan-curve.conf")
DEFAULT_CURVE = "40:40 55:55 70:75 80:95"
GPU_BENCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpu_bench.py")


def jline(obj):
    print("RESULT_JSON:" + json.dumps(obj), flush=True)


def parse_curve(text):
    pts = []
    for pair in text.replace(",", " ").split():
        t, _, p = pair.partition(":")
        pts.append((float(t), float(p)))
    pts.sort()
    if len(pts) < 2:
        raise ValueError("curve needs at least two temp:fan points")
    return pts


def interp(curve, temp):
    if temp <= curve[0][0]:
        return int(round(curve[0][1]))
    if temp >= curve[-1][0]:
        return int(round(curve[-1][1]))
    for (t0, p0), (t1, p1) in zip(curve, curve[1:]):
        if t0 <= temp <= t1:
            return int(round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0)))
    return int(round(curve[-1][1]))


# ------------------------------------------------------------------ NVIDIA --
def _nvidia_settings():
    for cand in ("/opt/nvidia-settings/usr/bin/nvidia-settings", "nvidia-settings"):
        if cand.startswith("/"):
            if os.path.exists(cand):
                return cand
        elif shutil.which(cand):
            return cand
    return None


def _ns_env():
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    for lib in ("/opt/nvidia-settings/usr/lib", "/usr/lib/nvidia"):
        if os.path.isdir(lib):
            env["LD_LIBRARY_PATH"] = lib + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def nvidia_apply(pct):
    ns = _nvidia_settings()
    if not ns:
        return False, "nvidia-settings not found"
    env = _ns_env()
    cmd = [ns, "-a", "GPUFanControlState=1", "-a", "GPUTargetFanSpeed=%d" % pct]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)
    if r.returncode != 0:
        # root needs X access to the user's display (Xwayland): grant once and retry
        try:
            subprocess.run(["xhost", "+SI:localuser:root"], capture_output=True, timeout=10, env=env)
        except Exception:
            pass
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        return False, (msg[-1] if msg else "nvidia-settings failed")
    return True, ""


def nvidia_restore():
    ns = _nvidia_settings()
    if not ns:
        return False, "nvidia-settings not found"
    r = subprocess.run([ns, "-a", "GPUFanControlState=0"], capture_output=True, text=True,
                       env=_ns_env(), timeout=15)
    return r.returncode == 0, (r.stderr or "").strip()


# --------------------------------------------------------------------- AMD --
def amd_sysfs():
    """(pwm1_enable, pwm1, fan1_input) paths for the first amdgpu card hwmon."""
    for card in gpu_util._amdgpu_cards():
        for hw in glob.glob(os.path.join(card, "device", "hwmon", "hwmon*")):
            pwm = os.path.join(hw, "pwm1")
            if os.path.exists(pwm):
                return (os.path.join(hw, "pwm1_enable"), pwm, os.path.join(hw, "fan1_input"))
    return None


def amd_write(path, value):
    with open(path, "w") as fh:
        fh.write(str(value))


def amd_apply(pct):
    paths = amd_sysfs()
    if not paths:
        return False, "amdgpu pwm1 not exposed"
    try:
        amd_write(paths[0], 1)                      # manual control
        amd_write(paths[1], max(0, min(255, int(round(pct / 100.0 * 255)))))
        return True, ""
    except OSError as exc:
        return False, str(exc)


def amd_restore():
    paths = amd_sysfs()
    if not paths:
        return False, "amdgpu pwm1 not exposed"
    try:
        amd_write(paths[0], 2)                      # automatic
        return True, ""
    except OSError as exc:
        return False, str(exc)


# ----------------------------------------------------------------- common ---
def apply_point(temp, pct):
    vendor = gpu_util.probe()
    if vendor == "nvidia":
        return nvidia_apply(pct)
    if vendor == "amd":
        return amd_apply(pct)
    return False, "no supported GPU found"


def restore():
    vendor = gpu_util.probe()
    if vendor == "nvidia":
        ok, err = nvidia_restore()
    elif vendor == "amd":
        ok, err = amd_restore()
    else:
        ok, err = False, "no supported GPU found"
    if ok:
        try:
            os.unlink(CONF)
        except OSError:
            pass
    return ok, err


def save_conf(curve_text):
    try:
        with open(CONF, "w") as fh:
            fh.write('CURVE="%s"\n' % curve_text)
    except OSError:
        pass


def load_conf():
    try:
        with open(CONF) as fh:
            for line in fh:
                if line.startswith("CURVE="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return None


def live():
    s = gpu_util.sample() or {}
    return {"vendor": gpu_util.probe(), "name": gpu_util.NAME, "temp_c": s.get("temp_c"),
            "fan_pct": s.get("fan_pct"), "fan_rpm": s.get("fan_rpm"), "power_w": s.get("power_w")}


# --------------------------------------------------------------- commands ---
def cmd_set(a):
    curve_text = a.curve or load_conf() or DEFAULT_CURVE
    curve = parse_curve(curve_text)
    temp = (gpu_util.sample() or {}).get("temp_c")
    pct = interp(curve, temp if temp is not None else curve[0][0])
    ok, err = apply_point(temp or 0, pct)
    if not ok:
        print("failed: %s" % err)
        jline({"cmd": "set", "ok": False, "error": err})
        return 1
    save_conf(curve_text)
    print("fan curve set: %s (temp %s -> %d%%)" % (curve_text, temp, pct))
    jline({"cmd": "set", "ok": True, "curve": curve_text, "temp_c": temp, "fan_pct": pct})
    return 0


def cmd_run(a):
    curve_text = a.curve or load_conf() or DEFAULT_CURVE
    curve = parse_curve(curve_text)
    save_conf(curve_text)
    print("fan curve daemon: %s (interval %ss)" % (curve_text, a.interval))
    last = None
    while True:
        s = gpu_util.sample() or {}
        temp = s.get("temp_c")
        if temp is not None:
            target = interp(curve, temp)
            if last is None or abs(target - last) >= 3:
                ok, _ = apply_point(temp, target)
                if ok:
                    last = target
        time.sleep(max(1, a.interval))


def cmd_off(_a):
    ok, err = restore()
    print("auto fan restored" if ok else "restore failed: %s" % err)
    jline({"cmd": "off", "ok": ok, "error": err})
    return 0 if ok else 1


def cmd_status(_a):
    info = live()
    conf = load_conf()
    print("gpu: %s (%s)" % (info["name"], info["vendor"]))
    print("temp: %s °C   fan: %s %%   rpm: %s   power: %s W" % (
        info["temp_c"], info["fan_pct"], info["fan_rpm"], info["power_w"]))
    print("curve config: %s" % (conf or "(none - automatic)"))
    jline({"cmd": "status", **info, "curve": conf})
    return 0


def cmd_check(a):
    """Ramp test: run a GPU load, sample temp + fan, verdict fan response."""
    start = live()
    samples = []
    load = subprocess.Popen([sys.executable, GPU_BENCH, "soak", "--size", str(a.size),
                             "--seconds", str(a.seconds), "--force"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    t0 = time.time()
    try:
        while load.poll() is None and time.time() - t0 < a.seconds + 30:
            s = live()
            samples.append((time.time() - t0, s.get("temp_c"), s.get("fan_pct"), s.get("fan_rpm")))
            time.sleep(2)
    finally:
        if load.poll() is None:
            os.killpg(os.getpgid(load.pid), signal.SIGTERM)
    temps = [s[1] for s in samples if s[1] is not None]
    fans = [s[2] for s in samples if s[2] is not None]
    rpms = [s[3] for s in samples if s[3] is not None]
    res = {"cmd": "check", "seconds": a.seconds, "vendor": start["vendor"],
           "temp_start": start["temp_c"], "temp_max": max(temps) if temps else None,
           "fan_start_pct": start["fan_pct"], "fan_max_pct": max(fans) if fans else None,
           "fan_max_rpm": max(rpms) if rpms else None}
    verdict = "inconclusive"
    if res["temp_max"] is not None and res["temp_start"] is not None:
        rose = res["temp_max"] - res["temp_start"]
        fan_rose = (res["fan_max_pct"] or 0) - (start["fan_pct"] or 0)
        rpm_rose = (res["fan_max_rpm"] or 0) - (start["fan_rpm"] or 0)
        if rose >= 4 and (fan_rose >= 8 or rpm_rose >= 200):
            verdict = "ok"
        elif rose >= 4 and res["temp_max"] >= 85:
            verdict = "thermal-limit"
        elif rose >= 4:
            verdict = "no-fan-response"
    res["verdict"] = verdict
    print("fan check: %s (temp %s -> %s °C, fan %s -> %s %%, rpm max %s)" % (
        verdict, res["temp_start"], res["temp_max"], res["fan_start_pct"], res["fan_max_pct"],
        res["fan_max_rpm"]))
    jline(res)
    return 0 if verdict in ("ok", "inconclusive") else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("set"); p.add_argument("--curve"); p.set_defaults(func=cmd_set)
    p = sub.add_parser("run"); p.add_argument("--curve"); p.add_argument("--interval", type=float, default=5)
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("off"); p.set_defaults(func=cmd_off)
    p = sub.add_parser("status"); p.set_defaults(func=cmd_status)
    p = sub.add_parser("check")
    p.add_argument("--seconds", type=int, default=90)
    p.add_argument("--size", type=int, default=8192)
    p.set_defaults(func=cmd_check)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
