# Pattern: normalized diagonal movement

Building the input vector from two axes gives length √2 ≈ 1.41 on diagonals, so the player
moves 41% faster diagonally.

```gdscript
# Preferred: get_vector() clamps to length 1 and applies deadzones
var input := Input.get_vector("move_left", "move_right", "move_up", "move_down")

# Or, if you build it yourself:
var raw := Vector2(Input.get_axis("move_left", "move_right"), Input.get_axis("move_up", "move_down"))
var input2 := raw.limit_length(1.0)  # keeps analog stick magnitudes below 1
```

Verify: `move.diagonal_ratio` ≈ 1.0.
