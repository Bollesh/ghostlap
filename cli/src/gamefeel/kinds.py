"""Which metrics are taste (motion character) and which are forgiveness/feedback/correctness.

Motion character has no universal "good": momentum-heavy, floaty movement is a valid style and
often reads as more game-like than snappy movement. Retuning it toward placeholder ranges made
games look stiff. So by default taste metrics are *described* ("style") rather than flagged,
and `propose` leaves them alone; a user who wants a different feel opts in with `tune: all`
in profile.yaml or `propose --scope all`.
"""

TASTE = frozenset({
    "move.accel_time", "move.decel_time", "move.turnaround_time", "move.top_speed", "move.turn_time",
    "move.slope_speed_ratio", "jump.time_to_apex", "jump.height_max", "jump.height_min", "jump.fall_rise_ratio",
    "jump.fall_time", "jump.air_time", "jump.max_fall_speed", "dash.distance", "dash.duration_frames",
    "camera.follow_lag", "impact.knockback", "attack.startup_frames", "attack.active_frames", "attack.recovery_frames",
})


def in_scope(metric: str, scope: str | None) -> bool:
    return (scope or "forgiveness") == "all" or metric not in TASTE
