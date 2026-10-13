"""bootstrap.py tests - the packaged app's runner-interpreter/dep provisioning.

Stdlib only, and deliberately side-effect free: nothing here creates a venv,
downloads Python, or shells out. We only check path layout, package selection,
and the interpreter-resolution order.
"""
import importlib.util
import os
import sys

from harness import Checker, BENCH


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("")


def run():
    c = Checker("bootstrap")
    bs = _load("bootstrap", os.path.join(BENCH, "bootstrap.py"))

    root = bs.cache_root()
    c.check("cache_root is an absolute path ending in bench-test",
            os.path.isabs(root) and os.path.basename(root) == "bench-test", root)
    c.check("base python lives under the cache root",
            os.path.dirname(bs.base_python_path()) == os.path.join(root, "python"),
            bs.base_python_path())
    c.check("env python lives under the cache root",
            root in bs.env_python_path(), bs.env_python_path())

    # package selection
    c.check("nvidia base selection has no torch by default",
            "torch" not in bs.packages_for("nvidia"), bs.packages_for("nvidia"))
    c.check("nvidia + include_torch adds torch",
            "torch" in bs.packages_for("nvidia", include_torch=True),
            bs.packages_for("nvidia", include_torch=True))
    for p in ("numpy", "psutil", "cryptography"):
        c.check("base selection includes %s" % p, p in bs.packages_for("nvidia"), "")
    if os.name == "nt":
        c.check("windows amd selection includes DirectML",
                "onnxruntime-directml" in bs.packages_for("amd"),
                bs.packages_for("amd"))
        c.check("windows selection includes windows-curses (TUI)",
                "windows-curses" in bs.packages_for("nvidia"),
                bs.packages_for("nvidia"))
    else:
        c.check("non-windows amd selection omits DirectML",
                "onnxruntime-directml" not in bs.packages_for("amd"),
                bs.packages_for("amd"))

    c.check("import-name mapping handles directml",
            bs._import_name("onnxruntime-directml") == "onnxruntime",
            bs._import_name("onnxruntime-directml"))
    c.check("import-name mapping handles hyphen",
            bs._import_name("opencv-python") == "opencv_python",
            bs._import_name("opencv-python"))

    # interpreter resolution order: BENCH_PY -> repo/.venv -> python
    import tempfile
    tmp = tempfile.mkdtemp(prefix="bench-bs-")
    fake = os.path.join(tmp, "py.exe" if os.name == "nt" else "py")
    _touch(fake)
    old = os.environ.get("BENCH_PY")
    try:
        os.environ["BENCH_PY"] = fake
        c.check("python_path honours BENCH_PY",
                bs.python_path(tmp) == fake, bs.python_path(tmp))
    finally:
        if old is None:
            os.environ.pop("BENCH_PY", None)
        else:
            os.environ["BENCH_PY"] = old

    rel = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python3")
    vpy = os.path.join(tmp, ".venv", *rel)
    _touch(vpy)
    c.check("python_path prefers a repo .venv",
            bs.python_path(tmp) == vpy, bs.python_path(tmp))

    return c.fails


if __name__ == "__main__":
    import sys as _s
    _s.exit(1 if run() else 0)
