"""body_sense (changes only, full once) and episodes (self.md block, day
boundary). Run: py -3.13 test_self_awareness.py"""
import datetime
import os
import sys
import tempfile

import body_sense
import episodes


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


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for fn in (test_body, test_day_boundary, test_self_block):
        fn()
        print("PASS", fn.__name__)
