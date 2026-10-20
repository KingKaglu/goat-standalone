"""body_sense (changes only, full once) and episodes (self.md block, day
boundary). Run: py -3.13 test_self_awareness.py"""
import datetime
import os
import sys
import tempfile

import body_sense
import episodes
import inner_life


class App:
    phone = None
    mic_muted = False
    _limit_hit_at = 0.0


def test_body():
    b = body_sense.BodySense(App())
    full = b.note(full=True)
    assert full.startswith("[body — how you are right now") and "up " in full
    assert b.note() == "", "no change must cost zero tokens"
    App.mic_muted = True
    assert "laptop mic muted" in b.note()
    App.mic_muted = False
    assert "laptop mic back on" in b.note()
    assert b.note() == ""
    assert body_sense.ASKED_RE.search("hey goat how are you")
    assert not body_sense.ASKED_RE.search("open chrome")


def test_day_boundary():
    t = datetime.datetime(2026, 10, 20, 3, 0).timestamp()   # 03:00 = yesterday
    assert episodes.day_of(t) == datetime.date(2026, 10, 19)
    t = datetime.datetime(2026, 10, 20, 6, 0).timestamp()
    assert episodes.day_of(t) == datetime.date(2026, 10, 20)


def test_self_block():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "self.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Me\nkeep this\n{episodes.START}\n{episodes.END}\ntail\n")
    old, episodes.SELF_MD = episodes.SELF_MD, path
    try:
        for i in range(1, 9):
            episodes.write_self(datetime.date(2026, 10, i), f"LINE: day {i}\n- x")
        episodes.write_self(datetime.date(2026, 10, 8), "LINE: day 8 again")
        doc = open(path, encoding="utf-8").read()
    finally:
        episodes.SELF_MD = old
    lines = [ln for ln in doc.splitlines() if ln.startswith("- 2026")]
    assert len(lines) == episodes.KEEP_LINES, lines
    assert lines[0].startswith("- 2026-10-08") and "day 8 again" in lines[0]
    assert "keep this" in doc and doc.rstrip().endswith("tail")


def test_mood():
    noon = datetime.datetime(2026, 10, 20, 12, 0)
    e = lambda u, r="Sir, done.": {"t": 0, "user": u, "reply": r}  # noqa: E731
    assert inner_life.read_mood("open chrome", [], noon)[0] == "steady"
    k, hint = inner_life.read_mood("it's still not working",
                                   [e("why did you close it")], noon)
    assert k == "frustrated" and "fix it first" in hint
    assert inner_life.read_mood("repeat the list", [e("repeat the list")],
                                noon)[0] == "frustrated"
    fails = [e(u, "Sir, that failed.") for u in ("fix wifi", "open the app",
                                                  "check the phone")]
    assert inner_life.read_mood("ok", fails, noon)[0] == "struggling"
    late = datetime.datetime(2026, 10, 21, 3, 10)
    assert inner_life.read_mood("open spotify", [], late)[0] == "late"
    assert inner_life.read_mood("next", [e("a")] * 0 + [e(f"step {i}")
                                for i in range(9)], noon)[0] == "flow"


def test_moment_and_note():
    class Eng:
        _last_exchange = None
        busy = False
    x = inner_life.InnerLife.__new__(inner_life.InnerLife)
    x.app, x._prev_idle = Eng(), 20 * 60
    x._mood, x._note = "steady", ""
    import threading
    x._lock = threading.Lock()
    assert x._moment(5) == "back"          # he came back after 20 min away
    x._prev_idle = 30
    assert x._moment(5) == ""              # he never left
    x._note = "- a thought"
    assert "on your mind" in x.turn_note("open chrome")
    assert "on your mind" not in x.turn_note("open chrome")   # once only


def test_thoughts_block():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "self.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"top\n{inner_life.T_START}\n{inner_life.T_END}\nend\n")
    old, episodes.SELF_MD = episodes.SELF_MD, path
    try:
        inner_life.write_thoughts(["a", "b", "c"], "10-20 23:00")
        inner_life.write_thoughts(["d", "e"], "10-21 01:00")
        doc = open(path, encoding="utf-8").read()
    finally:
        episodes.SELF_MD = old
    lines = [ln for ln in doc.splitlines() if ln.startswith("- ")]
    assert len(lines) == inner_life.KEEP_THOUGHTS
    assert lines[0] == "- 10-21 01:00: d" and doc.startswith("top")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for fn in (test_body, test_day_boundary, test_self_block, test_mood,
               test_moment_and_note, test_thoughts_block):
        fn()
        print("PASS", fn.__name__)
