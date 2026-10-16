# gamefeel

**A linter for game feel in Godot 4.** gamefeel reads an unfamiliar Godot project, works out
what kind of game it is, runs scripted micro-tests headless, and tells you, and your AI agent,
*how it feels* in numbers, with concrete fixes:

```
### ❌ jump.time_to_apex = 0.617 s (good: 0.3–0.45) — floaty
- Set `jump_velocity` -420 → -691 (res://player.gd:7) → time_to_apex 0.62s → 0.38s, height unchanged
- Set `gravity` 700 → 1893 (res://player.gd:8)

### ❌ jump.coyote_frames = 0 frames (good: 4–8) — unforgiving ledges
- Set `coyote_time` 0 → 0.1 (res://player.gd:10)
```

It's built to be driven by an AI coding agent (a Claude Code skill is included), but works
fine by hand.

> **Status: v1 (early).** The reference ranges are **uncalibrated placeholders**. They're
> reasonable starting points, not measured from reference games yet (see
> [docs/calibration.md](docs/calibration.md)). Treat findings as "worth a look", not verdicts.

## What it does

1. **Understands the project.** `gamefeel scan` builds a graph of scenes, scripts, signals,
   exported properties and input actions in `.gamefeel/graph.db` (SQLite). It uses Godot itself
   as the parser (headless, reading scenes without running them), with a text fallback for
   projects that don't load. Rescans are incremental.
2. **Detects what the game is.** `gamefeel detect` finds the player, maps input actions to
   roles, scores genre packs with evidence (`applies gravity: velocity.y += g * delta
   (res://player.gd:27)`), and lists mechanics, levels and hazards. You confirm or correct it.
3. **Measures feel.** `gamefeel check` builds small test arenas (or uses your levels), drives
   the player with frame-exact inputs at a fixed frame rate, records every physics frame, and
   computes metrics: jump arc, coyote time, input buffering, acceleration, stopping, turning,
   dash, attack startup, hitstop, knockback, camera follow and more (`gamefeel metrics`).
4. **Suggests fixes.** Findings point at the exact property (file:line, current → proposed),
   using simple physics models where they exist (a jump keeps its height while its timing
   changes). Findings that share a cause are grouped and get one shared value.

## Quick start

Requirements: Python 3.10+, Godot 4.3+ (`godot` on PATH, or `GAMEFEEL_GODOT=/path/to/godot`).

```bash
# not on PyPI yet: install from a checkout
git clone https://github.com/Bollesh/gamefeel && uv tool install ./gamefeel   # or: pip install ./gamefeel

cd your-godot-project
gamefeel scan          # build the graph (first run imports the project)
gamefeel detect        # what is this game? writes a draft .gamefeel/profile.yaml
#   review/edit .gamefeel/profile.yaml, then:
gamefeel confirm
gamefeel check         # run probes + analyze -> .gamefeel/report.md
```

gamefeel only writes to `.gamefeel/` (plus Godot's own `.godot/` import cache). It runs its
GDScript from outside your project, so nothing needs installing into `addons/`.

## With Claude Code

This repo is also a Claude Code plugin marketplace. Install the skill in one step:

```
/plugin install gamefeel --marketplace Bollesh/gamefeel
```

or, on older Claude Code versions:

```
/plugin marketplace add Bollesh/gamefeel
/plugin install gamefeel@gamefeel
```

(The plugin itself lives in `plugin/`, so installs only fetch the skill. The same commands work
from your shell as `claude plugin ...`. Updates arrive through
`/plugin`; pin a release with `Bollesh/gamefeel#v0.1.0`.) The plugin ships the skill only;
install the `gamefeel` CLI as shown above.

Then ask something like *"why does my jump feel floaty?"* or *"check the game feel"*. The skill
scans, confirms the genre and player with you, asks what to test, writes game-specific probes
if needed, and applies fixes only after you approve them.

## Genre packs (v1)

| pack | for | example probes |
|---|---|---|
| `platformer` | side view with gravity and jump | jump tap/hold, coyote, jump buffer |
| `action_sidescroller` | platformer + attacks | attack startup/active/recovery, combo window |
| `topdown` | free 2D movement, no gravity | diagonal speed, dash, dash cooldown |
| `third_person_3d` | CharacterBody3D (incl. 2.5D) | camera follow, turn-to-face, 30° slope |
| `core` (included by all) | any controlled character | run/stop, turnaround, input latency, getting hit |

Puzzle packs (tile-matching, block-pushing, logic grids, physics puzzles) are planned for
v1.x. Packs are plain YAML, so you can write your own: see [docs/packs.md](docs/packs.md).

## Writing a probe

```yaml
# .gamefeel/probes/dash_into_wall.yaml
probe: dash_into_wall
requires: {actions: [right, dash]}
arena: wall                 # or: scene: {path: res://levels/l2.tscn, spawn: SpawnPoints/A}
frames: 90
inputs:
  - {at: 0, action: right, hold: 60}
  - {at: 20, action: dash, hold: 1}
measure: [dash.distance, impact.knockback]
assert: {dash.distance: {max: 6}}
```

`gamefeel probe validate <file>` checks it; `gamefeel check` picks it up automatically.

## Commands

| command | |
|---|---|
| `scan [--full] [--text-only]` | build/update the graph |
| `detect [--write-profile]` | detect player, actions, genres, mechanics |
| `confirm` | mark the profile confirmed |
| `graph profile` / `graph query NAME …` / `graph sql "…"` | query the graph (`who-reads`, `exports-of`, `script-of`, `instanced-by`, `scenes-with`, `node`, `path`, `search`) |
| `probe list` / `probe validate FILE…` / `probe run [--only …]` | probes |
| `analyze [--run DIR]` / `check` | report / run + report |
| `metrics`, `packs`, `pattern NAME`, `doctor`, `init` | reference & environment |

Every command accepts `--project DIR` and `--json`.

## How it works

- `addons/gamefeel/scan/scan.gd`: headless extractor (SceneState, script reflection,
  InputMap, ProjectSettings).
- `addons/gamefeel/probe/`: probe runner, test arenas and per-frame sampler. Inputs are
  applied at the end of the previous physics tick, so `is_action_just_pressed()` is true on
  exactly the scheduled frame.
- `cli/src/gamefeel/`: graph store, incremental change detection (mtime → hash → uid
  renames), detection, probe compiler, metrics, analysis and suggestions.

## Known limits (v1)

- Mechanical feel only. Whether the game is *fun* is still a human call.
- Probes instance the player into generated arenas. Players that need their level (siblings,
  autoload state) may need a `scene:` probe pointing at a real level.
- RigidBody players have no `is_on_floor()`, so coyote/buffer probes can't measure them.
- `jump.buffer_frames` can read 1 frame high for games that check `is_on_floor()` after
  `move_and_slide()`.
- Camera shake is detected through `Camera2D.offset` / `Camera3D` h/v offsets only.
- Headless timing (`perf.frame_spikes`) excludes rendering.
- C# scripts are understood only through the engine scan of a built project.

## Development

When releasing, bump the version in `pyproject.toml`, `cli/src/gamefeel/__init__.py`,
`plugin/.claude-plugin/plugin.json` and `addons/gamefeel/plugin.cfg` together; a test fails if they differ.

```bash
uv sync
uv run pytest            # end-to-end tests run when Godot is available
uv run gamefeel --help
```

The `examples/` folder has one deliberately-off and one tuned project per pack. The test
suite asserts the off ones get their expected findings, the tuned ones come back clean, and
applying the report's own suggestions to the floaty platformer converges to clean.

## License

MIT
