#!/usr/bin/env python3
"""NVENC encode benchmark for the bench-test toolkit (Linux port of Invoke-NvencBench.ps1),
using ffmpeg's NVENC encoders on synthetic 1080p frames.

One phase, `bench`: tries h264/hevc/av1 NVENC and reports encode fps; codecs the
GPU/driver cannot run are recorded as skipped.

    RESULT_JSON:{"phase":"bench", ...}
"""
import argparse
import json
import shutil
import subprocess
import time


def jline(obj):
    print("RESULT_JSON:" + json.dumps(obj), flush=True)


def encode_fps(codec, frames, width, height, timeout):
    if not shutil.which("ffmpeg"):
        return None, "ffmpeg not installed"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
           "-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=60" % (width, height),
           "-frames:v", str(frames), "-c:v", codec, "-f", "null", "-"]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    dt = time.time() - t0
    if r.returncode != 0:
        lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
        useful = [l for l in lines
                  if "nvenc" in l.lower() and any(k in l for k in
                                                  ("not support", "minimum required", "error", "invalid",
                                                   "failed", "could not"))]
        msg = useful[0] if useful else (lines[-1] if lines else "ffmpeg failed")
        return None, msg[:180]
    return round(frames / dt, 1), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["bench"])
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--frames", type=int, default=1200)
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()

    out = {"phase": "bench", "width": a.width, "height": a.height,
           "frames": a.frames, "results": {}}
    for codec in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
        fps, err = encode_fps(codec, a.frames, a.width, a.height, a.timeout)
        if fps is not None:
            out["results"][codec] = {"fps": fps}
            print("%s %.1f fps" % (codec, fps))
        else:
            out["results"][codec] = {"skipped": True, "reason": err}
            print("%s skipped (%s)" % (codec, err))
    jline(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
