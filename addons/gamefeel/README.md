# gamefeel runtime (GDScript)

These scripts run inside headless Godot, launched by the `gamefeel` CLI:

- `scan/scan.gd`: extracts scenes (via SceneState, without instantiating them), scripts,
  input actions and project settings to JSON.
- `probe/runner.gd`: runs a compiled probe plan: builds arenas (`arena.gd`), instances the
  player, injects inputs on exact physics frames and records samples (`sampler.gd`).

You don't need to copy this folder into your project; the CLI runs it from its own install.
- `scan/error_logger.gd` uses the Logger API (Godot 4.5+). On older Godot it's simply never loaded by the CLI; if you copy this folder into a 4.3/4.4 project, the editor will show a parse error for that one file, which is harmless.
