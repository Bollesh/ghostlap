---
name: gamefeel
description: Understand an unfamiliar Godot 4 game and measure how it FEELS — jump arcs, coyote time, input buffering, acceleration, turning, dashes, attacks, hitstop, knockback, camera follow — then fix it with numbers. Use when the user wants to test, tune or "playtest" game feel, asks why their Godot game feels floaty/sluggish/slippery/unresponsive, wants a feel report, or wants an AI-generated Godot game checked before shipping.
---

# gamefeel — measure and fix game feel in Godot

`gamefeel` scans a Godot project into a graph, works out what kind of game it is, runs
scripted micro-tests ("probes") headless at a fixed frame rate, and reports feel metrics
against genre reference ranges with concrete, file:line-linked fixes.

You (the agent) run the CLI, talk to the user at the two decision points (confirm what the
game is, choose what to test), write game-specific probes when needed, and apply fixes only
after the user approves.

## Setup

```bash
gamefeel doctor             # needs Godot 4.3+ as `godot` on PATH, or GAMEFEEL_GODOT=/path/to/godot
```

If `gamefeel` isn't installed, ask the user before installing it from a checkout of the
repo (`uv tool install <path-to-gamefeel-repo>` or `pip install <path>`); it isn't on PyPI yet.

Run every command from inside the Godot project (or pass `--project <dir>`). Add `--json`
when you need to parse output. gamefeel writes only to `.gamefeel/` (and Godot's own
`.godot/` import cache); it never edits game files itself.

## The loop

### 1. Understand the game

```bash
gamefeel scan               # incremental; first run imports the project (can take a while)
gamefeel detect             # player, input actions, genre scores with evidence, mechanics, levels, hazards
gamefeel graph profile      # same, compact JSON (< 2k tokens) if you need it
```

Read `scan` warnings: files with errors are still understood via the text fallback, but
probes need the project to load.

### 2. Confirm with the user (always)

Present the detection briefly and ask the user to confirm or correct, e.g.:

> This looks like a **top-down action** game. Player: `res://actors/player.tscn` (CharacterBody2D).
> Actions: move_* + dash. Mechanics: dash, knockback, health. Hazard for hit tests: `res://enemies/slime.tscn`.
> Correct? Anything I got wrong?

Detection is evidence-based but can be wrong — never skip this. Then edit
`.gamefeel/profile.yaml` with their corrections (genres, player scene/node, actions, hazard,
hitboxes, style) and run `gamefeel confirm`.

- `genres` must be pack names from `gamefeel packs` (v1: platformer, action_sidescroller, topdown, third_person_3d).
- `style` picks reference ranges inside a pack (e.g. platformer: `precise` or `floaty`). If the
  user says the game is *meant* to be floaty, use that style instead of "fixing" it.
- `hazard` is a scene that damages the player on contact; without it the hit probe is skipped.

### 3. Agree on scope

List what can be tested — don't ask open-ended questions:

```bash
gamefeel probe list         # runnable probes for the confirmed genres, plus skipped ones and why
```

> I can test: jump (arc, tap vs hold), coyote time, jump buffer, run start/stop, turnaround,
> input latency, getting hit. Your game also has a **wall-jump** with no built-in probe — want
> me to write one? Test everything, or focus on something?

### 4. Write game-specific probes (only if needed)

Probes are short YAML files in `.gamefeel/probes/`. Use roles (`left/right/up/down/jump/dash/attack`)
or literal InputMap action names. Only use metric names from `gamefeel metrics`.

```yaml
# .gamefeel/probes/dash_into_wall.yaml
probe: dash_into_wall
title: Dash into a wall
requires: {actions: [right, dash]}
arena: wall                 # flat | ledge | wall | air | hazard | slope | open
# or a real level: scene: {path: res://levels/l2.tscn, spawn: SpawnPoints/WallTest}
frames: 90
inputs:
  - {at: 0, action: right, hold: 60}
  - {at: 20, action: dash, hold: 1}
measure: [dash.distance, impact.knockback]
assert: {dash.distance: {max: 6}}
```

Sweeps and calibration (see the built-in probes with `gamefeel packs` → probe files for
examples): `sweep: {var: k, from: 0, to: 12}` runs once per value; `at:` can be an expression
like `"cal.left_floor + k"` using a `calibrate:` run's events (`cal.left_floor`, `cal.landed`,
`cal.apex`, `cal.top_frame`).

Always validate before running, and fix until valid:

```bash
gamefeel probe validate .gamefeel/probes/dash_into_wall.yaml
```

### 5. Run and report

```bash
gamefeel check                       # = gamefeel probe run && gamefeel analyze
gamefeel check --only jump coyote    # just some probes
```

The full report is `.gamefeel/report.md` (and `report.json`). In chat, summarise only the
most important findings — what feels wrong, the measured number vs the range, and the fix —
grouped by root cause. Mention that ranges are uncalibrated placeholders when the report says so.

If runs fail, read the reason (`failed: <probe>: ...`) before anything else: a player that
needs its level (script errors in the arena), a missing hazard, or a player without
`is_on_floor()` (RigidBody) are common. Fix the profile or use a `scene:` probe instead.

### 6. Fix (with approval) and verify

Suggestions in the report are concrete: property, file:line, current → proposed value, why,
and predicted effect. Findings that share a property are grouped as one **root cause** with
one shared value — apply it once.

1. Propose the changes to the user; apply only what they approve. Respect intentional choices.
2. Edit the property (usually an `@export` default in a script, or a value in a `.tscn`).
   For a missing mechanic, `gamefeel pattern <name>` prints a minimal implementation
   (coyote_time, jump_buffer, variable_jump, fall_gravity, hitstop, invulnerability,
   normalize_diagonal, camera_shake).
3. `gamefeel scan && gamefeel check --only <affected probes>` and confirm the metric moved
   into range **without pushing another one out**. Fixing one thing can expose the next
   (e.g. faster acceleration cancels knockback) — that's expected, iterate.
4. Stop when there are no warnings, or record intentional deviations in `profile.yaml`:
   `accepted: {jump.time_to_apex: "floaty on purpose — dream level"}`.

## Using the graph instead of reading files

```bash
gamefeel graph query who-reads jump              # functions reading an action, with lines
gamefeel graph query exports-of res://player.gd  # tunable properties (+ base scripts)
gamefeel graph query script-of res://player.tscn # script + base chain
gamefeel graph query instanced-by res://player.tscn
gamefeel graph query scenes-with Camera2D        # by node type, group or name
gamefeel graph query path <id a> <id b>
gamefeel graph sql "select name, props from nodes where kind='Property' and name like '%dash%'"
```

## Rules

- Never claim a feel judgment that isn't backed by a metric in the report; cite the number.
- Never edit game files without the user's approval. Never edit `.gamefeel/runs/`.
- gamefeel measures *mechanical* feel. Whether the game is *fun* is the user's call — say so
  if asked.
- Probes run the game's code headless; if a project's autoloads do something heavy or
  destructive on start, tell the user before running.
