# Releasing — versioning, notes & changelog

How a bench-test release is meant to be prepared, and where the current
automation stops. The version is **derived from git**, not maintained by hand:
`bench_version()` reads `git describe --tags --long`, so exactly on a tag the
version is the release (`x.y.z`), commits past it are build metadata
(`x.y.z+<n>` = `n` commits after `vx.y.z`), and `version_string()` appends the
commit (`x.y.z+<n>.<commit>`). The repo root `VERSION` file mirrors the latest tag
so a copy without git (a live stick, a deployed server) reports the same release.

## 1. What `release.yml` does today

`.github/workflows/release.yml` ("Release (auto-tag)") runs on a push to `main`,
or on manual dispatch (`bump_kind: minor | patch | major | none`). On a push it:

1. reads the commit subjects since the last `v*` tag and picks a bump from them;
2. rewrites `VERSION` and commits `chore(release): VERSION <x.y.z>` to `main`;
3. cuts an annotated tag `v<x.y.z>` and pushes it.

The bump rule is commit-subject driven:

| Subject starts with | Bump |
|---|---|
| `feat:` / `feat(scope):` / `feat!:` | **minor** |
| `fix:` / `perf:` / `revert:` (and `(scope)` / `!` forms) | **patch** |
| anything else (`docs:`, `chore:`, `ci:`, …) | **skip** — no tag |

`bump_kind: major` is only available from manual dispatch; a push never cuts a
major.

**What it does not do yet.** It cuts a **bare tag**: there is no GitHub Release
body, no `generate_release_notes`, and no changelog step (audit `REL-P1-001`).
Separately, the build workflows publish their own releases tagged by **workflow
run number**, not by version or commit — `build-app.yml` uses `app-<run_number>`
and `build-iso.yml` uses `iso-<gpu>-<run_number>` (audit `REL-P2-002`). Those are
artifact drops, not version releases; pick an ISO by the **commit in its notes**,
not by its number (see `README.md` § Live USB).

## 2. Intended flow

The commit subjects are the **draft input**; a human writes the release notes.

1. **Land changes with conventional subjects.** Use `feat:`, `fix:`, `perf:`,
   `revert:`, `docs:`, `chore:`, `ci:` as the prefix; put breaking changes on a
   `!` (`feat!:`) so they are easy to find. `release.yml` already keys off these.
   Machine commits under `bench:` are excluded from notes — they are the majority
   of history and carry no release meaning.
2. **Draft the notes from the commits since the last tag**, grouped by type
   (features, fixes, performance, breaking changes, security). `git log
   <last-tag>..HEAD --format=%s` is the raw list; filter out `bench:` subjects.
3. **A human edits `CHANGELOG.md` before tagging.** Move the relevant entries out
   of `## [Unreleased]` into a new `## [<x.y.z>] - <YYYY-MM-DD>` heading, and add
   the operator-facing sections that a generated list cannot infer:
   - **Upgrade** — steps an operator must run (e.g. rotate a baked token,
     re-enroll runners, deploy the new `server/benchd.py` + `VERSION`).
   - **Rollback** — how to go back if the release is bad.
   - **Operator actions** — credential/command/deploy changes.
   - **Security** — fixes worth calling out, with the finding/issue reference.
   Keep `Unreleased` at the top for work in progress.
4. **Tag.** Let `release.yml` tag on the next push to `main`, or dispatch it with
   an explicit `bump_kind`. The tag is what promotes the build to a release.

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/):
one `## [Unreleased]` section plus one `## [x.y.z] - date` section per release,
and the standard groups (`Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`,
`Security`).

## 3. Why authored notes, not generated ones

A generated body alone is not usable here: the bump rule reads commit subjects,
but ~94% of the history is auto-generated `bench:` output, and no PR/issue
template or commit-lint exists to force a release-note line for a change (audit
`REL-P2-003`). So the changelog is **edited by a human** and the commit subjects
only seed the draft. If a change needs an operator to act (rotate a token,
re-enroll a runner, review `exec`/`shell` exposure), that belongs in the release
entry — it will not be inferred from a `fix:` subject.

## 4. Checklist for a release owner

- [ ] All intended changes are on `main` (or the release branch) and CI is green.
- [ ] `CHANGELOG.md` has the new version section, dated, with Upgrade / Rollback /
      Operator actions / Security filled in where they apply.
- [ ] `VERSION` and the tag agree (`release.yml` keeps them in step).
- [ ] Any credential or command-surface change has an explicit operator action in
      the notes.
- [ ] If an artifact carries a baked credential, the release notes say so and the
      rotation step is written down (see `docs/INCIDENT_RESPONSE.md`).
- [ ] Tag, then verify the tag's commit matches the changelog's date and scope.

> Traceability: this document and the root `CHANGELOG.md` address audit finding
> **REL-P1-001**. The gaps they reference are tracked as `REL-P2-002` (artifacts
> tagged by run number) and `REL-P2-003` (unenforced changelog input).
