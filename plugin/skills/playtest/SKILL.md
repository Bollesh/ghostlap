---
name: playtest
description: Play a Godot 4 game yourself, frame by frame from the shell, to prove a goal can be reached ("level 1 can be finished", "the door opens after the key") or to find what breaks, then rule pass / fail / blocked with evidence, a replay check, a report and a captioned video. Use when the user asks you to playtest, play through, beat, check completability, or find bugs by playing their Godot game.
---

# ghostlap playtest — Claude plays the game

`ghostlap play` runs the user's real game with a bridge that **freezes it between your
commands**. Nothing moves while you think. You read the frozen game as compact text, advance
exact frames with exact inputs, and end with a verdict that `play end` checks against evidence.
Every session leaves an input line that replays the run frame for frame.

Run every command from inside the Godot project (or pass `--project <dir>`). Add `--json` when
you need to parse output. ghostlap writes only to `.ghostlap/` and Godot's `.godot/` cache.

## Rules

- **Never edit game files during a playtest.** You're testing the game, not fixing it.
- **`eval` is read-only** unless the user allows setup cheats. `eval --write` turns a pass into `pass (assisted)`.
- **Never claim `pass` without the evidence** `play end` asks for. It refuses a pass that has none.
- **`blocked` is not `fail`.** "I couldn't find the route" or "the budget ran out" is `blocked`.
  `fail` needs a bug incident, or one of the goal's `fail_if` incidents.
- **When you find a game bug, stop and report it.** Don't fix it unless asked.

## 1. Understand

```bash
ghostlap doctor                      # Godot 4.3+ as `godot`, or GHOSTLAP_GODOT=/path
ghostlap scan && ghostlap detect     # player, input actions, genre, levels, hazards → .ghostlap/profile.yaml
ghostlap play preflight              # loads? deterministic? raw-key input? render-dependent? (cached)
```

- Confirm the player and the actions with the user in one line, unless they're obvious. Fix
  `.ghostlap/profile.yaml` if detect got them wrong.
- Give each preflight warning that matters to the user in one line:
  - **`input_style`**: the game reads raw keys. Use `--key` / `--key-hold` instead of action names.
  - **`render_dependent`**: never use `--headless`.
  - **`nondeterminism` / `determinism`**: replays may diverge, so no test can be made.

## 2. Goal

Write `.ghostlap/goals/<name>.yaml` from the user's words, and confirm it in one line:

```yaml
goal: finish_level_1
describe: "From the start of level 1, the player can reach the exit"
start: {scene: res://levels/level_1.tscn}     # or {main: true}
success: "node('Exit').get('reached') == true"  # optional, preferred: a machine check
budget: {frames: 36000, commands: 120, shots: 15}
```

- **Find `success` in the code** before you play: what happens when the level is won? Look for a
  signal, a `change_scene_to_*` call, a flag, or the exit's `body_entered`. Use `ghostlap graph query`
  or read the scripts. Prefer an expression that only becomes true on success.
- **No clean expression?** Leave `success` out. A pass then needs a cited screenshot (step 5).
- **Expressions:**
  - Inputs: `player`, `scene`, `incident`, `tree`.
  - Helpers: `node('Path')` (from the current scene), `group('name')`, `dist_h(a, b)` (in player heights).
  - A bare name reads a member of the current scene.
  - Use `.get('prop')` when a property name is also a built-in function, like `log` or `min`.
- `fail_if: [incident.stuck]` makes a signal count as a failure, when the goal is about it.

## 3. Plan

```bash
ghostlap play start .ghostlap/goals/finish_level_1.yaml     # rendered offscreen by default
ghostlap play look --view player,near,map
```

- `start` prints the first look and a `SHOT HINT first_look`. **Read that PNG once** to see the
  art and the layout.
- **The `map` view:** `#` solid, `=` one-way, `^` hazard, `o` pickup, `E` enemy, `G` goal,
  `P` player, `?` unclassified.
- **`near`** lists objects by distance in player heights.
- Write a 3–6 step route plan as a note: `ghostlap play note "plan: …"`.
- **Check the route against what the player can do.** Measure one jump if you don't know its
  height or distance: hold right and press jump, then compare `player` before and after.

## 4. Play

```bash
ghostlap play until "player.global_position.x > 770" --hold move_right --max 300
ghostlap play until "player.is_on_floor() and player.global_position.x > 960" --hold move_right --press jump@1:20
ghostlap play step -n 30 --hold move_left
ghostlap play look
```

- **Prefer `until` with a hold over `step`.** One `until` replaces dozens of step-and-look rounds.
  It stops on the condition, on any incident, or at `--max`.
- **Holds are declarative:** whatever was held before but isn't listed is released. `--keep` keeps
  holding it.
  - `--press jump@5:12` presses on frame 5 of the command for 12 frames. Hold longer for a higher jump.
  - Also: `--key`, `--key-hold`, `--mouse x,y@k` (click at screen x,y on frame k).
- **Every incident is printed.** Bugs are `error`, `crash`, `hang`, `out_of_bounds` and
  `player_lost`. Signals are `stuck` and `death`. An `until` stops on the incident.
- **Screenshots, only by the rules:**
  - When a response carries `SHOT HINT <reason>: read <path>`, read that PNG before your next action.
  - Otherwise don't use `look --shot`, unless the plan needs something only an image shows. Then say why in a note.
  - Cite what you saw: `ghostlap play note --saw shots/f1260.png "the door is open"`.
- **On a death:**
  - Note why, then `ghostlap play rewind --to <frame before the hazard>` and try a different timing.
  - A rewind replays the line to that frame and sets the rest aside as an attempt. Incidents and
    evidence from an abandoned attempt don't count.
- **Read each result before the next command.** Chaining commands blindly wastes the budget when
  an early one doesn't do what you expected.
- **Moving platforms, elevators and patrols are in the scene files**, as AnimationPlayer tracks
  with times and positions. Read them rather than guessing from views. With `--fixed-fps`, their
  phase is a function of the frame number.
- **Check progress with `eval`** on the game's own counters, like a coin count or a door flag.
  An object being listed in `near` doesn't mean it hasn't been collected yet.
- **`WARNING: Godot released …`** means a window focus change dropped a hold while the game was
  frozen. The bridge pressed it again and recorded it, so carry on. Without render-dependent
  logic, `--headless` avoids it.
- **After 3 failed approaches to the same obstacle,** take a different route, or end `blocked`.
- **If your context gets long or compacts,** `ghostlap play status` restores the goal, the budget, your notes and where you are.
- **The budget is enforced.** When it's used up, only `look`, `note`, `status` and `end` work.

## 5. Verdict

```bash
ghostlap play end --verdict pass --why "node('Exit').reached became true at f=1890" --video
ghostlap play end --verdict fail --why "null instance error when the key is picked up (key.gd:31)"
ghostlap play end --verdict blocked --why "couldn't clear the gap at x=1800: 3 timings tried"
```

**`end` checks the verdict.** On a refusal it says what's missing, and the session keeps running.

| Verdict | Needs |
|---|---|
| `pass` | `success` true on the final line, or `--evidence shots/fN.png`: a shot at or after the last input, cited with `note --saw`. Without `success`, a refused pass returns a `pre_verdict` shot hint: read it, cite it, and end again with it as evidence |
| `fail` | A bug incident (or a `fail_if` one) on the final line |
| `blocked` | Nothing |

Then `end` does three things:
- It replays the final line and compares every frame. It prints `exact`, or `diverged at f=…`
  with what differs. A fail is **confirmed** when the replay hits the same incident again.
- It writes `report.md`, `evidence.json` and `state.jsonl` in the session folder.
- With `--video`, it renders `playtest.mp4` / `.gif` with the goal, the held keys, your notes
  and the verdict. **Read `playtest-sheet.png`** to check the video before you show it.
  Later: `ghostlap play video [session] [--full]`.

## 6. Report to the user

- **Verdict in two lines:** what was proven, and how ("machine-checked: the exit's `reached` became true at f=1890; the replay is exact").
- **The details:** incidents (file:line), repro steps (from `report.md`), and paths to the video and report.
- **Budget used:** frames, commands, and the tokens the CLI printed.
- **To watch it:** `ghostlap play replay <session>` replays it in a window at real time.

**Not built yet (don't promise them):**
- `play sweep` (trying every timing);
- `play watch`;
- `--save-test` / `ghostlap test`;
- `play issue`;
- `goals suggest`;
- the `--axis`, `--aim` and `--look` inputs.
