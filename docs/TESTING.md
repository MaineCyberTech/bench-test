# Testing — what the suite runs, and what `RESULT: PASS` does *not* cover

The suite is `tests/run_all.py`: plain stdlib `print`-based checks, no pytest, no
`pyproject.toml`, no coverage tooling. It starts an in-process `benchd` on an
ephemeral port with a throwaway SQLite db (see `tests/harness.py`), so it needs no
network and no external service.

```bash
python tests/run_all.py                 # RESULT: PASS expected
python tests/run_all.py --allow-skips   # tolerate platform/tooling gaps
```

`RESULT: PASS` means **every check in every module passed and no group was
skipped**. It is not a statement that the product works — read the scope below.

## The skip gate

A module that cannot exercise a whole group (no display, no `curses`, no
`powershell`/`bash`, no `torch`) records it with `harness.skip(label, reason)`
instead of printing and returning silently. `run_all.py` then prints a
`SKIP SUMMARY` and **fails** unless skips are explicitly allowed via
`--allow-skips` or `BENCH_TEST_ALLOW_SKIPS=1`. A provisioning gap is therefore
visible instead of masquerading as a green suite.

Known conditional skips and their trigger:

| Skip | Trigger |
|---|---|
| `gui rows` | `tkinter` missing, or no display (`Tk()` raises) |
| `tui rows` | `curses`/`bench/tui.py` cannot be imported |
| `plateau logic` | `bench/gpu_bench.py` cannot be imported (e.g. no `torch`) |
| `the shipped-parse behaviour check`, `the bounded-CLI behaviour check` | no `powershell`/`pwsh` on the host |
| `the Run-LinuxBench guard behaviour check` | no `bash` on the host |
| `the RAM integrity record check` | `bench/ram_bench.py` cannot be imported (no `numpy`) |
| `the random-IO timing check` | `bench/disk_bench.py` cannot be imported |

On a fully provisioned host there are no skips; on a half-provisioned or headless
host, expect `RESULT: FAIL` (or pass `--allow-skips` when the gap is expected).

## Modules

Each `tests/test_*.py` exposes `run()` returning a list of failure strings, and
`run_all.py` aggregates them.

| Module | Covers |
|---|---|
| `test_control_plane.py` | `benchd` API: auth, runners, runs, telemetry, logs, artifacts, stats, commands, schedules, delete, reaper, SSE |
| `test_reporting.py` | `benchreport`: `Reporter`, `PhaseTracker`, `ReporterPump`, system info, summary/telemetry/inventory builders, `run_exec`, CSV mapping |
| `test_import.py` | `server/import_results.py` backfill: timestamp/CSV parsing, summary + verdict builders (must match the live path) |
| `test_update.py` | remote self-update helpers against a local stub server: toolkit update, app/ISO paths, guards |
| `test_presets.py` | preset parity across GUI/TUI/web, runner group lists, `extra_runner_args` |
| `test_dashboards.py` | phase-table close logic in GUI + TUI; web live-view **source** assertions |
| `test_soak.py` | stabilized-soak decision logic (`gpu_bench._temp_stable` / `_soak_decision`) and payload → argv |
| `test_version.py` | one release version reaching VERSION, toolkit, runners, control plane |
| `test_bootstrap.py` | `bootstrap.py` path layout, package selection, interpreter-resolution order |
| `test_packaging.py` | baked `bench-agent.conf`, PowerShell parse/throttle/TDR behaviour, `Run-LinuxBench` guard, serial unit, bounded Ollama CLI |
| `test_robustness.py` | finish retries, atomic battery start, safe tar extraction, idempotent import, and client retry/circuit-breaker behaviour |

## Executed vs source-string-matched

Not every check runs the thing it asserts on. When reading a PASS, know which kind
you are looking at:

- **Executed** (the real code runs in-process or as a subprocess): the control-plane
  API, `benchreport` helpers, `run_win.sampler` (threaded, CSV read back),
  `telemetry_log.py` (subprocess), `gpu_bench` decision helpers, `disk_bench`
  timing, `bootstrap` path logic, `import_results`, `live_update`.
- **Source-string-matched** (a substring/`re` search over a file, no execution):
  the 23 PowerShell battery scripts (`bench/*.ps1`), `server/web/app.js` +
  `style.css`, `bench/tui.py`/`gui.py` wiring, `Run-LinuxBench.sh`, and the serial
  systemd drop-in. These catch drift and deleted strings, **not** syntax or
  semantic errors — a `.ps1`/`.js`/`.sh` file can be broken and still pass.
- **Conditionally executed** where a host tool is present: `test_packaging.py`
  runs real `powershell`/`pwsh` and `bash` snippets when available (otherwise it
  records a skip, see above).

## Platform caveats

- **Windows needs `windows-curses` (and `psutil`)** for `test_presets.py` /
  `test_dashboards.py`: those modules import `bench/tui.py`, which does
  `import curses`. A bare system CPython has no `_curses`, so the module crashes
  rather than skipping. Run under the bootstrapped `.venv` (which installs
  `windows-curses`), not system Python. This is why the suite is described as
  *stdlib for the harness*, but is not stdlib-only on Windows.
- **`tkinter`/display**: `gui rows` runs a withdrawn `Tk` root; a headless host
  (or a Python built without Tk) records a skip.
- **`torch`/`numpy`**: `plateau logic` and the RAM-integrity / random-IO checks
  need the compute venv; without it they skip (and, by default, fail the run).
- **`powershell`/`bash`**: present on Windows/Linux respectively, often absent on
  the other; the affected behaviour checks skip there.
- **Deterministic waits**: the suite avoids fixed `time.sleep`-then-assert.
  `harness.wait_for(pred, timeout)` and `harness.wait_for_rows(path, n)` poll the
  observed condition and return as soon as it holds, so a slow/loaded host does
  not flake and a fast host is not slowed.

## What `RESULT: PASS` does **not** cover

- **No real benchmark run.** No GPU/CPU/RAM/disk/net/LLM/nvenc/gfx phase is
  executed end to end; the PowerShell battery and `app.js` are not run at all
  (source-string only). `bench/agent.py --self-test` exists but is not wired into
  the suite.
- **No built artifact.** The PyInstaller app bundles and the live ISO are never
  built, launched or verified here.
- **No coverage measurement.** There is no `coverage.py` config or threshold; a
  module can lose checks (or skip) without a number changing.
- **No lint/syntax gate** for `*.ps1`, `*.js`, `*.sh`, or even `*.py` beyond the
  checks the modules themselves happen to do.
- **No CI enforcement** is provided by this suite alone; a red or crashing
  `run_all.py` is only a local signal unless a workflow runs it.

## Adding a module

1. Create `tests/test_<area>.py` with a `run()` that returns a list of failure
   strings. Use the shared harness:

   ```python
   from harness import Checker, Server, tmpdir, skip, wait_for

   def run():
       c = Checker("myarea")
       srv = Server()                 # in-process benchd on an ephemeral port
       cl = srv.client()
       c.check("something", condition, detail)   # prints PASS/FAIL, records failures
       ...
       srv.stop()
       return c.fails
   ```

2. Record any group you cannot run with `skip("label", reason)` — never a bare
   `print("SKIP ...")`, or the skip gate cannot see it.
3. Wait on conditions with `wait_for(...)` / `wait_for_rows(...)`; do not add
   fixed sleeps.
4. Register the module in `tests/run_all.py` (`import` + append to `MODULES`).
5. Run `python tests/run_all.py` (expect `RESULT: PASS`); if your module
   legitimately skips on some platforms, note it in the table above.
