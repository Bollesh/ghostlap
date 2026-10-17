# Calibration (not done yet)

Every range shipped in v1 is a **placeholder** (`calibrated: false`), written from common
game-feel guidance and sanity-checked against the bundled examples. Reports say so.

## Method (planned, milestone 1.7)

1. For each pack, pick ≥ 5 open-source Godot games that players widely consider to feel good
   in that genre. Check licences, and record the commit used.
2. Run `ghostlap scan`, `detect`, confirm the profile by hand, and `ghostlap feel probe run` with
   the pack's probes. Store every `report.json`.
3. For each metric: `good` = the range covered by the middle ~80% of reference games, widened
   to the nearest round number; `warn` = the full observed range plus a margin.
4. Publish the table (game, commit, metric values) here, set `calibrated: true`, and note the
   ghostlap version used.
5. Styles (`precise`, `floaty`, …) get their own reference sets.

## Sanity checks so far

- The `*_tuned` examples come back clean and the `*_floaty` / `*_sluggish` / `*_basic`
  examples get their intended findings (enforced by `cli/tests/test_e2e_godot.py`).
- Ten official Godot demo projects (`godot-demo-projects`, commit b761b4c) are detected as the
  expected genre. Detection rules were adjusted while looking at these, so they're not an
  independent accuracy test.
- Visual review of the README GIF showed the platformer `precise` range for `jump.fall_rise_ratio`
  (then 1.3–2.2) produced falls that looked yanked down. It's now 1.15–1.6, and suggestions aim just
  inside the range from the violated edge instead of at its midpoint, to avoid overshooting.
