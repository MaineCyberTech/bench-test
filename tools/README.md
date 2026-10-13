# tools/ — vendored binaries and their pinned fetchers

The toolkit loads one vendored binary dependency at runtime: the
**LibreHardwareMonitor** sensor library (`tools/lhm/LibreHardwareMonitorLib.dll`)
used by `bench/Sample-Sensors.ps1` for AMD/Intel Windows telemetry (GPU
temp/power/clocks/VRAM, fan RPM, CPU temp/power/clock, per-core load). The
PyInstaller bundles ship it next to the exe (`packaging/build_app.py`).

Binaries are **not committed** (`tools/lhm` is git-ignored); the fetch script
that reproduces them **is**. So a fresh clone is reproducible without carrying
large third-party DLLs in git:

```bash
python tools/fetch-lhm.py          # downloads + verifies -> tools/lhm
```

`tools/fetch-lhm.py` downloads the **pinned** release (`LHM_VERSION`), verifies
its **SHA-256** against the committed constant (`LHM_SHA256`, fail closed), and
extracts the library plus its managed dependencies. `packaging/build_app.py`
runs it automatically when `tools/lhm` is missing, and warns loudly if the
sensor library still cannot be restored (EVOL-P3-002).

Do not commit `tools/lhm`; bump `LHM_VERSION` and `LHM_SHA256` together when
upgrading. See `docs/DEPENDENCIES.md` for the dependency/provenance policy.
