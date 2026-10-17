# Phase 1 progress: items 1.1–1.4

Phase 1's goal is "Claude proves level 1 is completable". After items 1.1–1.4, Claude can start a
Godot game frozen, read it as text, play it from the shell one short command at a time, leave
behind an input line that replays exactly, and is told when the game errors, crashes, hangs or
loses the player. Screenshot checkpoints, verdict rules, video and the skill come next (1.5–1.8).

Status on 2026-10-17: all tests pass on Godot 4.7.2; the play, bridge and view tests also pass on 4.3.
Items 1.1–1.3 are committed on `phase-1`; 1.4 is not committed yet.

## What exists now

```
ghostlap play start .ghostlap/goals/finish_long.yaml --headless
ghostlap play until "player.global_position.x > 770" --hold move_right
ghostlap play until "player.is_on_floor() and player.global_position.x > 960" --hold move_right --press jump@1:20
ghostlap play look --view player,near,map
ghostlap play until goal --hold move_right
ghostlap play end --verdict pass --why "node('Goal').reached became true at f=988"
```

| Layer | File | Does |
|---|---|---|
| Bridge (Godot) | `addons/ghostlap/play/bridge.gd` | Runs the unmodified game and freezes it between commands; frame-exact input; per-frame state hashes; expressions; `until` and goal checks; screenshots |
| Views (Godot) | `addons/ghostlap/play/observe.gd` | `look` as compact text: `ui`, `player`, `near`, `map`, `grid`, `state`, `game` |
| Client | `cli/src/ghostlap/play/bridge_client.py` | Launch (headless, window or offscreen), attach, one JSON-line request per call |
| Session | `cli/src/ghostlap/play/session.py` | Goal file, budget, held inputs, the input line, events |
| CLI | `cli/src/ghostlap/play/commands.py` | `ghostlap play start/look/step/until/eval/note/status/end/kill` |
| Incidents (Godot) | `addons/ghostlap/play/incidents.gd` | Per-frame rules: error, out_of_bounds, player_lost, stuck, death |
| Profile | `cli/src/ghostlap/play/profile.py`, `detect/detect.py` | Which node is the player, what kind of game it is, default views |

## 1.1 — The bridge (spike)

**Question:** can a `--script` main loop run an unmodified game, freeze it between commands, step
exact frames and inject input the game sees exactly like a real key press, deterministically?
**Answer: yes.** Full notes: `docs/spikes/1.1-bridge.md`.

- **Freezing:**
  - The bridge node runs last in every frame.
  - In its `_process` it blocks, serving token-checked JSON lines on 127.0.0.1, until a `step` arrives.
  - While it waits, no physics, timers or tweens advance, and with `--fixed-fps`, time spent thinking changes nothing.
- **Frame-exact input:** an input for frame f is queued at the end of frame f−1 with
  `Input.parse_input_event()` and no manual flush. `_input`, `_physics_process` and `_process`
  then all see it on frame f, once.
  - The v1 feel runner's `Input.action_press` path is wrong: `_process` code never sees the press as "just pressed".
- **Real start scene:** the start scene becomes `current_scene` via `change_scene_to_file`, so
  games that reload or change scenes work unchanged.
- **Hitstop and pause:** iterations keep counting through `time_scale = 0` and `paused`, so a step always returns.
- **Headless UI:** the headless root viewport is 64×64; the bridge resizes it to the project size so UI layout and clicks match a rendered run.
- **Determinism:** identical per-frame hashes whether a run is one big step, 13-frame chunks or
  single frames, on all six examples.
  - Cost: 600 frames in chunks ≈ 1.8 s including Godot startup.

## 1.2 — Views

`look` describes the frozen game as text. `detect` picks the default views per game type.

| Game type (detect) | Default views |
|---|---|
| spatial (platformer, top-down, 3D) | `player`, `near`, `map`, `ui` |
| grid (tiles, boards) | `grid`, `ui`, `state` |
| turn-based | `grid`/`map` + `state` + `ui`, or just `ui`, `state` for card games |
| ui, unknown | `ui`, `state` |

- **`map`:**
  - A character picture around the player from physics point queries: `#` solid, `=` one-way, `^` hazard, `o` pickup, `E` enemy, `G` goal, `P` player.
  - In 3D it's a height grid instead.
- **`grid`:** tile layers plus pieces snapped to the grid, with a legend.
- **`ui`:** visible text, buttons, inputs and bars, each with its on-screen click point.
- **`state`:** the game's own variables, changed ones first.
- **`game`:** an optional `_ghostlap_state()` hook the game can define.
- **Screenshots:** `force_draw` renders the frozen frame without advancing it. Headless sessions say screenshots are unavailable.
- **Window mode:** a visible window takes no desktop keyboard or mouse input, and replays identically to headless.
  - Checked by eye on GNOME: a window frozen for minutes is never flagged "not responding". Godot's X11 windows aren't pinged at all.
- **Cost:** a full platformer look is about 240–330 tokens.
- **Text alone is enough to play:**
  - a Sokoban fixture is solved from the `grid` view;
  - a card battle is won by clicking at the `ui` view's coordinates.

## 1.3 — The `play` CLI

- **Session state:** each command is a fresh process, and state lives in `.ghostlap/play/<id>/`:
  - `session.json`;
  - `inputs.jsonl`, the input line;
  - `events.jsonl`;
  - `hashes.jsonl`;
  - `godot.log`.
- **Goal files:** `goal`, `describe`, `start: {scene}` or `{main: true}`, `success` (an expression), `budget`.
  - The bridge checks `success` every frame and reports the first frame it was true.
- **Inputs:**
  - `--hold` is declarative: anything not listed is released, and a hold carried over isn't re-pressed.
  - `--press jump@5:12` taps jump on frame 5 for 12 frames. A tap that outlasts its command is released on the next command's first frame.
  - Also `--keep`, `--release`, `--key`, `--key-hold`, `--mouse x,y@k`.
- **`until "<expr>"`:** steps until the expression is true. `until goal` uses the goal's `success` expression.
  - Inputs the game never reached are dropped from the input line.
- **Output:** a header line (frame, scene, goal, held inputs, budget), then the default views.
  - Views identical to the last ones shown print as `unchanged`.
  - `--quiet` prints only the header.
- **Budget:**
  - At the frame or command limit, `step` and `until` are refused, with a reason.
  - `look`, `note`, `status` and `end` still work.
  - `until --max` is clamped to the frames left.
  - Screenshots have their own limit.
- **Expressions:**
  - Inputs are `player`, `scene`, `tree` and the engine singletons, plus `node()`, `group()` and `dist_h()`.
  - Unknown names read the current scene's members.
  - Calls that change the game, its input or its RNG are refused unless `eval --write` is given, which is logged as a setup cheat.
- **Crashes and timeouts:** a dead Godot is marked `crashed`, or `abandoned` after the 30-minute idle limit, with the log path.
- **`kill`:** only signals processes that really are this session's bridge.

**The 1.3 check:**
- **The level:** `examples/platformer_floaty/level_long.tscn` is three screens wide, with three pits that reload the level, a step, a floating ledge, two spike strips and a goal.
- **The run:** played from the shell in 19 commands and 988 frames, using about 2,200 tokens of output, without taking a hit.
- **The replay:** the recorded input line, replayed as one step, reaches the goal on the same frame with the same state hash.
- **The test:** the route is now `test_shell_session_finishes_the_long_level`.

## 1.4 — Incidents

The bridge checks these rules at the end of every frame. Bug incidents can support a `fail` verdict; signals only inform.
Each one fires once and re-arms when its condition clears.

| Incident | Class | How it's detected |
|---|---|---|
| `error` | bug | Godot 4.5+: the Logger API. Older: the bridge reads new `ERROR:`/`SCRIPT ERROR:` lines from `godot.log` every frame. Warnings don't count. Each distinct error fires once, with file and line; repeats are counted. Capped at 20 distinct errors. Load errors are reported by `start` |
| `crash` | bug | The CLI finds the process dead. The incident carries a log summary: errors, crash signal, GDScript backtrace, without C++ frames |
| `hang` | bug | No progress for 10 s while Godot is busy. The bridge writes the frame to a progress file at most every 250 ms while stepping. Godot is killed |
| `out_of_bounds` | bug | Player more than 10 heights outside the static level geometry. Side views and 3D leave the top open |
| `player_lost` | bug | A player existed in this scene and has been missing 120 frames, without a scene change |
| `stuck` | signal | Movement with a direction held 90 frames, player moved < 0.25 heights. Left and right held together cancel out |
| `death` | signal | Health ≤ 0; re-instanced in the same scene; or a teleport > 4 heights right after touching a hazard. At most one per 120 frames |

- **Ending commands:** every incident ends an `until`; a `step` runs its frames and lists what fired.
- **Evidence:** each incident carries the player view at that frame, and a screenshot in rendered modes.
- **Expressions:** `incident` is the latest one, e.g. `until "incident.type == 'death'"`.
- **Logging:** the CLI prints `INCIDENT f=… type [class]: why (file:line)` and logs each one to `events.jsonl`; `status` counts them.

**The 1.4 check:**
- **Fixture:** `cli/tests/fixtures/incidents` has one scene per incident: a script error, `OS.crash`, `while true`, a pit with no respawn, a freed player, a wall, and three kinds of death. It also has a clean scene that fires nothing.
- **Versions:** all pass on 4.7.2 and 4.3. On 4.3 the error path goes through `godot.log`.
- **Clean runs:**
  - The 1.3 long-level route fires no incident.
  - 600 frames of random input on all six examples, with two seeds, fire none.

**Found while building it:**
- **`stuck` fired on random input.** Holding left and right at once cancels out. It also re-fired within 5 frames, because it re-armed from a sliding window. Opposite inputs now cancel, and it re-arms only on release or 0.5 heights from where it stuck.
- **A window session looked hung.** A hidden window or a locked screen renders very slowly, so frames stop advancing, which looks like a hang. A real hang burns CPU and a window waiting on the compositor is idle. In rendered modes, idle without progress only counts as a hang after 120 s.
- **The crash message was missing.** A raw log tail was all C++ backtrace, pushing the message out. The incident now carries a summary instead.

**Review before committing** found and fixed:
- **Confirmed false positives:**
  - On 4.5+, `push_warning()` was reported as an `error`, because the Logger API delivers warnings through the same callback.
  - A game with no player (the card battle) fired `player_lost` after 120 frames.
- **Load-time errors:** errors on frame 0, while the scene loads, now appear in `play start`'s output.
- **Error floods:** an error whose message changes each frame no longer produces one incident per frame; incidents are capped at 20 distinct errors.
- **Godot 4.3 messages:** they're no longer cut at their second colon.
- **Duplicate deaths:** "health 0, then reload" is one death, and repeated deaths are reported about once per 2 s.
- **Late geometry:** the level bounds are recomputed before `out_of_bounds` fires, and retried when the level has no geometry yet.
- **Hang detection:**
  - Progress is time-based, so a 2 fps game isn't called hung.
  - The hang incident carries the last frame that made progress.
- **Crash race:** a crashing Godot closes its socket before it exits. The CLI now waits up to 3 s for the exit before deciding, which fixes an occasional generic error instead of a crash incident.

## Engine findings

| Finding | Where | Handling |
|---|---|---|
| `action_press()` from a callback is never "just pressed" in `_process` | 1.1 | Bridge uses buffered `parse_input_event` (the v1 feel runner still has the bug) |
| Headless root viewport is 64×64 | 1.1 | Resized to the project size |
| `Expression` can't see singletons; built-in names (`log`, `min`) shadow members | 1.1 | Singletons passed as inputs; documented |
| `JSON.stringify` sorts keys by default | 1.2 | `sort_keys = false`, so "changed first" survives |
| **Godot 4.3:** an `InputEventAction` for one action releases every other action (`InputMap.event_get_index` ignores the match result; fixed in 4.4) | 1.3 | On < 4.4 the bridge injects the action's mapped key/button event instead |
| One frame with no `current_scene` during `reload_current_scene()` | 1.3 review | `until` and the goal check skip such frames |
| `OS.crash()` / a fatal signal prints the message first, then a long C++ backtrace | 1.4 | Crash evidence is a filtered log summary |
| A rendered window that is hidden or on a locked screen advances very slowly | 1.4 | Hang detection requires a busy CPU in rendered modes |

## Code review fixes (before committing 1.3)

- **Goal check across reloads:** it hit one error when a pit reload left a frame with no current scene, then stopped checking for good, silently.
  - Now it skips frames with no scene, keeps evaluating after errors, and shows the latest error in every look's header.
  - `until` no longer stops on that frame.
- **Typo in `success`:** a `success` expression that doesn't compile used to leave Godot running with the session half-open. `start` now shuts it down and says why.
- **`kill --all`:** it could signal a reused PID from an old session. It now checks that the process is that session's bridge.
- **Session ids:** two sessions started in the same second with the same goal name no longer collide.
- **Dead sessions:** a session found dead is marked (`crashed` or `abandoned`) instead of staying `running`.
- **Write guard:** it now also covers RNG calls (`randf()` would break determinism), input calls, signals, playback and container mutators.
- **Player lookup:** cached per frame, since the goal and `until` checks call it every frame.
- **Regression tests:**
  - a pit reload during `until`;
  - a bad goal expression;
  - RNG and input calls in `eval`.

## Tests

| File | Covers |
|---|---|
| `test_bridge.py` (11) | Frozen clock, input timing in every callback, hitstop/pause/reload, determinism on all examples, refused requests |
| `test_observe.py` (10) | Every view on fixtures (tile platformer, Sokoban, card battle), screenshot, window == headless, detect's default views |
| `test_play_cli.py` (6) | The long level from the shell + replay, holds and taps, write guard, budgets, reload, bad goal, RNG calls |
| `test_incidents.py` (17) | Each planted incident fires (error, crash, hang, out of bounds, player lost, stuck, 3 deaths), an incident ends `until`, a clean run fires none; warnings, no-player games, load errors, error floods and repeated deaths |

## Not done yet, and known issues

- **Planned for later items:**
  - screenshot checkpoints and `play config` (1.5);
  - verdict rules and `report.md` (1.6), since `end` records the verdict but doesn't check it yet;
  - the replay check and captioned video (1.7);
  - the `playtest` skill (1.8).
- **Inputs not built yet:** `--axis`, `--aim`, `--look`.
- **Expression helpers not built yet:** `watch` and `visible_text` (2.5).
- **Moved:**
  - the `hotspots` view, to 2.5;
  - the Lantern Hop (godogen) check, to after phase 1.
- **Open follow-ups:**
  - Port the v1 feel runner (`probe/runner.gd`) to `parse_input_event` (finding 2 above).
  - CI only runs on `main` and PRs, so `phase-1` pushes aren't tested. The last `main` run failed on the 4.3 jobs for an unknown reason (the logs need auth).
  - `test_clip_renders_now_vs_proposal` (feel) takes about 10 minutes locally when its window is hidden. It renders a real window, unlike the play tests.
- **`trapped` and `softlock`** incidents are phase 2 (2.6); the `perf` signal isn't built.
- **Next:** item 1.5, screenshot checkpoints, capture config (`play config`) and the shot-read hook.
