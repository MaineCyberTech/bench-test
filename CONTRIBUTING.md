# Contributing

Thanks for improving `bench-test`. Keep changes small, factual, and verifiable.

## Before you commit

- **Run the test suite.** `python tests/run_all.py` must print `RESULT: PASS`.
  On Windows, run it from a virtualenv with `windows-curses` installed (see the
  README's [Tests](README.md#tests) section) so the dashboard checks are not
  skipped.
- **Keep the change minimal.** Touch only the files the fix needs, plus the tests
  or docs it requires. Do not reformat or reorder unrelated code.

## Line endings and exec bits

Text files are **LF**; `*.sh` files must be **executable** (`100755`). Windows
checkouts can silently drop both, so verify before committing:

```bash
git ls-files --eol        # LF everywhere except *.ps1/*.cmd (CRLF)
git ls-files -s           # shell scripts show 100755
```

`*.ps1` and `*.cmd` are kept CRLF for Windows tooling; `.gitattributes` enforces
this. If a shell script lost its exec bit: `git update-index --chmod=+x path`.

## Never commit

- **Tokens, keys, or the SQLite db** — `server/benchd.conf`, `server/data/`,
  `live/private/`, `~/.config/bench-agent.conf` are git-ignored for this reason.
- **A virtualenv** (`.venv/`) or Python caches (`__pycache__/`, `*.pyc`).
- **Generated results** (`results/`) and **ISOs / build output** (`live/out*/`,
  multi-GB images, `dist/`, `build/`).

These are already in `.gitignore`; if you find yourself forcing one in, stop and
ask why it is needed.

## Pull requests

- **Small and focused** — one finding or fix per PR where possible.
- **Draft first.** Open the PR as a draft; do not self-approve or merge. A
  maintainer merges.
- **Agents:** never commit a test result you did not run. If something could not
  be run, say `not run` and why. Never fabricate evidence in a PR body.
- Fill in the [pull request template](.github/PULL_REQUEST_TEMPLATE.md), including
  the test result.

## License

This is proprietary software — see [`LICENSE`](LICENSE). By contributing you agree
your changes are licensed under the same terms.
