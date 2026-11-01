"""Offline checks for game_mind's first-person fixes (2026-11-02)."""
import numpy as np

import game_mind as gm


def test_shift():
    rng = np.random.default_rng(1)
    base = rng.random((90, 400)) * 255
    a = base[:, 100:260].astype(np.float32)
    b = base[:, 120:280].astype(np.float32)      # view slid 20 px
    s, peak = gm._shift_x(a, b)
    assert abs(abs(s) - 20) <= 1 and peak > 0.3, (s, peak)


def test_stuck_and_calibrate():
    m = gm.Mind()
    f = np.full((90, 160), 100, np.float32)
    notes = [m._stuck_note(f, "keys w:500") for _ in range(6)]
    assert notes[-1].startswith("STUCK"), notes
    m3 = gm.Mind()                       # walking on (view changes) ...
    walk = [m3._stuck_note(f + i * 9, "keys w:800") for i in range(5)]
    assert not any(walk), walk           # ... is not stuck before 6 repeats
    assert gm._fix_keys("a:500+w:500 space") == "a+w:500 space"
    assert gm.gh.parse("a+w:500 space")
    m2 = gm.Mind()
    assert m2._stuck_note(f, "keys w") == "" and m2._stuck_note(f + 50, "keys s") == ""
    # calibration: wanted 20 px, view slid 10 -> gain must grow
    rng = np.random.default_rng(2)
    base = (rng.random((90, 400)) * 255).astype(np.float32)
    m._turn_probe = (20.0, base[:, 100:260])
    m._calibrate(base[:, 110:270])
    assert m.gain > 1.3, m.gain


def test_act_fps():
    calls = []
    gm.gh.button_down = lambda b: calls.append(("down", b))
    gm.gh.button_up = lambda b: calls.append(("up", b))
    gm.gh.mouse_rel = lambda dx, dy, ms=60: calls.append(("rel", dx, dy))
    gm.gh.run = lambda keys, stop=None: calls.append(("keys", keys))
    m = gm.Mind()
    m._fps = True
    box = (0, 0, 1000, 600)
    r1 = m._act({"thought": "mine", "click": "left"}, box)
    r2 = m._act({"thought": "mine", "click": "left"}, box)
    assert calls == [("down", "left")], calls
    assert "kept held" in r1 and "still holding" in r2
    r3 = m._act({"thought": "turn", "point": [800, 500]}, box)
    assert ("up", "left") in calls and ("rel", 300, 0) in calls, calls
    assert m._held is None and "turned" in r3
    calls.clear()
    m._act({"thought": "place", "click": "right", "hold_ms": 40}, box)
    assert calls == [("down", "right"), ("up", "right")], calls


if __name__ == "__main__":
    for t in (test_shift, test_stuck_and_calibrate, test_act_fps):
        t()
        print("PASS", t.__name__)
    print("pointer_locked now:", gm.pointer_locked())
