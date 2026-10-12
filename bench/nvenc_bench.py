#!/usr/bin/env python3
"""NVENC encode benchmark for the bench-test toolkit (Linux port of Invoke-NvencBench.ps1),
using ffmpeg's NVENC encoders on synthetic 1080p frames.

One phase, `bench`: tries h264/hevc/av1 NVENC and reports encode fps; codecs the
GPU/driver cannot run are recorded as skipped.

    RESULT_JSON:{"phase":"bench", ...}
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time


def jline(obj):
    print("RESULT_JSON:" + json.dumps(obj), flush=True)


def source(width, height):
    return "testsrc2=size=%dx%d:rate=60" % (width, height)


def quality(codec, frames, width, height, timeout):
    """SSIM + PSNR of an NVENC encode against the (deterministic) source.

    fftestsrc2 is reproducible, so the same filter regenerates the reference.
    """
    if not shutil.which("ffmpeg"):
        return None, "ffmpeg not installed"
    src = source(width, height)
    tmp = tempfile.mkdtemp()
    enc = os.path.join(tmp, "enc.mkv")
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-f", "lavfi", "-i", src, "-frames:v", str(frames),
                            "-c:v", codec, "-b:v", "8M", "-y", enc],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not os.path.exists(enc):
            return None, "encode failed"
        out = {}
        for filt, key, pat in (("ssim", "ssim", r"All:\s*([\d.]+)"),
                               ("psnr", "psnr", r"average:\s*([\d.]+)")):
            rr = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin",
                                 "-i", enc, "-f", "lavfi", "-i", src, "-frames:v", str(frames),
                                 "-lavfi", filt, "-f", "null", "-"],
                                capture_output=True, text=True, timeout=timeout)
            m = re.search(pat, rr.stderr)
            if m:
                out[key] = float(m.group(1))
        return (out or None), (None if out else "no ssim/psnr output")
    except Exception as exc:
        return None, str(exc)[:120]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    # quality: SSIM/PSNR on a short clip per working codec (VMAF needs libvmaf)
    q_frames = max(60, min(a.frames, 180))
    for codec, res in out["results"].items():
        if "fps" not in res:
            continue
        q, err = quality(codec, q_frames, a.width, a.height, a.timeout)
        if q:
            res.update(q)
            print("  %s quality ssim=%.4f psnr=%.1f dB" % (codec, q.get("ssim", 0), q.get("psnr", 0)))
        else:
            res["quality_error"] = err
    jline(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
