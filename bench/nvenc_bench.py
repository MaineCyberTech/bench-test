#!/usr/bin/env python3
"""NVENC encode benchmark for the bench-test toolkit (Linux port of Invoke-NvencBench.ps1),
using ffmpeg on a pre-encoded 1080p60 source.

One phase, `bench`: detects the GPU vendor, tries the matching hardware encoder
family (nvidia: *_nvenc, amd: *_amf / *_vaapi, intel: *_qsv / *_vaapi) and
reports encode fps; codecs the GPU/driver cannot run are recorded as skipped.
The source clip is generated once, outside the timed runs, so the fps measures
the encode pipeline (decode + encode), not lavfi frame generation - a generator
input made every codec read ~512 fps because the generator was the limit. The
source, pipeline and timing method are recorded in the result.

    RESULT_JSON:{"phase":"bench", ...}
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

# vendor -> encoder families, most specific first (mirrors Invoke-NvencBench.ps1;
# the ffmpeg encoder list filters out the ones this build/platform cannot run)
CANDIDATES = {
    "nvidia": ["h264_nvenc", "hevc_nvenc", "av1_nvenc"],
    "amd": ["h264_amf", "hevc_amf", "av1_amf", "h264_vaapi", "hevc_vaapi", "av1_vaapi"],
    "intel": ["h264_qsv", "hevc_qsv", "av1_qsv", "h264_vaapi", "hevc_vaapi", "av1_vaapi"],
    "unknown": ["h264_nvenc", "hevc_nvenc", "h264_amf", "h264_qsv", "h264_vaapi"],
}


def jline(obj):
    print("RESULT_JSON:" + json.dumps(obj), flush=True)


def source(width, height):
    return "testsrc2=size=%dx%d:rate=60" % (width, height)


def gpu_vendor():
    """'nvidia'/'amd'/'intel'/'unknown' from the DRM sysfs vendor ids.

    Only trying *_nvenc meant an AMD/Intel box reported every codec skipped even
    with a working hardware encoder; the Windows script vendor-detects first.
    """
    ids = []
    for p in glob.glob("/sys/class/drm/card[0-9]*/device/vendor"):
        try:
            with open(p) as fh:
                ids.append(fh.read().strip().lower())
        except OSError:
            pass
    for vid, name in (("0x10de", "nvidia"), ("0x1002", "amd"), ("0x8086", "intel")):
        if vid in ids:
            return name
    if shutil.which("nvidia-smi"):
        return "nvidia"
    return "unknown"


def vaapi_device():
    nodes = sorted(glob.glob("/dev/dri/renderD*"))
    return nodes[0] if nodes else "/dev/dri/renderD128"


def encoder_args(codec):
    """Same settings as Invoke-NvencBench.ps1, so the platforms compare."""
    if codec.endswith("_nvenc"):
        return ["-c:v", codec, "-preset", "p5", "-b:v", "12M"]
    if codec.endswith("_amf"):
        return ["-c:v", codec, "-quality", "speed", "-usage", "transcoding", "-b:v", "12M"]
    if codec.endswith("_qsv"):
        return ["-c:v", codec, "-preset", "medium", "-b:v", "12M"]
    if codec.endswith("_vaapi"):
        return ["-vaapi_device", vaapi_device(), "-vf", "format=nv12,hwupload",
                "-c:v", codec, "-b:v", "12M"]
    return ["-c:v", codec, "-preset", "veryfast", "-b:v", "12M"]


def available_encoders():
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=60)
        return r.stdout + r.stderr
    except Exception:
        return ""


def make_source(path, frames, width, height, timeout):
    """Pre-encode the test clip once; the timed runs only decode+encode it."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "lavfi", "-i", source(width, height),
           "-frames:v", str(frames), "-c:v", "libx264", "-preset", "veryfast",
           "-b:v", "20M", "-y", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "source generation timed out"
    if r.returncode != 0 or not os.path.exists(path):
        lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
        return ("source generation failed: " + (lines[-1] if lines else "ffmpeg failed"))[:180]
    return None


def quality(codec, enc_args, frames, width, height, timeout):
    """SSIM + PSNR of an NVENC encode against the (deterministic) source.

    testsrc2 is reproducible, so the same filter regenerates the reference.
    """
    if not shutil.which("ffmpeg"):
        return None, "ffmpeg not installed"
    src = source(width, height)
    tmp = tempfile.mkdtemp()
    enc = os.path.join(tmp, "enc.mkv")
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-f", "lavfi", "-i", src, "-frames:v", str(frames)]
                           + enc_args + ["-y", enc],
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


def encode_fps(codec, src, frames, timeout):
    """(fps, error, timed_by) for one encode of the pre-generated source.

    -benchmark's rtime is the encode pipeline's own clock (decoding the source
    is part of it); the wall clock is the fallback. Frame generation is outside
    both - that is the fix: a lavfi input made the generator, not the encoder,
    the limit.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin", "-benchmark",
           "-i", src, "-frames:v", str(frames)] + encoder_args(codec) + ["-f", "null", "-"]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timed out", None
    wall = time.time() - t0
    if r.returncode != 0:
        lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
        useful = [l for l in lines
                  if any(k in l.lower() for k in
                         ("not support", "minimum required", "error", "invalid",
                          "failed", "could not", "no device", "cannot"))]
        msg = useful[0] if useful else (lines[-1] if lines else "ffmpeg failed")
        return None, msg[:180], None
    m = re.search(r"rtime=([\d.]+)", r.stderr)
    dt = float(m.group(1)) if m else wall
    return round(frames / dt, 1), None, ("ffmpeg rtime" if m else "wall")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["bench"])
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--frames", type=int, default=1200)
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()

    vendor = gpu_vendor()
    codecs = CANDIDATES.get(vendor) or CANDIDATES["unknown"]
    out = {"phase": "bench", "width": a.width, "height": a.height,
           "frames": a.frames, "vendor": vendor,
           "source": "testsrc2 %dx%d@60 pre-encoded with libx264" % (a.width, a.height),
           "pipeline": "decode source + encode to null muxer",
           "timing": "ffmpeg -benchmark rtime (wall fallback)",
           "results": {}}

    if not shutil.which("ffmpeg"):
        for codec in codecs:
            out["results"][codec] = {"skipped": True, "reason": "ffmpeg not installed"}
            print("%s skipped (ffmpeg not installed)" % codec)
        jline(out)
        return 0

    enc_list = available_encoders()
    codecs = [c for c in codecs if c in enc_list] or ["libx264"]
    tmp = tempfile.mkdtemp(prefix="bench_nvenc_")
    try:
        src = os.path.join(tmp, "src.mp4")
        src_err = make_source(src, a.frames, a.width, a.height, a.timeout)
        for codec in codecs:
            if src_err:
                out["results"][codec] = {"skipped": True, "reason": src_err}
                print("%s skipped (%s)" % (codec, src_err))
                continue
            fps, err, timed = encode_fps(codec, src, a.frames, a.timeout)
            if fps is not None:
                out["results"][codec] = {"fps": fps, "timed_by": timed}
                print("%s %.1f fps" % (codec, fps))
            else:
                out["results"][codec] = {"skipped": True, "reason": err}
                print("%s skipped (%s)" % (codec, err))
        # quality: SSIM/PSNR on a short clip per working codec (VMAF needs libvmaf)
        q_frames = max(60, min(a.frames, 180))
        for codec, res in out["results"].items():
            if "fps" not in res:
                continue
            q, err = quality(codec, encoder_args(codec), q_frames, a.width, a.height, a.timeout)
            if q:
                res.update(q)
                print("  %s quality ssim=%.4f psnr=%.1f dB" % (codec, q.get("ssim", 0), q.get("psnr", 0)))
            else:
                res["quality_error"] = err
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    jline(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
