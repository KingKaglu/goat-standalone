"""screen_sense (marks merge, change boxes), game_hands (sequence language,
anti-cheat), game_pilot (rule conditions on synthetic frames, plan checks).
No screen, no input. Run: py -3.13 test_sight_games.py"""
import sys
import time

import numpy as np

import game_hands as gh
import game_pilot
import screen_sense as ss


def test_merge_names_and_dedup():
    ui = [{"src": "ui", "kind": "button", "name": "", "rect": (10, 10, 110, 40)},
          {"src": "ui", "kind": "button", "name": "Save", "rect": (10, 50, 110, 80)}]
    ocr = [{"src": "ocr", "kind": "text", "name": "Alpha", "rect": (40, 18, 80, 32)},
           {"src": "ocr", "kind": "text", "name": "Save", "rect": (40, 58, 80, 72)},
           {"src": "ocr", "kind": "text", "name": "Status idle",
            "rect": (200, 10, 300, 30)}]
    out = ss.merge(ui, ocr, 1000 * 1000)
    names = [(e["kind"], e["name"]) for e in out]
    assert ("button", "Alpha") in names, names       # nameless button adopts caption
    assert names.count(("button", "Save")) == 1      # caption not duplicated
    assert ("text", "Save") not in names
    assert ("text", "Status idle") in names
    assert [e["rect"][1] for e in out] == sorted(e["rect"][1] for e in out)


def test_diff_boxes():
    a = np.zeros((100, 100), np.uint8)
    b = a.copy()
    b[40:60, 10:30] = 200
    boxes = ss.diff_boxes(a, b, (1000, 500, 400, 400))
    assert len(boxes) == 1
    l, t, r, bt = boxes[0]
    assert 1000 + 10 * 4 - 40 <= l <= 1000 + 10 * 4 and r >= 1000 + 30 * 4
    assert ss.diff_boxes(a, a.copy(), (0, 0, 400, 400)) == []
    assert "nothing" in ss._describe_change([], (0, 0, 10, 10))


def test_sequence_language():
    st = gh.parse("w:800 space*2 a+d:300 wait:200 rclick:150 mouse:+5,-7 "
                  "down:shift up:shift up:300")
    ops = [s["op"] for s in st]
    assert ops == ["keys", "keys", "keys", "keys", "wait", "click", "mouse",
                   "down", "up", "keys"]
    assert st[0]["ms"] == 800 and st[3]["keys"] == ["a", "d"]
    assert st[5]["button"] == "right" and st[6]["dx"] == 5 and st[6]["dy"] == -7
    assert st[9]["keys"] == ["up"] and st[9]["ms"] == 300   # arrow, not release
    for bad in ("w:x", "nosuchkey", "w:40000", "mouse:5"):
        try:
            gh.parse(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should be refused")


def test_anticheat():
    for t in ("Rainbow Six Siege", "VALORANT", "Counter-Strike 2",
              "Fortnite", "Roblox"):
        assert gh.anticheat_block({"title": t, "pid": -1}), t
    assert gh.REFUSAL.format(why="x").startswith("I won't drive that one")


def _frame():
    f = np.full((200, 300, 3), 255, np.uint8)       # BGR white
    f[50:80, 100:140] = (16, 16, 224)                # red block (BGR)
    return f


def test_pilot_conditions():
    p = game_pilot.Pilot()
    fbox = (1000, 100, 300, 200)
    f = _frame()
    hit, xy = p.test({"color": [224, 16, 16], "tol": 30, "min_px": 50},
                     f, fbox, None, {})
    assert hit and abs(xy[0] - (1000 + 120)) <= 3 and abs(xy[1] - (100 + 65)) <= 3
    hit, _ = p.test({"color": [16, 192, 16], "tol": 20}, f, fbox, None, {})
    assert not hit
    hit, _ = p.test({"color": [224, 16, 16], "in": [1000, 100, 50, 50]},
                    f, fbox, None, {})
    assert not hit                                   # outside its area
    hit, _ = p.test({"pixel": [1110, 160], "rgb": [224, 16, 16]}, f, fbox, None, {})
    assert hit
    hit, _ = p.test({"pixel": [1110, 160], "rgb": [224, 16, 16], "not": True},
                    f, fbox, None, {})
    assert not hit
    g = f.copy()
    g[100:200, :] = 0
    assert p.test({"change": 18}, g, fbox, f, {})[0]
    assert not p.test({"change": 18}, f, fbox, f.copy(), {})[0]
    st = {}
    assert p.test({"every_ms": 50}, f, fbox, None, st)[0]
    assert not p.test({"every_ms": 50}, f, fbox, None, st)[0]
    time.sleep(0.06)
    assert p.test({"every_ms": 50}, f, fbox, None, st)[0]


def test_plan_checks():
    p = game_pilot.Pilot()
    p._check_rule({"if": {"always": True}, "do": {"keys": "space"}})
    p._check_rule({"if": {"text": "game over"}, "do": "stop"})
    for bad in ({"if": {}, "do": "stop"},
                {"if": {"weird": 1}, "do": "stop"},
                {"if": {"always": True}, "do": {"dance": 1}},
                {"if": {"always": True}, "do": {"keys": "notakey"}},
                {"if": {"template": "no/such.png"}, "do": "stop"}):
        try:
            p._check_rule(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} should be refused")
    try:
        p.start({"rules": [{"if": {"always": True}, "do": "stop"}]},
                {"title": "Rainbow Six Siege", "pid": -1, "hwnd": 1})
    except PermissionError as e:
        assert "won't drive" in str(e)
    else:
        raise AssertionError("anti-cheat game must be refused")
    assert not p.status_d.get("running")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for fn in (test_merge_names_and_dedup, test_diff_boxes,
               test_sequence_language, test_anticheat, test_pilot_conditions,
               test_plan_checks):
        fn()
        print("PASS", fn.__name__)
