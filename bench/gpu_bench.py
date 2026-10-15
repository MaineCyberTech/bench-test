#!/usr/bin/env python3
"""GPU benchmark phases for the bench-test toolkit.

Each phase prints human-readable lines and a single machine-readable summary line:

    RESULT_JSON:{"phase":"...", ...}

Phases: info, matmul, membw, pcie, conv, vram, integrity, streams, soak

Backends (auto-detected, or forced with --backend):
  * cuda - PyTorch + CUDA (NVIDIA)
  * dml  - ONNX Runtime DirectML (AMD / Intel / NVIDIA on Windows)
  * cpu  - PyTorch CPU (last-resort fallback)

Only the standard library is imported at module load: torch / numpy / onnxruntime
are imported lazily by the backend that needs them, so `import gpu_bench` works on
the live USB's system Python (the plateau logic is unit-tested that way).
"""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time


def jline(obj):
    print("RESULT_JSON:" + json.dumps(obj), flush=True)


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- torch
_TORCH = None


def torch():
    global _TORCH
    if _TORCH is None:
        import torch as _t
        _TORCH = _t
    return _TORCH


def _sync(dev):
    if dev == "cuda":
        torch().cuda.synchronize()


def _empty_cache(dev):
    if dev == "cuda":
        torch().cuda.empty_cache()


def timed_torch(dev, seconds, fn, flops=0, nbytes=0):
    fn()
    _sync(dev)
    t0 = time.time()
    it = 0
    while time.time() - t0 < seconds:
        fn()
        # Sync every iteration: launches outpace a slow GPU by orders of
        # magnitude (pre-Volta Maxwell: a 10 s unsynced loop queues hours of
        # matmuls and the phase looks like it hangs). Negligible overhead for the
        # multi-millisecond ops these phases measure.
        _sync(dev)
        it += 1
    dt = time.time() - t0
    out = {"iters": it, "seconds": round(dt, 2)}
    if flops:
        out["tflops"] = round(flops * it / dt / 1e12, 1)
    if nbytes:
        out["gbps"] = round(nbytes * it / dt / 1e9, 0)
    return out


def torch_info(a):
    t = torch()
    if a.device == "cuda":
        p = t.cuda.get_device_properties(0)
        info = {
            "phase": "info", "backend": "cuda", "device": t.cuda.get_device_name(0),
            "torch": t.__version__, "cuda": t.version.cuda,
            "arch": list(t.cuda.get_device_capability()), "sm_count": p.multi_processor_count,
            "vram_gib": round(p.total_memory / 1024 ** 3, 1),
            "cudnn": t.backends.cudnn.version() if t.backends.cudnn.is_available() else None,
        }
        log("device=%s torch=%s cuda=%s arch=%s SMs=%d VRAM=%.1fGiB" % (
            info["device"], info["torch"], info["cuda"], info["arch"], info["sm_count"], info["vram_gib"]))
    else:
        info = {
            "phase": "info", "backend": "cpu", "device": "CPU (torch)", "torch": t.__version__,
            "cuda": None, "arch": None, "sm_count": None, "vram_gib": None, "cudnn": None,
        }
        log("device=%s torch=%s (cpu)" % (info["device"], info["torch"]))
    jline(info)


def torch_matmul(a):
    t = torch()
    dev = a.device
    n = a.size if dev == "cuda" else min(a.size, 4096)
    out = {"phase": "matmul", "backend": dev, "n": n, "results": {}}
    combos = ([(t.float16, "fp16", False), (t.bfloat16, "bf16", False),
               (t.float32, "tf32", True), (t.float32, "fp32", False),
               (t.float64, "fp64", False)] if dev == "cuda"
              else [(t.float32, "fp32", False), (t.float64, "fp64", False)])
    for dtype, name, tf in combos:
        if dev == "cuda":
            t.backends.cuda.matmul.allow_tf32 = tf
        A = t.randn(n, n, device=dev, dtype=dtype)
        B = t.randn(n, n, device=dev, dtype=dtype)
        r = timed_torch(dev, a.seconds, lambda: A @ B, flops=2.0 * n ** 3)
        out["results"][name] = r
        log("matmul %-5s N=%d iters=%d TFLOPS=%.1f" % (name, n, r["iters"], r.get("tflops", 0)))
        del A, B
        _empty_cache(dev)
    jline(out)


def torch_membw(a):
    t = torch()
    dev = a.device
    out = {"phase": "membw", "backend": dev, "results": {}}
    n = 256 * 1024 * 1024 if dev == "cuda" else 32 * 1024 * 1024
    for dtype, name, sz in [(t.float32, "fp32", 4), (t.bfloat16, "bf16", 2)]:
        x = t.randn(n, device=dev, dtype=dtype)
        y = t.randn(n, device=dev, dtype=dtype)
        r_add = timed_torch(dev, a.seconds, lambda: x + y, nbytes=3 * n * sz)
        r_red = timed_torch(dev, a.seconds, lambda: x.sum(), nbytes=n * sz)
        out["results"][name] = {"add_gbps": r_add["gbps"], "reduce_gbps": r_red["gbps"]}
        log("membw %-5s add=%.0f GB/s reduce=%.0f GB/s" % (name, r_add["gbps"], r_red["gbps"]))
        del x, y
        _empty_cache(dev)
    jline(out)


def torch_pcie(a):
    t = torch()
    dev = a.device
    if dev != "cuda":
        log("pcie: not applicable on %s backend" % dev)
        jline({"phase": "pcie", "backend": dev, "skipped": True})
        return
    sz = 256 * 1024 * 1024  # 1 GiB fp32
    host = t.empty(sz, dtype=t.float32, pin_memory=True)
    devt = host.to(dev, non_blocking=True)
    t.cuda.synchronize()
    h2d = timed_torch(dev, a.seconds, lambda: host.to(dev, non_blocking=True), nbytes=sz * 4)
    back = t.empty(sz, dtype=t.float32, pin_memory=True)
    d2h = timed_torch(dev, a.seconds, lambda: back.copy_(devt, non_blocking=True), nbytes=sz * 4)
    out = {"phase": "pcie", "backend": dev, "h2d_gbps": h2d["gbps"], "d2h_gbps": d2h["gbps"]}
    log("pcie H2D=%.1f GB/s D2H=%.1f GB/s" % (h2d["gbps"], d2h["gbps"]))
    jline(out)


def torch_conv(a):
    t = torch()
    import torch.nn.functional as F
    dev = a.device
    if dev != "cuda" or not t.backends.cudnn.is_available():
        log("conv: skipped on %s" % dev)
        jline({"phase": "conv", "backend": dev, "skipped": True})
        return
    x = t.randn(32, 128, 128, 128, device=dev, dtype=t.float16)
    w = t.randn(256, 128, 3, 3, device=dev, dtype=t.float16)
    flops = 2.0 * 32 * 256 * (128 * 128) * (128 * 3 * 3)
    r = timed_torch(dev, a.seconds, lambda: F.conv2d(x, w, padding=1), flops=flops)
    log("conv2d fp16 TFLOPS=%.1f" % r["tflops"])
    jline({"phase": "conv", "backend": dev, "tflops": r["tflops"], "iters": r["iters"]})


def torch_integrity(a):
    t = torch()
    dev = a.device
    if dev == "cuda":
        free0, _total = t.cuda.mem_get_info()
        chunk = 256 * 1024 * 1024
        nt = int((free0 * 0.90) // (chunk * 4))
    else:
        chunk = 128 * 1024 * 1024
        nt = 4
    nt = max(1, nt)
    ts = [t.empty(chunk, dtype=t.float32, device=dev) for _ in range(nt)]
    for x in ts:
        x.fill_(1.0)
    _sync(dev)
    bad = sum(1 for x in ts if abs(x.sum().item() - chunk) > chunk * 1e-3)
    # Free the fp32 pass before the int32 pass (together they need ~2x the fill
    # size and OOM on small-VRAM GPUs). The fill loop's last `x` holds a chunk too.
    del ts, x
    _empty_cache(dev)
    ints = [t.empty(chunk, dtype=t.int32, device=dev) for _ in range(nt)]
    C = 0x7F3A2C1B
    for x in ints:
        x.fill_(C)
    _sync(dev)
    # Verify in slices: (x != C).sum() can materialize a multi-GiB temporary.
    badbits = 0
    for x in ints:
        for s in x.split(32 * 1024 * 1024):
            badbits += int((s != C).sum().item())
    out = {"phase": "integrity", "backend": dev, "chunks": nt,
           "gib": round(nt * chunk * 4 / 1024 ** 3, 1),
           "float_mismatched": bad, "int_bad_elements": badbits}
    log("integrity filled=%.1fGiB mismatched=%d bad_elements=%d" % (out["gib"], bad, badbits))
    del ints, x
    _empty_cache(dev)
    jline(out)


def torch_streams(a):
    t = torch()
    dev = a.device
    if dev != "cuda" or t.cuda.get_device_capability(0) < (7, 0):
        reason = "cpu backend" if dev == "cpu" else "pre-Volta GPU"
        log("streams skipped (%s)" % reason)
        jline({"phase": "streams", "backend": dev, "skipped": True, "reason": reason})
        return
    N = a.size
    A = t.randn(N, N, device=dev, dtype=t.bfloat16)
    B = t.randn(N, N, device=dev, dtype=t.bfloat16)
    out = {"phase": "streams", "backend": dev, "n": N, "results": {}}
    for ns in (1, 2, 4, 8):
        streams = [t.cuda.Stream() for _ in range(ns)]
        for s in streams:
            with t.cuda.stream(s):
                _ = A @ B
        t.cuda.synchronize()
        t0 = time.time(); it = 0
        while time.time() - t0 < a.seconds:
            for s in streams:
                with t.cuda.stream(s):
                    _ = A @ B; it += 1
        t.cuda.synchronize()
        dt = time.time() - t0
        tf = 2.0 * N ** 3 * it / dt / 1e12
        out["results"][str(ns)] = round(tf, 1)
        log("streams=%d TFLOPS=%.1f" % (ns, tf))
    jline(out)


# --- telemetry (soak) -------------------------------------------------------

def _telemetry_loop(samples, stop):
    have_smi = shutil.which("nvidia-smi") is not None
    while not stop.wait(2.0):
        row = None
        if have_smi:
            try:
                r = subprocess.run(
                    ["nvidia-smi", "--query-gpu=temperature.gpu,fan.speed,power.draw,clocks.sm",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5)
                vals = [float(v.strip()) for v in r.stdout.strip().splitlines()[0].split(",")]
                if len(vals) == 4:
                    row = vals
            except Exception:
                row = None
        if row is None and os.name != "nt":
            # Vendor-agnostic fallback (AMD via amd-smi/rocm-smi/sysfs). Windows is
            # excluded on purpose: amd-smi.exe there churns the Virtual Disk Service
            # (a visible "disk" popup every couple of seconds), and the PowerShell
            # runner already supplies telemetry via GPU Engine counters.
            try:
                import gpu_util
                s = gpu_util.sample()
                if s and s.get("temp_c") is not None:
                    row = [s.get("temp_c") or 0, s.get("fan_pct") or 0,
                           s.get("power_w") or 0, s.get("sm_mhz") or 0]
            except Exception:
                row = None
        if row is not None:
            samples.append(row)


def _telemetry_summary(samples):
    if not samples:
        return None
    cols = list(zip(*samples))
    return {
        "samples": len(samples),
        "temp_c": [min(cols[0]), round(statistics.mean(cols[0]), 1), max(cols[0])],
        "fan_pct": [min(cols[1]), round(statistics.mean(cols[1]), 1), max(cols[1])],
        "power_w": [round(statistics.mean(cols[2]), 1), max(cols[2])],
        "sm_mhz": [round(statistics.mean(cols[3]))],
    }


def _temp_stable(samples, window=8, tol=0.5):
    """True when the GPU temperature has plateaued.

    Temperatures settle onto a plateau rather than stopping dead, so compare the
    mean of the older half of the window with the newer half: under `tol` degrees
    of drift means stable. The window is 2s-spaced samples (~16s here).
    """
    if len(samples) < window:
        return False
    recent = [s[0] for s in samples[-window:]]
    half = len(recent) // 2
    return abs(statistics.mean(recent[half:]) - statistics.mean(recent[:half])) < tol


def _soak_decision(t0, a, samples, stable_since, now=None):
    """Return ``(keep_going, stable_since)`` for one soak iteration.

    Stops at the ceiling (``--seconds``), or once the temperature has *held*
    steady for ``--stable-hold`` seconds after it first plateaued — a plateau
    alone is not enough, we want a full minute of it (never before the minimum).
    ``stable_since`` is the wall clock when the plateau was first seen; it resets
    if the temperature starts moving again, so the hold must be continuous.
    """
    now = time.time() if now is None else now
    el = now - t0
    if el >= a.seconds:
        return False, stable_since
    if not getattr(a, "until_stable", False):
        return True, None
    if el < min(45.0, a.seconds):
        return True, None
    if not _temp_stable(samples):
        return True, None
    if stable_since is None:
        stable_since = now
    if now - stable_since >= getattr(a, "stable_hold", 60.0):
        return False, stable_since
    return True, stable_since


def torch_soak(a):
    t = torch()
    import torch.nn.functional as F
    dev = a.device
    N = min(a.size, 10240) if dev == "cuda" else min(a.size, 2048)
    A = t.randn(N, N, device=dev, dtype=t.bfloat16)
    B = t.randn(N, N, device=dev, dtype=t.bfloat16)
    w = t.randn(128, 128, 3, 3, device=dev, dtype=t.float16)
    xi = t.randn(32, 128, 128, 128, device=dev, dtype=t.float16)
    samples, stop = [], threading.Event()
    tel = threading.Thread(target=_telemetry_loop, args=(samples, stop), daemon=True)
    tel.start()
    t0 = time.time(); mats = convs = 0
    stable_since = None
    if dev == "cuda" and t.cuda.get_device_capability(0) >= (7, 0):
        streams = [t.cuda.Stream() for _ in range(4)]
        while True:
            keep, stable_since = _soak_decision(t0, a, samples, stable_since)
            if not keep:
                break
            for s in streams:
                with t.cuda.stream(s):
                    _ = A @ B; mats += 1
                    _ = F.conv2d(xi, w, padding=1); convs += 1
    else:
        # Single-stream (cpu or pre-Volta); sync each iteration to bound the queue.
        while True:
            keep, stable_since = _soak_decision(t0, a, samples, stable_since)
            if not keep:
                break
            _ = A @ B; mats += 1
            _ = F.conv2d(xi, w, padding=1); convs += 1
            _sync(dev)
    _sync(dev)
    dt = time.time() - t0
    stop.set(); tel.join(timeout=5)
    # The loop runs a conv2d alongside every matmul, so a matmul-only count
    # understates throughput - badly at small N, where the conv is comparable work
    # (~half of it at 4096, ~7% at 10240). Report both, plus the size: the TFLOPS is
    # only comparable between runs that used the same N.
    mm_flops = 2.0 * N ** 3 * mats
    conv_flops = 2.0 * 32 * 128 * 128 * 128 * (128 * 3 * 3)
    out = {"phase": "soak", "backend": dev, "seconds": round(dt, 1), "matmuls": mats, "convs": convs,
           "size": N,
           "matmul_tflops": round(mm_flops / dt / 1e12, 1),
           "total_tflops": round((mm_flops + conv_flops * convs) / dt / 1e12, 1),
           "peak_vram_gib": round(t.cuda.max_memory_allocated() / 1024 ** 3, 1) if dev == "cuda" else None,
           "until_stable": bool(getattr(a, "until_stable", False)),
           "stable": stable_since is not None,
           "stable_at_s": (round(stable_since - t0, 1) if stable_since is not None else None),
           "stable_hold_s": float(getattr(a, "stable_hold", 60.0))}
    ts = _telemetry_summary(samples)
    if ts:
        out["telemetry"] = ts
    log("soak %.1fs matmuls=%d convs=%d N=%d matmul_TFLOPS=%.1f total_TFLOPS=%.1f%s" % (
        dt, mats, convs, N, out["matmul_tflops"], out["total_tflops"],
        (" stable@%.0fs+%.0fs" % (out["stable_at_s"], out["stable_hold_s"]))
        if out["stable"] else ""))
    jline(out)


TORCH_PHASES = {
    "info": torch_info, "matmul": torch_matmul, "membw": torch_membw, "pcie": torch_pcie,
    "conv": torch_conv, "vram": torch_integrity, "integrity": torch_integrity,
    "streams": torch_streams, "soak": torch_soak,
}


# --------------------------------------------------------------------------- DirectML
def _np():
    import numpy
    return numpy


def _dml_np_dt(npdt):
    import numpy as np
    from onnx import TensorProto
    return {np.float32: TensorProto.FLOAT, np.float16: TensorProto.FLOAT16,
            np.int32: TensorProto.INT32}[npdt]


def _dml_session(nodes, inputs, outputs, opset=17):
    import onnx
    from onnx import helper
    import onnxruntime as ort
    graph = helper.make_graph(nodes, "g", inputs, outputs)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", opset)])
    model.ir_version = 9
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.log_severity_level = 3
    return ort.InferenceSession(model.SerializeToString(), so,
                                providers=["DmlExecutionProvider", "CPUExecutionProvider"])


def _dml_tvi(name, npdt, shape):
    from onnx import helper
    return helper.make_tensor_value_info(name, _dml_np_dt(npdt), list(shape))


def _dml_dev(arr):
    import onnxruntime as ort
    return ort.OrtValue.ortvalue_from_numpy(arr, "dml", 0)


def _dml_devout(shape, npdt):
    import onnxruntime as ort
    return ort.OrtValue.ortvalue_from_shape_and_type(list(shape), npdt, "dml", 0)


def timed_dml(sess, io, seconds, batch=20):
    """Time a DirectML session. DML executes asynchronously, so runs are submitted
    in batches and a device->host readback is used to synchronize each batch."""
    io.get_outputs()[0].numpy()  # warm-up + sync
    t0 = time.time(); it = 0
    while time.time() - t0 < seconds:
        for _ in range(batch):
            sess.run_with_iobinding(io)
        io.get_outputs()[0].numpy()  # sync
        it += batch
    return it, time.time() - t0


def _win_gpu_info():
    """Best-effort (name, vram_bytes) for the primary display adapter on Windows."""
    name = vram = None
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000")
        try:
            name = winreg.QueryValueEx(key, "DriverDesc")[0]
        except OSError:
            pass
        for prop in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
            try:
                vram = int(winreg.QueryValueEx(key, prop)[0]); break
            except OSError:
                continue
        winreg.CloseKey(key)
    except Exception:
        pass
    return name, vram


def dml_info(a):
    import onnxruntime as ort
    name, vram = _win_gpu_info()
    info = {"phase": "info", "backend": "dml", "device": name or "DirectML device",
            "torch": None, "cuda": "DirectML/onnxruntime", "onnxruntime": ort.__version__,
            "arch": None, "sm_count": None,
            "vram_gib": round(vram / 1024 ** 3, 1) if vram else None, "cudnn": None}
    log("device=%s provider=DmlExecutionProvider onnxruntime=%s VRAM=%s GiB" % (
        info["device"], ort.__version__, info["vram_gib"]))
    jline(info)


def dml_matmul(a):
    np = _np()
    n = min(a.size, 4096)
    out = {"phase": "matmul", "backend": "dml", "n": n, "results": {}}
    for npdt, name in [(np.float16, "fp16"), (np.float32, "fp32")]:
        sess = _dml_session([__import__("onnx").helper.make_node("MatMul", ["A", "B"], ["Y"])],
                            [_dml_tvi("A", npdt, [n, n]), _dml_tvi("B", npdt, [n, n])],
                            [_dml_tvi("Y", npdt, [n, n])])
        io = sess.io_binding()
        io.bind_ortvalue_input("A", _dml_dev(np.random.randn(n, n).astype(npdt)))
        io.bind_ortvalue_input("B", _dml_dev(np.random.randn(n, n).astype(npdt)))
        io.bind_ortvalue_output("Y", _dml_devout([n, n], npdt))
        it, dt = timed_dml(sess, io, a.seconds)
        tf = round(2.0 * n ** 3 * it / dt / 1e12, 1)
        out["results"][name] = {"iters": it, "seconds": round(dt, 2), "tflops": tf}
        log("matmul %-5s N=%d iters=%d TFLOPS=%.1f" % (name, n, it, tf))
    jline(out)


def dml_membw(a):
    np = _np()
    from onnx import helper
    m = 64 * 1024 * 1024
    out = {"phase": "membw", "backend": "dml", "results": {}}
    add = _dml_session([helper.make_node("Add", ["A", "B"], ["Y"])],
                       [_dml_tvi("A", np.float32, [m]), _dml_tvi("B", np.float32, [m])],
                       [_dml_tvi("Y", np.float32, [m])])
    io = add.io_binding()
    io.bind_ortvalue_input("A", _dml_dev(np.random.randn(m).astype(np.float32)))
    io.bind_ortvalue_input("B", _dml_dev(np.random.randn(m).astype(np.float32)))
    io.bind_ortvalue_output("Y", _dml_devout([m], np.float32))
    it, dt = timed_dml(add, io, a.seconds)
    add_gbps = round(3 * m * 4 * it / dt / 1e9, 0)

    red = _dml_session([helper.make_node("ReduceSum", ["A"], ["Y"], keepdims=0)],
                       [_dml_tvi("A", np.float32, [m])], [_dml_tvi("Y", np.float32, [])])
    io = red.io_binding()
    io.bind_ortvalue_input("A", _dml_dev(np.random.randn(m).astype(np.float32)))
    io.bind_ortvalue_output("Y", _dml_devout([], np.float32))
    it, dt = timed_dml(red, io, a.seconds)
    red_gbps = round(m * 4 * it / dt / 1e9, 0)

    out["results"]["fp32"] = {"add_gbps": add_gbps, "reduce_gbps": red_gbps}
    log("membw fp32 add=%.0f GB/s reduce=%.0f GB/s" % (add_gbps, red_gbps))
    jline(out)


def dml_pcie(a):
    import onnxruntime as ort
    np = _np()
    # Shared-memory iGPU: host<->device copies are CPU-mediated. Keep the buffer
    # small and the iteration count bounded (the DML upload pool grows per copy).
    sz = 8 * 1024 * 1024
    max_it = 100
    host = np.random.randn(sz).astype(np.float32)
    dv = ort.OrtValue.ortvalue_from_numpy(host, "dml", 0)  # single reused device buffer
    t0 = time.time(); it = 0
    while it < max_it and time.time() - t0 < a.seconds:
        dv.update_inplace(host); it += 1  # H2D into the existing buffer
    h2d = round(sz * 4 * it / (time.time() - t0) / 1e9, 1)
    t0 = time.time(); it = 0
    while it < max_it and time.time() - t0 < a.seconds:
        _ = dv.numpy(); it += 1  # D2H
    d2h = round(sz * 4 * it / (time.time() - t0) / 1e9, 1)
    out = {"phase": "pcie", "backend": "dml", "h2d_gbps": h2d, "d2h_gbps": d2h}
    log("pcie H2D=%.1f GB/s D2H=%.1f GB/s" % (h2d, d2h))
    jline(out)


def dml_conv(a):
    np = _np()
    from onnx import helper
    x = np.random.randn(16, 64, 128, 128).astype(np.float16)
    w = np.random.randn(128, 64, 3, 3).astype(np.float16)
    sess = _dml_session([helper.make_node("Conv", ["X", "W"], ["Y"], pads=[1, 1, 1, 1])],
                        [_dml_tvi("X", np.float16, x.shape), _dml_tvi("W", np.float16, w.shape)],
                        [_dml_tvi("Y", np.float16, [16, 128, 128, 128])])
    io = sess.io_binding()
    io.bind_ortvalue_input("X", _dml_dev(x))
    io.bind_ortvalue_input("W", _dml_dev(w))
    io.bind_ortvalue_output("Y", _dml_devout([16, 128, 128, 128], np.float16))
    flops = 2.0 * 16 * 128 * (128 * 128) * (64 * 3 * 3)
    it, dt = timed_dml(sess, io, a.seconds)
    tf = round(flops * it / dt / 1e12, 1)
    log("conv2d fp16 TFLOPS=%.1f" % tf)
    jline({"phase": "conv", "backend": "dml", "tflops": tf, "iters": it})


def dml_integrity(a):
    from onnx import helper, TensorProto
    np = _np()
    chunk_bytes = int(os.environ.get("BENCH_DML_CHUNK_MB", "256")) * 1024 * 1024
    target_bytes = int(os.environ.get("BENCH_DML_VRAM_MB", "2048")) * 1024 * 1024
    nt = max(1, target_bytes // chunk_bytes)
    nelem = chunk_bytes // 4
    nshape = np.array([nelem], dtype=np.int64)

    ffill = _dml_session(
        [helper.make_node("ConstantOfShape", ["shape"], ["Y"],
                          value=helper.make_tensor("v", TensorProto.FLOAT, [1], [1.0]))],
        [helper.make_tensor_value_info("shape", TensorProto.INT64, [1])],
        [_dml_tvi("Y", np.float32, [nelem])])
    ifill = _dml_session(
        [helper.make_node("ConstantOfShape", ["shape"], ["Y"],
                          value=helper.make_tensor("v", TensorProto.INT32, [1], [0x7F3A2C1B]))],
        [helper.make_tensor_value_info("shape", TensorProto.INT64, [1])],
        [_dml_tvi("Y", np.int32, [nelem])])

    bad = 0
    io = ffill.io_binding(); io.bind_cpu_input("shape", nshape)
    for _ in range(nt):
        io.bind_ortvalue_output("Y", _dml_devout([nelem], np.float32))
        ffill.run_with_iobinding(io)
        bad += int(np.count_nonzero(np.abs(io.get_outputs()[0].numpy() - 1.0) > 1e-3))

    badbits = 0
    io = ifill.io_binding(); io.bind_cpu_input("shape", nshape)
    for _ in range(nt):
        io.bind_ortvalue_output("Y", _dml_devout([nelem], np.int32))
        ifill.run_with_iobinding(io)
        badbits += int(np.count_nonzero(io.get_outputs()[0].numpy() != 0x7F3A2C1B))

    out = {"phase": "integrity", "backend": "dml", "chunks": nt,
           "gib": round(nt * chunk_bytes / 1024 ** 3, 1),
           "float_mismatched": bad, "int_bad_elements": badbits}
    log("integrity filled=%.1fGiB mismatched=%d bad_elements=%d" % (out["gib"], bad, badbits))
    jline(out)


def dml_streams(a):
    """Concurrent-session scaling. DML runs asynchronously, so independent sessions
    are submitted interleaved from one thread (a single iGPU has one command queue,
    so expect little or no scaling - that is itself the measurement)."""
    from onnx import helper
    np = _np()
    n = 2048
    nm = helper.make_node("MatMul", ["A", "B"], ["Y"])
    out = {"phase": "streams", "backend": "dml", "n": n, "results": {}}
    for ns in (1, 2, 4, 8):
        sess = [_dml_session([nm], [_dml_tvi("A", np.float16, [n, n]), _dml_tvi("B", np.float16, [n, n])],
                             [_dml_tvi("Y", np.float16, [n, n])]) for _ in range(ns)]
        ios = []
        for s in sess:
            io = s.io_binding()
            io.bind_ortvalue_input("A", _dml_dev(np.random.randn(n, n).astype(np.float16)))
            io.bind_ortvalue_input("B", _dml_dev(np.random.randn(n, n).astype(np.float16)))
            io.bind_ortvalue_output("Y", _dml_devout([n, n], np.float16))
            ios.append(io)
        for io in ios:
            io.get_outputs()[0].numpy()
        t0 = time.time(); it = 0
        while time.time() - t0 < a.seconds:
            for s, io in zip(sess, ios):
                s.run_with_iobinding(io)
            for io in ios:
                io.get_outputs()[0].numpy()
            it += 1
        dt = time.time() - t0
        tf = round(2.0 * n ** 3 * it * ns / dt / 1e12, 1)
        out["results"][str(ns)] = tf
        log("streams=%d TFLOPS=%.1f" % (ns, tf))
        del sess, ios
    jline(out)


def dml_soak(a):
    from onnx import helper
    np = _np()
    n = min(a.size, 4096)
    mm = _dml_session([helper.make_node("MatMul", ["A", "B"], ["Y"])],
                      [_dml_tvi("A", np.float16, [n, n]), _dml_tvi("B", np.float16, [n, n])],
                      [_dml_tvi("Y", np.float16, [n, n])])
    io_mm = mm.io_binding()
    io_mm.bind_ortvalue_input("A", _dml_dev(np.random.randn(n, n).astype(np.float16)))
    io_mm.bind_ortvalue_input("B", _dml_dev(np.random.randn(n, n).astype(np.float16)))
    io_mm.bind_ortvalue_output("Y", _dml_devout([n, n], np.float16))

    cv = _dml_session([helper.make_node("Conv", ["X", "W"], ["Y"], pads=[1, 1, 1, 1])],
                      [_dml_tvi("X", np.float16, [8, 64, 128, 128]), _dml_tvi("W", np.float16, [128, 64, 3, 3])],
                      [_dml_tvi("Y", np.float16, [8, 128, 128, 128])])
    io_cv = cv.io_binding()
    io_cv.bind_ortvalue_input("X", _dml_dev(np.random.randn(8, 64, 128, 128).astype(np.float16)))
    io_cv.bind_ortvalue_input("W", _dml_dev(np.random.randn(128, 64, 3, 3).astype(np.float16)))
    io_cv.bind_ortvalue_output("Y", _dml_devout([8, 128, 128, 128], np.float16))

    samples, stop = [], threading.Event()
    tel = threading.Thread(target=_telemetry_loop, args=(samples, stop), daemon=True)
    tel.start()
    t0 = time.time(); mats = convs = 0
    while time.time() - t0 < a.seconds:
        for _ in range(10):
            mm.run_with_iobinding(io_mm); mats += 1
            cv.run_with_iobinding(io_cv); convs += 1
        io_mm.get_outputs()[0].numpy()
        io_cv.get_outputs()[0].numpy()
    dt = time.time() - t0
    stop.set(); tel.join(timeout=5)
    mm_flops = 2.0 * n ** 3 * mats
    conv_flops = 2.0 * 8 * 128 * 128 * 128 * (64 * 3 * 3)
    out = {"phase": "soak", "backend": "dml", "seconds": round(dt, 1), "matmuls": mats, "convs": convs,
           "size": n,
           "matmul_tflops": round(mm_flops / dt / 1e12, 1),
           "total_tflops": round((mm_flops + conv_flops * convs) / dt / 1e12, 1),
           "until_stable": bool(getattr(a, "until_stable", False)),
           "stable": False}
    ts = _telemetry_summary(samples)
    if ts:
        out["telemetry"] = ts
    log("soak %.1fs matmuls=%d convs=%d N=%d matmul_TFLOPS=%.1f total_TFLOPS=%.1f"
        % (dt, mats, convs, n, out["matmul_tflops"], out["total_tflops"]))
    jline(out)


DML_PHASES = {
    "info": dml_info, "matmul": dml_matmul, "membw": dml_membw, "pcie": dml_pcie,
    "conv": dml_conv, "vram": dml_integrity, "integrity": dml_integrity,
    "streams": dml_streams, "soak": dml_soak,
}


# --------------------------------------------------------------------------- driver
def detect_backend(requested):
    if requested in ("cuda", "dml", "cpu"):
        return requested
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    try:
        import onnxruntime as ort
        if "DmlExecutionProvider" in ort.get_available_providers():
            return "dml"
    except Exception:
        pass
    try:
        import torch  # noqa: F401
        return "cpu"
    except Exception:
        pass
    return None


# Import failures that mean "the NVIDIA driver is not up yet" rather than "no
# torch at all": libcuda.so.1 missing on Linux, the CUDA DLLs/NVML on Windows.
_DRIVER_MARKERS = ("libcuda", "nvcuda", "cuda", "nvidia")


def classify_backend_error():
    """Why the CUDA path is not usable, as ``(kind, detail)``; ``(None, None)`` when it is.

    The three cases an operator has to tell apart:
      * ``driver_not_ready`` - importing torch itself failed because the NVIDIA
        driver is not up yet (no /dev/nvidia*, no libcuda.so.1). A live USB
        booted moments ago hits this: the DKMS module is built but not loaded,
        and waiting for the driver before the GPU group (Run-LinuxBench.sh
        does) is the fix.
      * ``no_cuda_device`` - torch imports but reports no CUDA device.
      * ``no_torch`` - no importable torch at all.

    torch stays optional and lazy: this is only called when a GPU phase or the
    probe asks why CUDA was not selected.
    """
    try:
        import torch as t
    except Exception as e:
        msg = "%s: %s" % (type(e).__name__, e)
        if any(m in msg.lower() for m in _DRIVER_MARKERS):
            return "driver_not_ready", (
                "torch import failed while the NVIDIA driver is coming up (%s); "
                "wait for the driver before the GPU group" % msg)
        return "no_torch", "torch could not be imported (%s)" % msg
    try:
        if t.cuda.is_available():
            return None, None
    except Exception as e:
        return "no_cuda_device", "torch.cuda.is_available() failed: %s: %s" % (
            type(e).__name__, e)
    return "no_cuda_device", "torch %s imports but reports no CUDA device" % (
        getattr(t, "__version__", "?"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=sorted(TORCH_PHASES) + ["probe"])
    ap.add_argument("--seconds", type=float, default=12.0, help="per-measurement seconds")
    ap.add_argument("--size", type=int, default=12288, help="matrix size N")
    ap.add_argument("--force", action="store_true", help="skip VRAM-fill confirmation")
    ap.add_argument("--until-stable", action="store_true",
                    help="soak until the GPU temperature plateaus (--seconds is the ceiling)")
    ap.add_argument("--stable-hold", type=float, default=60.0,
                    help="keep soaking this many seconds after the plateau is reached")
    ap.add_argument("--backend", default="auto", choices=["auto", "cuda", "dml", "cpu"])
    a = ap.parse_args()

    backend = detect_backend(a.backend)
    if a.phase == "probe":
        cuda = torch_ok = dml = 0
        try:
            import torch
            torch_ok = 1
            cuda = 1 if torch.cuda.is_available() else 0
        except Exception:
            pass
        try:
            import onnxruntime as ort
            dml = 1 if "DmlExecutionProvider" in ort.get_available_providers() else 0
        except Exception:
            pass
        log("CUDA=%d" % cuda)
        log("TORCH=%d" % torch_ok)
        log("DML=%d" % dml)
        log("BACKEND=%s" % (backend or "none"))
        # WHY the CUDA path was not taken, so a missing driver is not just "CUDA=0"
        kind, detail = classify_backend_error()
        if kind:
            log("BACKEND_ERROR=%s" % kind)
        if backend is None:
            print("ERROR: no usable backend (kind=%s): %s" % (kind or "unknown", detail or ""),
                  file=sys.stderr)
        return 0

    if backend is None:
        # Name the reason and record it: a run with no usable backend must never
        # look like a run that was not asked for a GPU test. The runner stores
        # this RESULT_JSON in the run and benchreport turns it into a warn
        # verdict (apply_backend_liveness).
        kind, detail = classify_backend_error()
        err = "no usable backend (kind=%s): %s" % (kind or "unknown", detail or "")
        print("ERROR: " + err, file=sys.stderr)
        jline({"phase": a.phase, "backend": None, "error": err,
               "backend_error_kind": kind or "unknown"})
        return 2
    a.backend = backend
    a.device = "cuda" if backend == "cuda" else ("cpu" if backend == "cpu" else "dml")
    log("[backend] %s" % backend)

    phases = DML_PHASES if backend == "dml" else TORCH_PHASES
    try:
        phases[a.phase](a)
    except Exception as e:
        import traceback
        traceback.print_exc()
        jline({"phase": a.phase, "backend": backend, "error": "%s: %s" % (type(e).__name__, e)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
