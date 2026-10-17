from ghostlap.analyze import _group_root_causes, classify


def test_classify():
    rng = {"good": [0.3, 0.45], "warn": [0.25, 0.55]}
    assert classify(0.4, rng) == ("ok", None)
    assert classify(0.5, rng) == ("warn", "high")
    assert classify(0.1, rng) == ("error", "low")
    assert classify(None, rng) == ("not_measured", None)
    assert classify(3, None) == ("info", None)


def test_root_cause_shares_one_value():
    def finding(metric, value, proposed):
        return {"metric": metric, "value": value, "range": [0.0, 0.15],
                "suggestions": [{"kind": "property", "id": "prop:res://p.gd::acceleration", "current": 500.0,
                                 "proposed": proposed, "model": "inverse", "metric_value": value}]}
    a = finding("move.accel_time", 0.4, 2667.0)
    b = finding("move.turnaround_time", 0.85, 5000.0)
    _group_root_causes([a, b])
    assert a["root_cause"] == b["root_cause"]
    pa, pb = a["suggestions"][0]["proposed"], b["suggestions"][0]["proposed"]
    assert pa == pb
    assert 0.85 * 500 / pa <= 0.15 and 0.4 * 500 / pa <= 0.15  # both predicted in range
