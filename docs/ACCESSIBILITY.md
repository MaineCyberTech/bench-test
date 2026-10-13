# Accessibility

The web dashboard (`server/web/`) is the operator's control surface for a live
fleet, so a dropped `role`, a missing image alternative or a duplicated `id` is a
real regression, not cosmetic. `tests/test_a11y.py` is a dependency-free structural
guard for that surface (findings **UX-P3-003 / MOB-P3-003**: the suite was
source-string matching only and had no accessibility coverage).

## Run it

```bash
python tests/run_all.py            # whole suite (includes the a11y module)
python tests/test_a11y.py          # just the structural a11y checks
```

On Windows, use the project interpreter so the suite runs on the same Python as
the rest of the tests:

```powershell
C:\temp\bench-test\.venv\Scripts\python.exe tests\test_a11y.py
```

A red run prints every violation as `index.html:<line>  <element>  <detail>` (or
`app.js:<line>`) and the module fails. Nothing external is needed — no browser, no
Node, no pip.

## What the test covers automatically

| Invariant | How it is checked |
|---|---|
| Document language | `<html lang>` is non-empty |
| Image alternatives | every `<img>` has a non-empty `alt` |
| Button names | every `<button>` / `[role=button]` has text or an `aria-label` |
| Control names | form controls inside a form/`fieldset`/`.formrow` have a label, `aria-label`, `title` or `placeholder` |
| Dialog semantics | the modal carries `role="dialog"` + `aria-modal` (static or set by `app.js`) |
| Tab semantics | the tab strip is `role="tablist"` with at least two `role="tab"` children |
| Live updates | an `aria-live` region (or `role=status/alert/log`) exists |
| Canvas alternatives | every `<canvas>` has `role` + `aria-label`, or `app.js` sets them when it draws |
| Unique ids | no duplicate `id` values in `index.html` |
| Safe-area viewport | the viewport meta contains `viewport-fit=cover` |

The parser is `html.parser` from the standard library, so the checks are structural
(element, attribute, line) rather than substring matches. Attributes that `app.js`
adds at runtime — the chart canvas `role="img"` / `aria-label`, the runner panel's
`role="button"` name — are verified in `app.js` because they are not in the served
HTML.

## What needs manual verification

These cannot be judged from static markup and are **not** covered by the test:

- **Colour contrast** — the dark palette (`style.css`) must keep body text ≥ 4.5:1
  and large text ≥ 3:1. Dimmed/offline cards and `--muted` text are the risky spots;
  verify with the browser accessibility inspector or axe DevTools.
- **Screen-reader flows** — tab order, that the modal traps focus and restores it,
  that the `aria-live` region actually announces phase/telemetry updates, and that
  the chart text alternative is read instead of the canvas. Test with NVDA/JAWS
  (Windows) and VoiceOver (macOS/iOS).
- **Reduced motion** — `@media (prefers-reduced-motion: reduce)` should stop the
  pulse/animation on the connection dot and runner tabs.
- **Touch targets** — primary controls (tabs, runner actions, clickable rows) should
  reach ~44×44 px at phone widths; check at 360 px wide.
- **Keyboard-only operation** — every action reachable without a mouse, with a
  visible focus indicator, including the sortable table headers and runner cards.
- **Reflow / zoom** — at 200 % zoom and 320 px width the layout must not require
  horizontal scrolling for text.

## Known limitations

- Standalone filter `<select>`s in `index.html` (`histHost`, `histStatus`,
  `trendHost`, `trendMetric`, `trendMetric2`, `cmpA`, `cmpB`) and the chart-preset
  `<select>`s built by `app.js` carry no explicit label; they rely on the section
  heading / adjacent chart for context. The test reports these as `NOTE` lines
  (manual check) rather than hard failures, so it stays green on the current markup
  while still surfacing them. Labelling them explicitly is the next improvement.
- Contrast, focus order and motion cannot be checked without a browser; the audit's
  recommended Playwright + axe-core job would extend this module — it is
  deliberately dependency-free for now.

## Findings

- **UX-P3-003** — No accessibility or visual-regression tooling or tests for any UI
  surface (`tests/test_a11y.py`, this document).
- **MOB-P3-003** — No mobile/responsive test coverage; the suite is source-string
  matching only.
- Related UI findings the markup fixes address: UX-P2-002 (dialog semantics),
  UX-P2-003 (ARIA / tab strip / live region), UX-P2-004 (canvas alternatives),
  MOB-P3-002 (`viewport-fit=cover`).
