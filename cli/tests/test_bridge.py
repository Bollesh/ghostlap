"""Play bridge (spike 1.1): frozen clock, exact input timing, hitstop/pause/reload, determinism."""
import random
import re
import shutil
import time
from pathlib import Path

import pytest

from ghostlap.play.bridge_client import Bridge, BridgeError
from ghostlap.project import find_godot

pytestmark = pytest.mark.godot

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _copy(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".ghostlap", ".godot"))
    return dst


@pytest.fixture
def probe_session(tmp_path):
    proj = _copy(FIXTURES / "bridge_probe", tmp_path / "bridge_probe")
    opened = []

    def _open(name="s", **kw):
        b = Bridge.launch(find_godot(), proj, "res://main.tscn", proj / ".ghostlap/play" / name, **kw)
        opened.append(b)
        return b

    yield _open
    for b in opened:
        b.quit()


def _events(b: Bridge) -> list:
    """The fixture's [iteration, where, what] log, converted to bridge frames."""
    it0 = b.look()["it0"]
    return [[e[0] - it0, e[1], e[2]] for e in b.eval("events")]


def test_frozen_between_commands(probe_session):
    b = probe_session()
    first = b.look()
    assert first["f"] == 0 and first["scene"] == "res://main.tscn"
    time.sleep(0.5)
    assert b.look() == first  # no frame ran while nobody asked
    after = b.step(10)
    assert after["f"] == 10 and after["hash"] != first["hash"]  # the body fell


def test_inputs_land_on_their_frame_in_every_callback(probe_session):
    b = probe_session()
    b.step(20, [{"at": 5, "action": "jump"}, {"at": 6, "action": "jump", "pressed": False},
                {"at": 8, "key": "K"}, {"at": 9, "key": "K", "pressed": False},
                {"at": 11, "mouse": [140, 120]}, {"at": 12, "mouse": [140, 120], "pressed": False}])
    ev = _events(b)
    # exactly once per callback, on the scheduled frame, like a real key press between frames
    assert sorted(e for e in ev if e[2] == "jump") == [[5, "input", "jump"], [5, "physics", "jump"], [5, "process", "jump"]]
    assert sorted(e for e in ev if e[2] == "key_k") == [[8, "input", "key_k"], [8, "physics", "key_k"], [8, "process", "key_k"]]
    # a Button emits `pressed` on release (its default action mode)
    assert [e for e in ev if e[2] == "button"] == [[12, "signal", "button"]]


def test_hitstop_pause_and_reload_keep_the_clock_running(probe_session):
    b = probe_session()
    r = b.step(40, [{"at": 3, "action": "hitstop"}, {"at": 4, "action": "hitstop", "pressed": False}], timeout=10)
    assert r["f"] == 40 and r["time_scale"] == 1.0
    ev = _events(b)
    assert [3, "physics", "hitstop"] in ev and [9, "timer", "hitstop_end"] in ev  # 0.1 s = 6 frames at 60 fps

    r = b.step(60, [{"at": 2, "action": "pause"}, {"at": 3, "action": "pause", "pressed": False}], timeout=10)
    assert r["f"] == 100 and not r["paused"]
    ev = _events(b)
    assert [42, "physics", "pause"] in ev and [71, "pauser", "unpause"] in ev

    r = b.step(5, [{"at": 1, "action": "reload"}, {"at": 2, "action": "reload", "pressed": False}])
    assert r["scene"] == "res://main.tscn" and b.eval("events") == []  # a fresh instance
    assert b.eval("tree.current_scene == scene")


SCRIPT = {1: [{"action": "move_right"}], 30: [{"action": "jump"}], 31: [{"action": "jump", "pressed": False}],
          50: [{"action": "hitstop"}], 51: [{"action": "hitstop", "pressed": False}],
          80: [{"action": "move_right", "pressed": False}, {"action": "move_left"}],
          100: [{"key": "K"}], 101: [{"key": "K", "pressed": False}],
          130: [{"action": "pause"}], 131: [{"action": "pause", "pressed": False}],
          200: [{"action": "move_left", "pressed": False}]}


def _play(b: Bridge, script: dict, total: int, chunk: int, sleep: float = 0.0) -> list[str]:
    f = 0
    while f < total:
        n = min(chunk, total - f)
        b.step(n, [dict(i, at=k - f) for k in range(f + 1, f + n + 1) for i in script.get(k, [])])
        f += n
        if sleep:
            time.sleep(sleep)
    return b.hashes()


def test_same_inputs_same_states_whatever_the_step_size(probe_session):
    one = _play(probe_session("one"), SCRIPT, 240, 240)
    small = _play(probe_session("small"), SCRIPT, 240, 7, sleep=0.02)
    single = _play(probe_session("single"), SCRIPT, 40, 1)
    assert len(one) == 241
    assert one == small
    assert one[:41] == single
    assert _play(probe_session("seed", seed=99), SCRIPT, 240, 240) != one  # the seed reaches game code


def test_bad_requests_are_refused_without_running_frames(probe_session):
    b = probe_session()
    with pytest.raises(BridgeError, match="unknown action"):
        b.step(5, [{"at": 1, "action": "nope"}])
    with pytest.raises(BridgeError, match="outside this step"):
        b.step(5, [{"at": 6, "action": "jump"}])
    with pytest.raises(BridgeError, match="bad token"):
        Bridge(b.session_dir, b.port, "wrong", b.pid, b.info).look()
    assert b.look()["f"] == 0


@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.iterdir() if (p / "project.godot").is_file()))
def test_examples_replay_identically(tmp_path, name):
    proj = _copy(EXAMPLES / name, tmp_path / name)
    pg = (proj / "project.godot").read_text()
    scene = re.search(r'run/main_scene="([^"]+)"', pg).group(1)
    godot = find_godot()
    runs = []
    for label, chunk in (("whole", 300), ("chunked", 13)):
        b = Bridge.launch(godot, proj, scene, proj / ".ghostlap/play" / label)
        try:
            actions = [a for a in b.eval("InputMap.get_actions()") if not a.startswith("ui_")]
            rng, held, script = random.Random(7), set(), {}
            for f in range(1, 301):
                if rng.random() < 0.08:
                    a = rng.choice(actions)
                    script.setdefault(f, []).append({"action": a, "pressed": a not in held})
                    held ^= {a}
            runs.append(_play(b, script, 300, chunk))
        finally:
            b.quit()
    assert runs[0] == runs[1]
    assert len(set(runs[0])) > 100  # the inputs actually moved things
