# Deterministic checks - bench-test

Machine checks (no LLM). Findings use the repo-deep-dive `DET` area.

## Findings

| ID | Title | Severity |
|---|---|---|
| DET-P3-001 | [CI] No GitHub Actions workflows | P3 |
| DET-P3-002 | [PORT] .gitattributes missing (no line-ending policy) | P3 |

## Detail

### Finding ID: DET-P3-001 - [CI] No GitHub Actions workflows

Add CI for lint/test/build.

### Finding ID: DET-P3-002 - [PORT] .gitattributes missing (no line-ending policy)

Without a policy, checkouts differ between Windows and Linux. Add `* text=auto eol=lf`.

