from pathlib import Path

from gamefeel.metrics.base import ProbeData, Run
from gamefeel.metrics.jump import coyote_frames, time_to_apex
from gamefeel.metrics.movement import accel_time, decel_time

ROLES = {"right": "move_right", "left": "move_left", "jump": "jump"}


def _run(frames, name="run", vars=None, dims="2d"):
    return Run({"run": name, "physics_fps": 60, "dims": dims, "size": [24, 40], "vars": vars or {}}, frames, [], Path("x"))


def _pd(runs, plane="x"):
    return ProbeData("p", {"plane": plane, "roles": ROLES}, None, runs, Path("."))


def test_accel_and_decel_constant_rate():
    frames, v = [], 0.0
    for f in range(120):
        held = f < 60
        v = min(v + 10, 200) if held else max(v - 20, 0)
        frames.append({"f": f, "v": [v, 0], "p": [0, 0], "in": ["move_right"] if held else []})
    pd = _pd([_run(frames)])
    assert accel_time(pd).value == round(18 / 60, 4)   # reaches 180 (90%) on the 18th frame
    assert decel_time(pd).value == round(9 / 60, 4)    # 200 -> <=20 after 9 frames


def test_time_to_apex_2d_y_down():
    frames, y, vy = [], 0.0, 0.0
    for f in range(90):
        if f == 5:
            vy = -300.0
        elif f > 5:
            vy += 1000 / 60
        y += vy / 60
        frames.append({"f": f, "v": [0, vy], "p": [0, y], "fl": f < 5 or (f > 6 and y >= 0),
                       "in": ["jump"] if 5 <= f < 40 else []})
    mv = time_to_apex(_pd([_run(frames)]))
    assert abs(mv.value - 0.3) < 0.03


def test_coyote_sweep_with_control():
    def run(k, jumped):
        fr = [{"f": f, "v": [0, -200 if (jumped and f >= 10 + k) else 50], "p": [0, 0],
               "in": ["jump"] if f == 10 + k else []} for f in range(40)]
        return _run(fr, name=f"k={k}", vars={"k": k})
    pd = _pd([run(-1, True), run(0, True), run(1, True), run(2, True), run(3, False), run(4, True)])
    mv = coyote_frames(pd)
    assert mv.value == 2  # first failure ends the window
    bad = coyote_frames(_pd([run(-1, False), run(0, False)]))
    assert bad.value is None and "control" in bad.note
