# Security Policy

`bench-test` is a workstation benchmark & stress toolkit with an optional fleet
control plane (`benchd`). In scope for this policy: the runners and phases under
`bench/`, the control plane under `server/`, the live-USB build under `live/`, and
the app/ISO release artifacts the CI workflows publish.

## Supported versions

- The current `main` branch and the latest tagged release (the value in `VERSION`).
- Older tagged releases are **not** patched in place; upgrade to the latest release
  or `main` to receive fixes.
- A runner, dashboard or agent reports the toolkit identity it is running
  (release, build, commit) to the control plane, so a deployment can be checked
  against the current `main`.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** using GitHub's private
vulnerability reporting for this repository:

1. Go to the repository's **Security** tab → **Report a vulnerability**
   (<https://github.com/MaineCyberTech/bench-test/security/advisories/new>).
2. Include: affected path/version (or commit), impact, reproduction steps, and any
   suggested fix.
3. Redact secret values — reference the file path and the secret **type** only.

## What not to file publicly

Do **not** open a public issue for a suspected vulnerability, and do not paste
live credentials or personal data into a report or an issue. In particular:

- The repository, and the app-bundle and live-ISO releases, are **public**. A
  control-plane token baked into an artifact is world-readable — a report about a
  baked or leaked credential is in scope and should be sent privately, and the
  credential should be rotated.
- Results and system inventory can contain hostnames, usernames/home paths, IP and
  MAC addresses, and hardware identifiers. Do not include a real machine's
  inventory or a raw results directory in a public issue; redact it first.
- Do not run the destructive/state-mutating operations the control plane exposes
  (`exec`/`shell`, `iso_write`, reboot/shutdown) against systems you do not own
  while investigating.

## Response expectations

Reports are triaged by the maintainers on a best-effort basis; there is no
guaranteed response time. Fixes land on `main` and in the next tagged release, and
findings are tracked with a one-line status in [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md).

## Related

- [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) — open issues, including security
  items from the current audit.
- [`docs/PRIVACY.md`](docs/PRIVACY.md) — data classification, retention and the
  vendor/processor list.
- [`docs/CONTROL_PLANE.md`](docs/CONTROL_PLANE.md) § Security — the control plane's
  auth model and deployment posture.
- [`.github/CODEOWNERS`](.github/CODEOWNERS) — review ownership.
