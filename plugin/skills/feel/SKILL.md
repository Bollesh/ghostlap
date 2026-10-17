---
name: feel
description: Understand an unfamiliar Godot 4 game and measure how it FEELS — jump arcs, coyote time, input buffering, acceleration, turning, dashes, attacks, hitstop, knockback, camera follow — then fix it with numbers. Use when the user wants to test, tune or "playtest" game feel, asks why their Godot game feels floaty/sluggish/slippery/unresponsive, wants a feel report, or wants an AI-generated Godot game checked before shipping.
---

# ghostlap feel — measure and fix game feel in Godot

`ghostlap` scans a Godot project into a graph, works out what kind of game it is, runs
scripted micro-tests ("probes") headless at a fixed frame rate, and reports feel metrics
against genre reference ranges with concrete, file:line-linked fixes.

You (the agent) run the CLI, talk to the user at the decision points (confirm what the game
is, choose what to test, judge the clip of the proposed fix), write game-specific probes when
needed, and apply fixes only after the user has watched them and approved.

## Setup

```bash
ghostlap doctor             # needs Godot 4.3+ as `godot` on PATH, or GHOSTLAP_GODOT=/path/to/godot
```

If `ghostlap` isn't installed, ask the user before installing it from a checkout of the
repo (`uv tool install <path-to-ghostlap-repo>` or `pip install <path>`); it isn't on PyPI yet.

Run every command from inside the Godot project (or pass `--project <dir>`). Add `--json`
when you need to parse output. ghostlap writes only to `.ghostlap/` (and Godot's own
`.godot/` import cache); it never edits game files itself.

## The loop

### 1. Understand the game

```bash
ghostlap scan               # incremental; first run imports the project (can take a while)
ghostlap detect             # player, input actions, genre scores with evidence, mechanics, levels, hazards
ghostlap graph profile      # same, compact JSON (< 2k tokens) if you need it
```

Read `scan` warnings: files with errors are still understood via the text fallback, but
probes need the project to load.

### 2. Confirm with the user (always)

Present the detection briefly and ask the user to confirm or correct, e.g.:

> This looks like a **top-down action** game. Player: `res://actors/player.tscn` (CharacterBody2D).
> Actions: move_* + dash. Mechanics: dash, knockback, health. Hazard for hit tests: `res://enemies/slime.tscn`.
> Correct? Anything I got wrong?

Detection is evidence-based but can be wrong — never skip this. Then edit
`.ghostlap/profile.yaml` with their corrections (genres, player scene/node, actions, hazard,
hitboxes, style) and run `ghostlap confirm`.

- `genres` must be pack names from `ghostlap feel packs` (v1: platformer, action_sidescroller, topdown, third_person_3d).
- `style` picks reference ranges inside a pack (e.g. platformer: `precise` or `floaty`). If the
  user says the game is *meant* to be floaty, use that style instead of "fixing" it.
- `hazard` is a scene that damages the player on contact; without it the hit probe is skipped.

### 3. Agree on scope

List what can be tested — don't ask open-ended questions:

```bash
ghostlap feel probe list         # runnable probes for the confirmed genres, plus skipped ones and why
```

> I can test: jump (arc, tap vs hold), coyote time, jump buffer, run start/stop, turnaround,
> input latency, getting hit. Your game also has a **wall-jump** with no built-in probe — want
> me to write one? Test everything, or focus on something?

### 4. Write game-specific probes (only if needed)

Probes are short YAML files in `.ghostlap/probes/`. Use roles (`left/right/up/down/jump/dash/attack`)
or literal InputMap action names. Only use metric names from `ghostlap feel metrics`.

```yaml
# .ghostlap/probes/dash_into_wall.yaml
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

Sweeps and calibration (see the built-in probes with `ghostlap feel packs` → probe files for
examples): `sweep: {var: k, from: 0, to: 12}` runs once per value; `at:` can be an expression
like `"cal.left_floor + k"` using a `calibrate:` run's events (`cal.left_floor`, `cal.landed`,
`cal.apex`, `cal.top_frame`).

Always validate before running, and fix until valid:

```bash
ghostlap feel probe validate .ghostlap/probes/dash_into_wall.yaml
```

### 5. Measure

```bash
ghostlap feel check                       # = ghostlap feel probe run && ghostlap feel analyze
ghostlap feel check --only jump coyote    # just some probes
```

The full report is `.ghostlap/report.md` (and `report.json`). Its suggestions are
**predictions** from simple models. Don't present them as the fix yet.

If runs fail, read the reason (`failed: <probe>: ...`) before anything else: a player that
needs its level (script errors in the arena), a missing hazard, or a player without
`is_on_floor()` (RigidBody) are common. Fix the profile or use a `scene:` probe instead.

### 6. Forgiveness first; motion character is the user's call

Metrics come in two kinds:

- **Forgiveness, feedback, correctness**: coyote time, jump buffer, variable jump height,
  hitstop, invulnerability, hit feedback, input latency, diagonal speed. These help nearly
  every game. `ghostlap feel propose` fixes these by default.
- **Motion character (taste)**: acceleration, stopping, turnaround, jump timing, fall speed,
  dash, camera lag. There's no universal "good". Momentum-heavy, floaty movement is a valid
  style and often reads as *more* game-like than snappy movement. `propose` reports these under
  "Motion character left alone" and does **not** change them unless asked.

Only retune motion when the user wants a different feel. Then pick a `style` (e.g.
platformer `floaty` or `precise`) with them and run `ghostlap feel propose --scope all`, or set
`tune: all` in `profile.yaml`. Show the clip; snappy settings on a plain rectangle can look stiff.

### 7. Verify the fix in a scratch copy, then SHOW it

```bash
ghostlap feel propose        # applies suggestions to .ghostlap/work/<stamp>/ (not the real project), re-measures,
                        # repeats until clean; each value changes at most 2x per pass; forgiveness/feedback only
                        # unless --scope all
ghostlap feel clip           # renders "now" vs "proposed" with identical inputs -> GIF, MP4 and a contact-sheet PNG
```

1. Read `proposal.md`: the net changes, every metric before → after (**measured**), any
   regressions, predictions that were off, and what still needs code (patterns).
2. **Look at the clip's contact sheet yourself** (read the `-sheet.png` with your image
   tool) before saying anything about how it feels. Check that jumps rise and land, nothing
   stands on air, and hits visibly react. Describe only what the frames show; if you're unsure
   what a frame shows, say so rather than guess.
3. Give the user the GIF/MP4 path and a short table of the changes, and **ask them to watch
   it**. A clean report is *not* proof it feels right: the ranges are uncalibrated placeholders.
4. If they say something feels off, record it in their words and try again:

   ```bash
   ghostlap feel feedback "the player falls too fast"   # tightens this project's ranges (profile.yaml)
   ghostlap feel propose && ghostlap feel clip
   ```

   If `feedback` can't map the words, pick the metric with the user:
   `ghostlap feel feedback --metric jump.fall_time --direction low`.
5. Only when the user approves: `ghostlap feel apply` writes the proposal's changes to the
   project (it refuses if those files changed since the proposal). Then
   `ghostlap scan && ghostlap feel check` to confirm in the real project.

For a missing mechanic, which no number can fix, `ghostlap feel pattern <name>` prints a minimal
implementation (coyote_time, jump_buffer, variable_jump, fall_gravity, hitstop, invulnerability,
normalize_diagonal, camera_shake, hit_feedback). Add it with the user's approval, then `propose` again.

Respect intentional choices: `accepted: {jump.time_to_apex: "floaty on purpose — dream level"}` in
`profile.yaml`, or a different `style`.

## Using the graph instead of reading files

```bash
ghostlap graph query who-reads jump              # functions reading an action, with lines
ghostlap graph query exports-of res://player.gd  # tunable properties (+ base scripts)
ghostlap graph query script-of res://player.tscn # script + base chain
ghostlap graph query instanced-by res://player.tscn
ghostlap graph query scenes-with Camera2D        # by node type, group or name
ghostlap graph query path <id a> <id b>
ghostlap graph sql "select name, props from nodes where kind='Property' and name like '%dash%'"
```

## Rules

- Never claim a feel judgment that isn't backed by a metric in the report; cite the number.
- Never call something "fixed" or "feels right" from numbers alone. Show the clip and get the
  user's verdict; their eye outranks the placeholder ranges (record it with `ghostlap feel feedback`).
- Never edit game files without the user's approval. Never edit `.ghostlap/runs/`.
- ghostlap measures *mechanical* feel. Whether the game is *fun* is the user's call — say so
  if asked.
- Probes run the game's code headless; if a project's autoloads do something heavy or
  destructive on start, tell the user before running.
