from gamefeel.feedback import interpret, tighten


def test_interpret_common_complaints():
    assert ("jump.fall_time", "low") in interpret("the player falls too fast, it looks unnatural")
    assert ("jump.fall_time", "high") in interpret("jump feels floaty")
    assert interpret("it's slippery when I stop") == [("move.decel_time", "high")]
    assert ("impact.feedback", "low") in interpret("hitting the spikes looks very wonky, the hits don't register")
    assert interpret("the camera lags behind") == [("camera.follow_lag", "high")]
    assert interpret("I like it") == []


def test_tighten_moves_range_past_the_value():
    rng = {"good": [0.24, 0.45], "warn": [0.18, 0.6]}
    new, _ = tighten(rng, 0.28, "low")           # "falls too fast" at 0.28 s
    assert new["good"][0] > 0.28 and new["good"][1] == 0.45 and new["warn"][0] <= 0.28
    new, _ = tighten({"good": [1.15, 1.6]}, 1.44, "high")
    assert new["good"][1] < 1.44
    same, msg = tighten(rng, 0.5, "high")        # already above the range: nothing to change
    assert same == rng and "flagged" in msg
