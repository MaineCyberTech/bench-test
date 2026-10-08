#!/usr/bin/env python3
"""GPU benchmark phases for the bench-test toolkit.

Each phase prints human-readable lines and a single machine-readable summary line:

    RESULT_JSON:{"phase":"...", ...}

Phases: info, matmul, membw, pcie, conv, vram, integrity, streams, soak
Requires PyTorch with CUDA (torch.cuda.is_available()).
"""
import argparse
import json
import sys
import time

import torch
import torch.nn.functional as F

DEV = "cuda"


def jline(obj):
    print("RESULT_JSON:" + json.dumps(obj), flush=True)


def timed(seconds, fn, flops=0, nbytes=0):
    fn()
    torch.cuda.synchronize()
    t0 = time.time()
    it = 0
    while time.time() - t0 < seconds:
        fn()
        it += 1
    torch.cuda.synchronize()
    dt = time.time() - t0
    out = {"iters": it, "seconds": round(dt, 2)}
    if flops:
        out["tflops"] = round(flops * it / dt / 1e12, 1)
    if nbytes:
        out["gbps"] = round(nbytes * it / dt / 1e9, 0)
    return out


def phase_info(_):
    p = torch.cuda.get_device_properties(0)
    info = {
        "phase": "info",
        "device": torch.cuda.get_device_name(0),
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "arch": list(torch.cuda.get_device_capability()),
        "sm_count": p.multi_processor_count,
        "vram_gib": round(p.total_memory / 1024 ** 3, 1),
        "cudnn": torch.backends.cudnn.version() if torch.backends.cudnn.is_available() else None,
    }
    print("device=%s torch=%s cuda=%s arch=%s SMs=%d VRAM=%.1fGiB" % (
        info["device"], info["torch"], info["cuda"], info["arch"], info["sm_count"], info["vram_gib"]))
    jline(info)


def phase_matmul(a):
    torch.backends.cuda.matmul.allow_tf32 = True
    n = a.size
    out = {"phase": "matmul", "n": n, "results": {}}
    for dtype, name, tf in [(torch.float16, "fp16", False), (torch.bfloat16, "bf16", False),
                            (torch.float32, "tf32", True), (torch.float32, "fp32", False)]:
        torch.backends.cuda.matmul.allow_tf32 = tf
        A = torch.randn(n, n, device=DEV, dtype=dtype)
        B = torch.randn(n, n, device=DEV, dtype=dtype)
        r = timed(a.seconds, lambda: A @ B, flops=2.0 * n ** 3)
        out["results"][name] = r
        print("matmul %-5s N=%d iters=%d TFLOPS=%.1f" % (name, n, r["iters"], r.get("tflops", 0)))
        del A, B
        torch.cuda.empty_cache()
    jline(out)


def phase_membw(a):
    out = {"phase": "membw", "results": {}}
    n = 256 * 1024 * 1024
    for dtype, name, sz in [(torch.float32, "fp32", 4), (torch.bfloat16, "bf16", 2)]:
        x = torch.randn(n, device=DEV, dtype=dtype)
        y = torch.randn(n, device=DEV, dtype=dtype)
        r_add = timed(a.seconds, lambda: x + y, nbytes=3 * n * sz)
        r_red = timed(a.seconds, lambda: x.sum(), nbytes=n * sz)
        out["results"][name] = {"add_gbps": r_add["gbps"], "reduce_gbps": r_red["gbps"]}
        print("membw %-5s add=%.0f GB/s reduce=%.0f GB/s" % (name, r_add["gbps"], r_red["gbps"]))
        del x, y
        torch.cuda.empty_cache()
    jline(out)


def phase_pcie(a):
    sz = 256 * 1024 * 1024  # 1 GiB fp32
    host = torch.empty(sz, dtype=torch.float32, pin_memory=True)
    devt = host.to(DEV, non_blocking=True)
    torch.cuda.synchronize()
    h2d = timed(a.seconds, lambda: host.to(DEV, non_blocking=True), nbytes=sz * 4)
    back = torch.empty(sz, dtype=torch.float32, pin_memory=True)
    d2h = timed(a.seconds, lambda: back.copy_(devt, non_blocking=True), nbytes=sz * 4)
    out = {"phase": "pcie", "h2d_gbps": h2d["gbps"], "d2h_gbps": d2h["gbps"]}
    print("pcie H2D=%.1f GB/s D2H=%.1f GB/s" % (h2d["gbps"], d2h["gbps"]))
    jline(out)


def phase_conv(a):
    if not torch.backends.cudnn.is_available():
        print("cudnn unavailable"); jline({"phase": "conv", "skipped": True}); return
    x = torch.randn(32, 128, 128, 128, device=DEV, dtype=torch.float16)
    w = torch.randn(256, 128, 3, 3, device=DEV, dtype=torch.float16)
    flops = 2.0 * 32 * 256 * (128 * 128) * (128 * 3 * 3)
    r = timed(a.seconds, lambda: F.conv2d(x, w, padding=1), flops=flops)
    out = {"phase": "conv", "tflops": r["tflops"], "iters": r["iters"]}
    print("conv2d fp16 TFLOPS=%.1f" % r["tflops"])
    jline(out)


def phase_vram(a):
    if not a.force and input("VRAM fill will use ~90% of free VRAM; continue? [y/N] ").lower() != "y":
        print("skipped"); jline({"phase": "vram", "skipped": True}); return
    free0, total = torch.cuda.mem_get_info()
    chunk = 256 * 1024 * 1024
    nt = int((free0 * 0.90) // (chunk * 4))
    ts = [torch.empty(chunk, dtype=torch.float32, device=DEV) for _ in range(nt)]
    for t in ts:
        t.fill_(1.0)
    torch.cuda.synchronize()
    bad = sum(1 for t in ts if abs(t.sum().item() - chunk) > chunk * 1e-3)
    ints = [torch.empty(chunk, dtype=torch.int32, device=DEV) for _ in range(nt)]
    C = 0x7F3A2C1B
    for t in ints:
        t.fill_(C)
    torch.cuda.synchronize()
    badbits = sum(int((t != C).sum().item()) for t in ints)
    out = {"phase": "vram", "chunks": nt, "gib": round(nt * chunk * 4 / 1024 ** 3, 1),
           "float_mismatched": bad, "int_bad_elements": badbits}
    print("VRAM filled=%.1fGiB mismatched=%d bad_elements=%d" % (out["gib"], bad, badbits))
    del ts, ints
    torch.cuda.empty_cache()
    jline(out)


def phase_integrity(a):
    phase_vram(a)


def phase_streams(a):
    if torch.cuda.get_device_capability(0) < (7, 0):
        print("streams skipped (pre-Volta GPU: multi-stream matmul hangs)")
        jline({"phase": "streams", "skipped": True, "reason": "pre-Volta GPU"})
        return
    N = a.size
    A = torch.randn(N, N, device=DEV, dtype=torch.bfloat16)
    B = torch.randn(N, N, device=DEV, dtype=torch.bfloat16)
    out = {"phase": "streams", "n": N, "results": {}}
    for ns in (1, 2, 4, 8):
        streams = [torch.cuda.Stream() for _ in range(ns)]
        for s in streams:
            with torch.cuda.stream(s):
                _ = A @ B
        torch.cuda.synchronize()
        t0 = time.time(); it = 0
        while time.time() - t0 < a.seconds:
            for s in streams:
                with torch.cuda.stream(s):
                    _ = A @ B; it += 1
        torch.cuda.synchronize()
        dt = time.time() - t0
        tf = 2.0 * N ** 3 * it / dt / 1e12
        out["results"][str(ns)] = round(tf, 1)
        print("streams=%d TFLOPS=%.1f" % (ns, tf))
    jline(out)


def phase_soak(a):
    if torch.cuda.get_device_capability(0) < (7, 0):
        print("soak skipped (pre-Volta GPU: matmul+conv loop hangs)")
        jline({"phase": "soak", "skipped": True, "reason": "pre-Volta GPU"})
        return
    N = min(a.size, 10240)
    A = torch.randn(N, N, device=DEV, dtype=torch.bfloat16)
    B = torch.randn(N, N, device=DEV, dtype=torch.bfloat16)
    w = torch.randn(128, 128, 3, 3, device=DEV, dtype=torch.float16)
    xi = torch.randn(32, 128, 128, 128, device=DEV, dtype=torch.float16)
    # Multi-stream matmul hangs on pre-Volta (Pascal) GPUs; run single-stream there.
    if torch.cuda.get_device_capability(0) >= (7, 0):
        streams = [torch.cuda.Stream() for _ in range(4)]
        t0 = time.time(); mats = convs = 0
        while time.time() - t0 < a.seconds:
            for s in streams:
                with torch.cuda.stream(s):
                    _ = A @ B; mats += 1
                    _ = F.conv2d(xi, w, padding=1); convs += 1
    else:
        t0 = time.time(); mats = convs = 0
        while time.time() - t0 < a.seconds:
            _ = A @ B; mats += 1
            _ = F.conv2d(xi, w, padding=1); convs += 1
    torch.cuda.synchronize()
    dt = time.time() - t0
    out = {"phase": "soak", "seconds": round(dt, 1), "matmuls": mats, "convs": convs,
           "matmul_tflops": round(2.0 * N ** 3 * mats / dt / 1e12, 1),
           "peak_vram_gib": round(torch.cuda.max_memory_allocated() / 1024 ** 3, 1)}
    print("soak %.1fs matmuls=%d convs=%d matmul_TFLOPS=%.1f" % (
        dt, mats, convs, out["matmul_tflops"]))
    jline(out)


PHASES = {
    "info": phase_info, "matmul": phase_matmul, "membw": phase_membw, "pcie": phase_pcie,
    "conv": phase_conv, "vram": phase_vram, "integrity": phase_integrity,
    "streams": phase_streams, "soak": phase_soak,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=sorted(PHASES))
    ap.add_argument("--seconds", type=float, default=12.0, help="per-measurement seconds")
    ap.add_argument("--size", type=int, default=12288, help="matrix size N")
    ap.add_argument("--force", action="store_true", help="skip VRAM-fill confirmation")
    a = ap.parse_args()
    if not torch.cuda.is_available():
        print("ERROR: torch.cuda not available", file=sys.stderr)
        return 2
    PHASES[a.phase](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
