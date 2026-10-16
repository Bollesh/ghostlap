# Writing a genre pack

A pack is a directory of YAML. Built-in packs live in `cli/src/gamefeel/packs/`; your own can
live in `<project>/.gamefeel/packs/` or any directory listed in `GAMEFEEL_PACKS` (a later pack
with the same name overrides a built-in one).

```
my_pack/
├── pack.yaml
├── probes/*.yaml
└── profiles/<style>.yaml
```

## pack.yaml

```yaml
name: racing_arcade
title: Arcade racing
description: Cars you steer directly; drifting, boosting.
includes: [core]              # packs whose probes/ranges you build on (core = shared character metrics)
dims: [3d]
plane: xz                     # speed plane for metrics: x (side view), xy (top-down), xz (3D)
vars: {move: up, back: down, ground: flat}   # substituted into {move}/{back}/{ground} in included probes
default_style: default
detect:
  requires: {dims: 3d}
  rules:                      # weights add up; negative weights count against the genre
    - {signal: player_type, args: [VehicleBody3D, CharacterBody3D], weight: 3, label: vehicle body}
    - {signal: role_read, args: dash, weight: 2, label: boost input}
    - {signal: player_property, args: "drift|steer", weight: 2, label: steering/drift tuning}
feel:                         # words used in reports, by metric and direction
  move.turnaround_time: {high: boat-like steering}
```

### Detection signals

| signal | args | true when |
|---|---|---|
| `player_type` | list of node types | the detected player is one of them |
| `role_read` / `role_missing` | role | the player's scripts (don't) read the mapped action |
| `calls_any` | list of function names | player scripts call any of them |
| `assigns_match` | `{target: regex, value: regex, ops: [..]}` | an assignment in player scripts matches |
| `gravity_on_velocity` | — | gravity is integrated into `velocity.y` (or RigidBody) |
| `node_in_player` | `{types: [...], name: regex}` | a node under the player matches |
| `player_property` | regex | a player script property name matches |
| `project_node_type` | list of types | any scene contains such a node |
| `group_exists` | regex | a node group name matches |
| `orthographic_camera`, `y_sort`, `planar_movement_3d` | — | see `detect/signals.py` |

A pack's score is the matched positive weight minus matched negative weight, divided by the
total positive weight. `gamefeel detect` drafts the profile with every pack scoring ≥ 0.6.

## Probes

See the probe schema at the top of `cli/src/gamefeel/probes/schema.py`, and the built-in
packs for examples. Rules of thumb:

- Use roles (`left/right/up/down/jump/dash/attack`) and pack vars (`{move}`), not literal action
  names, so the probe works across games.
- Prefer generated arenas; they make results independent of level design.
- Every probe must `measure` existing metrics (`gamefeel metrics`). New metrics are Python
  functions registered with `@metric` in `cli/src/gamefeel/metrics/`.
- Use a sweep with a control run when you're measuring a window (see `coyote.yaml`).

## Profiles (reference ranges)

```yaml
calibrated: false   # set true only with a published calibration (docs/calibration.md)
metrics:
  move.turnaround_time: {good: [0.0, 0.25], warn: [0.0, 0.45]}
```

`good` is "feels right"; inside `warn` is a warning; outside is an error. When a project
selects two genres that both define a metric, their ranges are intersected.
