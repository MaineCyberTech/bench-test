#!/usr/bin/env python3
"""Vendor-agnostic GPU helpers for the bench-test toolkit.

NVIDIA: nvidia-smi.
AMD:    amd-smi (if present) -> rocm-smi -> amdgpu sysfs fallback (no ROCm needed).

Used by the telemetry logger and the GPU soak sampler; dependency-free. All sample
values are best-effort: any field the vendor cannot report comes back as None.

    import gpu_util
    gpu_util.probe()   # 'nvidia' | 'amd' | None
    gpu_util.sample()  # {'util': ..., 'temp_c': ..., ...}
"""
import glob
import json
import os
import shutil
import subprocess
import time

FIELDS = ("util", "mem_util", "temp_c", "fan_pct", "power_w",
          "sm_mhz", "mem_mhz", "vram_used_mib", "pstate", "fan_rpm")

VENDOR = None
NAME = ""
VRAM_TOTAL_MIB = None
_PROBED = False
_CACHE = {"t": 0.0, "v": None}


def _run(args, timeout=5):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _num(value):
    """Convert vendor strings like '45.0', '300Mhz', '[N/A]' to float/None."""
    if value is None:
        return None
    try:
        return float(str(value).replace("Mhz", "").replace("MHz", "").replace("W", "").strip())
    except ValueError:
        return None


def probe():
    global VENDOR, NAME, VRAM_TOTAL_MIB, _PROBED
    if _PROBED:
        return VENDOR
    _PROBED = True
    out = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    if out and out.strip():
        VENDOR, NAME = "nvidia", out.strip().splitlines()[0].strip()
        vram = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
        if vram:
            VRAM_TOTAL_MIB = _num(vram.strip().splitlines()[0])
        return VENDOR
    if shutil.which("amd-smi") or shutil.which("rocm-smi"):
        VENDOR = "amd"
        out = _run(["rocm-smi", "--showproductname", "--csv"]) or ""
        for line in out.splitlines():
            if line.lower().startswith("card") and "," in line:
                NAME = line.split(",", 1)[1].strip().strip('"')
                break
        if not NAME:
            NAME = "AMD GPU"
        return VENDOR
    for card in _amdgpu_cards():
        VENDOR = "amd"
        NAME = _read(os.path.join(card, "device", "product_name")) or "AMD GPU"
        total = _num(_read(os.path.join(card, "device", "mem_info_vram_total")))
        if total:
            VRAM_TOTAL_MIB = round(total / 1024 ** 2, 1)
        return VENDOR
    return None


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return ""


def _amdgpu_cards():
    cards = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        if _read(os.path.join(card, "device", "vendor")).lower() == "0x1002":
            cards.append(card)
    return cards


# --- NVIDIA -----------------------------------------------------------------

def _nvidia_settings_bin():
    for cand in ("/opt/nvidia-settings/usr/bin/nvidia-settings", shutil.which("nvidia-settings")):
        if cand and os.path.exists(cand):
            return cand
    return None


def nvidia_fan_rpm():
    ns = _nvidia_settings_bin()
    if not ns or not os.environ.get("DISPLAY"):
        return None
    env = os.environ.copy()
    for lib in ("/opt/nvidia-settings/usr/lib", "/usr/lib/nvidia"):
        if os.path.isdir(lib):
            env["LD_LIBRARY_PATH"] = lib
    try:
        r = subprocess.run([ns, "-q", "GPUCurrentFanSpeedRPM", "-t"], capture_output=True,
                           text=True, timeout=5, env=env)
        return float(r.stdout.strip().splitlines()[-1])
    except Exception:
        return None


def _nvidia_sample():
    out = _run(["nvidia-smi",
                "--query-gpu=utilization.gpu,utilization.memory,temperature.gpu,fan.speed,"
                "power.draw,clocks.sm,clocks.mem,memory.used,pstate",
                "--format=csv,noheader,nounits"])
    if not out:
        return None
    vals = [v.strip() for v in out.splitlines()[0].split(",")]
    if len(vals) != 9:
        return None
    return {"util": _num(vals[0]), "mem_util": _num(vals[1]), "temp_c": _num(vals[2]),
            "fan_pct": _num(vals[3]), "power_w": _num(vals[4]), "sm_mhz": _num(vals[5]),
            "mem_mhz": _num(vals[6]), "vram_used_mib": _num(vals[7]), "pstate": vals[8],
            "fan_rpm": nvidia_fan_rpm()}


# --- AMD --------------------------------------------------------------------

def _amd_smi_sample():
    out = _run(["amd-smi", "metric", "--json"])
    if not out:
        return None
    try:
        data = json.loads(out)
        entry = data["gpu_data"][0] if isinstance(data, dict) and "gpu_data" in data else data[0]
    except Exception:
        return None

    def dig(*keys):
        node = entry
        for k in keys:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                return None
        return node

    clock = dig("clock") or {}
    gfx_clk = None
    for k, v in clock.items():
        if k.startswith("gfx") and isinstance(v, dict):
            gfx_clk = v.get("clk")
            break
    return {"util": _num(dig("usage", "gfx_activity")),
            "mem_util": _num(dig("usage", "memory_activity")),
            "temp_c": _num(dig("temperature", "edge")),
            "fan_pct": _num(dig("fan", "speed")),
            "power_w": _num(dig("power", "socket_power") or dig("power", "average_socket_power")),
            "sm_mhz": _num(gfx_clk), "mem_mhz": None,
            "vram_used_mib": _num(dig("mem_usage", "used")),
            "pstate": None, "fan_rpm": _num(dig("fan", "rpm"))}


def _rocm_smi_sample():
    out = _run(["rocm-smi", "--showuse", "--showtemp", "--showfan", "--showpower",
                "--showclocks", "--showmemuse", "--showmeminfo", "vram", "--json"])
    if not out:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    cards = [v for k, v in data.items() if str(k).lower().startswith("card") and isinstance(v, dict)]
    if not cards:
        return None
    card = cards[0]

    def find(*fragments):
        for key, val in card.items():
            kl = key.lower()
            if all(f in kl for f in fragments):
                return val
        return None

    def mhz(value):
        return _num(str(value).replace("(", "").replace(")", "")) if value is not None else None

    return {"util": _num(find("gpu use")),
            "mem_util": _num(find("gpu memory use")),
            "temp_c": _num(find("temperature", "edge")) or _num(find("temperature", "junction")),
            "fan_pct": _num(find("fan speed")),
            "power_w": _num(find("power")),
            "sm_mhz": mhz(find("sclk")),
            "mem_mhz": mhz(find("mclk")),
            "vram_used_mib": (lambda b: round(b / 1024 ** 2, 1) if b else None)(_num(find("used memory"))) or None,
            "pstate": None, "fan_rpm": _num(find("fan", "rpm"))}


def _amdgpu_sysfs_sample():
    cards = _amdgpu_cards()
    if not cards:
        return None
    card = cards[0]
    dev = os.path.join(card, "device")

    temp = fan = power = None
    for hw in glob.glob(os.path.join(dev, "hwmon", "hwmon*")):
        if temp is None:
            temp = _num(_read(os.path.join(hw, "temp1_input")))
            temp = temp / 1000.0 if temp is not None else None
        if fan is None:
            pwm = _num(_read(os.path.join(hw, "pwm1")))
            if pwm is not None:
                fan = round(pwm / 255.0 * 100.0, 1)
        if power is None:
            power = _num(_read(os.path.join(hw, "power1_average")))
            power = power / 1e6 if power is not None else None

    def active_clock(path):
        for line in _read(os.path.join(dev, path)).splitlines():
            if "*" in line:
                return _num(line.split(":", 1)[1]) if ":" in line else None
        return None

    vram_used = _num(_read(os.path.join(dev, "mem_info_vram_used")))
    rpm = None
    for hw in glob.glob(os.path.join(dev, "hwmon", "hwmon*")):
        rpm = _num(_read(os.path.join(hw, "fan1_input")))
        if rpm is not None:
            break
    return {"util": _num(_read(os.path.join(dev, "gpu_busy_percent"))),
            "mem_util": None, "temp_c": temp, "fan_pct": fan, "power_w": power,
            "sm_mhz": active_clock("pp_dpm_sclk"), "mem_mhz": active_clock("pp_dpm_mclk"),
            "vram_used_mib": round(vram_used / 1024 ** 2, 1) if vram_used else None,
            "pstate": None, "fan_rpm": rpm}


def sample(cache_seconds=0.75):
    """Current GPU sample (vendor-dispatched). Cached briefly: cheap for UIs."""
    now = time.time()
    if _CACHE["v"] is not None and now - _CACHE["t"] < cache_seconds:
        return _CACHE["v"]
    vendor = probe()
    if vendor == "nvidia":
        result = _nvidia_sample()
    elif vendor == "amd":
        result = _amd_smi_sample() or _rocm_smi_sample() or _amdgpu_sysfs_sample()
    else:
        result = None
    _CACHE.update(t=now, v=result)
    return result


if __name__ == "__main__":
    print(json.dumps({"vendor": probe(), "name": NAME, "sample": sample()}, indent=2))
