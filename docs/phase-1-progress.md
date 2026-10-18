# Phase 1 progress: items 1.1–1.8

Phase 1's goal is "Claude proves level 1 is completable". **It's met:**
- **The run:** following the `playtest` skill from a cold start, Claude collected all 21 coins
  in Godot's official 2D platformer demo, using 87 of its 120 commands.
- **The verdict:** `play end` accepted the pass on a machine check (the game's coin counter
  reached 21).
- **The replay:** it replayed all 3,382 frames identically, rendered and compared frame by
  frame, then made the captioned video now at the top of the README.

Claude can now:
- start a Godot game frozen and read it as text;
- play it from the shell one short command at a time;
- be told about errors, crashes, hangs and lost players, and which screenshot to read when the
  text may be wrong;
- rewind and try again;
- end with a verdict that `play end` checks, confirms by replay, and turns into a report and a video.

Status on 2026-10-18: all 130 tests pass on Godot 4.7.2, and the 87 play, bridge and view tests on 4.3.
Items 1.1–1.6 are committed on `phase-1`; 1.7 and 1.8 aren't committed yet.

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
| CLI | `cli/src/ghostlap/play/commands.py` | `ghostlap play preflight/start/look/step/until/eval/note/rewind/config/status/end/replay/video/kill` |
| Verdict | `cli/src/ghostlap/play/verdict.py` | The rules `end` enforces, the final input line, `evidence.json`, `report.md` |
| Replay | `cli/src/ghostlap/play/replay.py` | The replay check, `play replay`, `play video` (Movie Maker + ffmpeg); also drives `rewind` |
| Video HUD (Godot) | `addons/ghostlap/play/hud.gd` | The video's captions: goal, held keys, notes, incidents, verdict |
| Preflight | `cli/src/ghostlap/play/preflight.py` | Load check, determinism smoke, code checks; cached |
| Skill | `plugin/skills/playtest/SKILL.md` | How Claude plays: understand, goal, plan, play, verdict, report |
| Incidents (Godot) | `addons/ghostlap/play/incidents.gd` | Per-frame rules: error, out_of_bounds, player_lost, stuck, death |
| Shots (Godot) | `addons/ghostlap/play/shots.gd` | Screenshot checkpoints: captures on events, new-UI detection, rolling rotation |
| Settings | `cli/src/ghostlap/play/config.py` | `play config` keys and where each value came from |
| Read hook | `plugin/hooks/hooks.json` | Logs which session screenshots Claude reads |
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

## 1.5 — Screenshot checkpoints

The bridge captures freely; Claude reads a shot only when a response carries a `shot_hint` (design §3.3.1). Capturing costs milliseconds; reading costs about 700 tokens.

**Auto captures** go to `shots/f<frame>.png`, one file per frame, listed in `events.jsonl` with their triggers:

| Trigger | Fires on |
|---|---|
| `start` | Frame 0 |
| `scene_change` | The scene's path changed (a reload is a death, not a change) |
| `incident` / `death` | Any incident; the shot path is attached to the incident |
| `success` | The goal's `success` expression became true |
| `ui` | A new popup, dialog or end screen, shot 15 frames later for fade-ins. A toast or a screen flash doesn't count |
| `rolling` | Multiples of `every` (600), keeping the last `keep` (5). Shots cited with `--saw` or read by Claude are kept |

**Hints** (one per response, the rest in `also`):

| Hint | When |
|---|---|
| `first_look` | Frame 0 |
| `scene_changed` | The scene changed since the last response |
| `repeat_death` | A death within 3 heights of an earlier death in the same scene |
| `expectation_missed` | `until` hit `--max`, or `stuck` fired |
| `ui_without_text` | New UI with no readable text (icon buttons) |
| `unclassified_nearby` | A new `?` cell or a `near` entry of kind unknown; at most every 300 frames |

**Settings:** defaults < `ghostlap.cfg` < goal `shots:` < `start --rolling-shots` < `play config`. `play config` alone lists each value and its source. Live changes apply from the next frame.

**Read hook:** `plugin/hooks/hooks.json` runs `ghostlap play log-read --hook` after a Read of a session shot. It appends `{kind: shot_read, f, path, reason}`, where `reason` is the hint that pointed at the shot, else `requested`, else `auto:<triggers>`. `play status` shows the read count.

Other changes: deaths now carry `at` (where the player died) and `repeat_of`. `look` applies the session's `look.*` settings. Step output lists new UI. Headless sessions say "screenshots: off" and give no hints.

## 1.6 — Verdict rules, the input line and the report

`play end` checks the verdict before it closes the game. A refused verdict leaves the session running, logs a `refused` event and says what's missing:

| Verdict | Accepted when | Recorded as |
|---|---|---|
| `pass` | The goal's `success` was true on a frame of the final input line | `pass (machine-checked)` |
| `pass`, no `success` | `--evidence` names a shot taken at or after the last input, and a `note --saw` on the final line cites it. Otherwise the refusal carries a `pre_verdict` hint with a fresh shot of the frozen frame | `pass (visual)` |
| `pass` with an eval write on the final line | As above | `pass (assisted)` |
| `fail` | A bug incident on the final line, or one of the goal's `fail_if` incidents (`incident.stuck`). Signals alone are refused | `fail`, unconfirmed until the replay check (1.7) |
| `blocked` | Always | not a game bug |

Every `--evidence` frame must be on the final line: a frame after its end is refused.

**`play rewind --to F`** restarts the game in the session's display mode and replays the input line up to F in one fast pass, with captures off:
- Eval writes are replayed on their frames, after that frame runs, as they happened.
- The replay's state hashes are compared with the first run frame by frame. A mismatch is reported as the first frame that diverged.
- Inputs after F move to `attempts/<n>.jsonl`. Shots after F and the old `godot.log` move to `attempts/<n>/`. Eval writes after F are dropped.
- Held inputs at F are rebuilt from the line, and a goal met after F is cleared.
- Replayed frames don't count against the frame budget. The rewind counts as a command.

**The final line in `events.jsonl`:** a rewind to R abandons every earlier event after R. `verdict.split_line` gives the final events plus one list per attempt. Verdicts, the report's timeline and its incidents use only the final line.

**`end` writes:**
- `evidence.json`: verdict, basis (machine, visual, incident or blocked), where the line ends and its last input, and each evidence item with the player's state on that frame. Fails carry `confirmed: null` until 1.7. Cited shots are pinned.
- `state.jsonl`: the player's `pos`, `vel` and `floor` on every frame, sampled by the bridge (new `samples` command).
- `report.md`: verdict and basis, why, session facts, evidence with embedded shots, repro steps, incidents, attempts, timeline and budget, including shots read.
  - Repro steps compress the line into phrases: `hold move_right 0.02–2.00 s`, `tap jump at 1.40 s`, `click at (10, 20) at 2.17 s`.

**A bug from 1.4, found here:** error incidents carry their own `kind` (script or engine), and it overwrote `kind: incident` in `events.jsonl`. So `status` didn't list error incidents in its recent events, and the new fail rule couldn't see them. The field is now `error_kind`.

## 1.7 — Replay check and captioned video

**The replay check.**
- `play end` replays the final input line in the session's display class (headless, or rendered when the session rendered) and compares every frame's state hash.
- The result is `exact`, or `diverged at f=N` with the first player field that differs, or "a node other than the player", plus likely causes from the cached preflight.
- It goes into `replay.json`, `evidence.json` and the report.
- **A fail is confirmed** when the replay hits the same incident type within 2 frames. A crash or hang is confirmed by the replay crashing or hanging too, since their frame is only known roughly. Otherwise the verdict is `fail (unconfirmed)`.
- A session that crashed or hung keeps the crashing command's inputs up to the last progress frame, and the replay runs 10 s past it.

**The bridge replays, not the v1 runner.** The design planned a `replay` plan type in the feel runner, but that runner presses actions with `Input.action_press`, which `_process` code never sees as just pressed. Replaying through the bridge uses the session's own input path. `rewind` uses the same `replay.drive`.

**`play replay [session] [--window|--headless] [--fast]`** is the exact repro: real time in a window by default, compared with `hashes.jsonl`.

**`play video [session] [--full]`, or `end --video`:**
- A Movie Maker replay with the caption HUD (`hud.gd`, given to the bridge as `--hud=<json>`):
  - the goal and its description;
  - a clock;
  - chips for the inputs held (a tap stays up 8 frames, so it's visible at 30 fps);
  - each note for 3 s from its frame;
  - an incident or "goal met" flash for 2 s;
  - the verdict banner from the final frame on.
- **Holding the final frame:** Movie Maker only writes frames it renders, and the bridge is blocked inside the final frame when it quits. So after the line ends, the replay pauses the game tree and runs 2 s more, rendering the frozen final frame with the banner. An ffmpeg `tpad` hold added nothing.
- **Highlights:** sessions over 30 s keep ±3 s around each note, incident, evidence frame and the success frame, plus the start and end (`--full` keeps everything).
- **Output:** `playtest.mp4`, a GIF stepped down until it's ≤ 8 MB, and `playtest-sheet.png` (24 frames across the video, so Claude can check it). The AVI is deleted.

## 1.8 — Preflight, the skill, and a real game

**`ghostlap play preflight [--scene] [--force]`**:
- **Boots:** boots the start scene headless twice for 300 frames. That checks it loads (no bug incident in 60 frames) and is deterministic (the two boots' hashes match).
- **Code scan:** text-scans the project's scripts and scenes, outside `addons/`, for:
  - raw-key input (`input_style`);
  - nondeterminism (`randomize()`, unseeded `RandomNumberGenerator`, clocks, threads, physics on a separate thread);
  - render-dependent nodes;
  - pause and time-scale use;
  - `get_playback_position()`.
- It's a text scan rather than graph queries, so it works before `ghostlap scan`.
- **Cache:** `.ghostlap/preflight.json`, keyed by a fingerprint of scripts, scenes and `project.godot`.
- **On `play start`:** prints its warnings, or "not run for this version", and warns about a headless session in a game with render-dependent logic.
- **`play start` now imports the project** (`godot --import`) when `.godot/` is missing. A fresh clone with textures didn't load before.

**The skill**, `plugin/skills/playtest/SKILL.md`:
- the rules (no game edits, read-only eval, no pass without evidence, blocked isn't fail, report bugs rather than fix them);
- the six steps, with the commands and what each output means;
- a list of what isn't built yet, so the skill doesn't promise it.

**The playthrough.** Godot's official 2D platformer demo (godotengine/godot-demo-projects, MIT). The level has no win state, so the goal was "collect all 21 coins", checked by the pause menu's coin counter. From a cold start, following the skill:
1. **Understand:** scan, detect (player, actions and enemy found correctly), preflight (clean).
2. **Goal:** found the counter in the code and counted the coins with `eval`.
3. **Plan and measure:** the first jump was ignored, correctly: the player was still falling from its spawn point, and this game only jumps from the floor.
4. **The route:** the elevators that make the upper level reachable are in `level.tscn`. One screenshot, requested and noted, fixed a misread of the 64 px map.
5. **Result:** `pass (machine-checked)`; replay exact (rendered, 3,382 frames in 60 s); 87/120 commands, 3,502 frames, ~7.4k tokens of CLI output.

**Bugs the playthrough found, all fixed:**

| Bug | Since | Fix |
|---|---|---|
| Sessions ran at `--fixed-fps 60` whatever the project's tick rate. This game runs physics at 120, so each frame was two ticks, and the report's times and the video speed were off by 2×. Replays used 60 too, so they still matched | 1.3 | Sessions run at the project's `physics_ticks_per_second`, stored as `fps` in `session.json`. Rewind, replay and video reuse it, and older sessions fall back to 60 |
| In a window, Godot releases every pressed input when the window gains or loses focus (X11). Holds vanished behind the input line: the player stopped while the session said `move_right` was held | 1.2 | The bridge notes which injected holds are down when it freezes. When the next step starts, any released during the freeze are pressed again on that step's first frame. The step reports `input_lost`, the CLI warns and records a release+press on that frame (same frame: just_pressed true, just_released false, as live). The bridge's no-focus flag doesn't stop the window getting focus |
| `stuck` compared the two ends of its 90-frame window, so walking right and then back left fired it | 1.4 | The window restarts whenever the held direction changes |
| A `look` after any incident crashed: `shot_hint` iterated a look's `incidents`, which is a count | 1.5 | Only a step's list is iterated |
| `step` / `until` had no `--view` | 1.3 | Added |

Rewinding past the lost hold reported `diverged at f=1359`, honestly. The line after f=1358 says `move_left` was held, but the live game didn't see it. The replay followed the line and still reached the same state at f=2755.

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
| PyYAML (YAML 1.1) reads a bare `on:` key as `true` | 1.5 | The goal loader maps `true` back to `shots.on` |
| A window that gains or loses focus makes Godot release every pressed input, even with `WINDOW_FLAG_NO_FOCUS` set after start (the window still got focus) | 1.8 | The bridge presses lost holds again after a freeze and records it (above) |
| `--fixed-fps` must equal the project's physics tick rate for one frame to be one tick | 1.8 | Sessions read `physics_ticks_per_second` |
| `Input.release_pressed_events()` isn't exposed to scripts | 1.8 | Tests simulate a focus release with `Input.action_release()` |
| Movie Maker doesn't write the frame the bridge is blocked in when it quits | 1.7 | The video pauses the game tree and runs on 2 s, so the final frame is rendered |

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
| `test_incidents.py` (18) | Each planted incident fires (error, crash, hang, out of bounds, player lost, stuck, 3 deaths), an incident ends `until`, a clean run fires none; warnings, no-player games, load errors, error floods and repeated deaths |
| `test_verdict.py` (9) | Pass refused off the final line and accepted on it, rewind replays inputs and eval writes to the same hash, fail needs a bug incident on the final line, signals vs `fail_if`, visual pass and `pre_verdict`, headless pass without `success`, repro phrases, `split_line` |
| `test_replay.py` (14) | Exact replay of a pass and of `play replay`, divergence located (`chaos.tscn`), fails confirmed (error, crash), confirmation rules, highlight windows, the video (HUD data, the 2 s hold, frame count), every example replays exactly, re-pressed holds, the project tick rate |
| `test_preflight.py` (2) | Code findings (comments, seeded generators and `addons/` skipped), load and determinism checks, the cache and its invalidation, `start`'s preflight line |
| `test_shots.py` (17) | Each capture trigger and hint on its fixture, none on a flat run, toast/flash ignored, ui settle and flush, rolling rotation with a live change and a pinned shot, settings layers and errors, headless, the real hook command |

## Not done yet, and known issues

- **Next (phase 2):**
  - `--save-test` and `ghostlap test` (2.1);
  - `play watch`, `click`, `type` and `visible_text` (2.5);
  - `sweep`;
  - `goals suggest`;
  - `play issue`.
- **Lost holds mid-run:** a focus change while frames run (not while frozen) still releases holds unnoticed. The bridge only checks after a freeze, because a game may release an action itself. The replay check would show a divergence. Headless avoids it, or xvfb (`--offscreen`) when installed.
- **The Read hook only runs where the plugin is installed:** the playthrough read 2 screenshots, but `status` says "0 read" because the hook wasn't active in this shell.
- **Inputs not built yet:** `--axis`, `--aim`, `--look`.
- **Expression helpers not built yet:** `watch` and `visible_text` (2.5). So a `fail` can't yet rest on a watch-backed contradiction.
- **`state.jsonl`** has the bridge's own format (`f`, `pos`, `vel`, `floor`), not the v1 Sampler's; 4.4 (passive feel metrics) adapts one to the other.
- **`play rewind`** replays at full speed in the session's display mode. A rendered rewind still renders every frame; replaying headless and switching back isn't possible in one process.
- **Moved:**
  - the `hotspots` view, to 2.5;
  - the Lantern Hop (godogen) check, to after phase 1.
- **Open follow-ups:**
  - Port the v1 feel runner (`probe/runner.gd`) to `parse_input_event` (finding 2 above).
  - CI only runs on `main` and PRs, so `phase-1` pushes aren't tested. The last `main` run failed on the 4.3 jobs for an unknown reason (the logs need auth).
  - `test_clip_renders_now_vs_proposal` (feel) takes about 10 minutes locally when its window is hidden. It renders a real window, unlike the play tests.
- **`trapped` and `softlock`** incidents are phase 2 (2.6); the `perf` signal isn't built.
- **The Stop hook** (end or explain an open session) is listed with the hooks in design §5; it isn't built.
- **The playthrough session** in the README ran before the tick-rate fix, at 60 frames/s on a 120-tick game (two ticks per frame). Its numbers are frames at 60/s.
