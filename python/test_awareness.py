"""awareness.py: the stream GOAT keeps while he's silent, its measured self-
state, and the notes the brain gets. Offline, temp folder, no model.
Run: py -3.13 test_awareness.py"""
import os
import sys
import tempfile
import threading
import time

import awareness
import inner_life
import metrics
import watchers

T = time.time()
MIN = 60.0


def new(folder=None, **kw):
    a = awareness.Awareness(folder or tempfile.mkdtemp(), **kw)
    a.him_since = a._seen_t = T
    return a


def snap(**kw):
    s = {"idle": 0.0, "quiet": 9999.0, "heard": 9999.0, "front": "brave",
         "busy": False, "engine_down": False, "up": 600.0, "boot": T - 9e5,
         "cpu": 3.0}
    s.update(kw)
    return s


def post(a, t, kind, text, sal):
    """note() at the test's clock instead of the wall clock."""
    a._tnow = t
    try:
        return a.note(kind, text, sal)
    finally:
        a._tnow = None


def texts(a, kind=None):
    return [e["text"] for e in a.events() if kind in (None, e["kind"])]


def test_presence_and_app():
    a = new()
    a.tick(snap(), T)                                   # here, in brave
    assert a.him == "here" and a.app == "brave"
    for i in range(1, 80):                              # 40 min in brave
        a.tick(snap(), T + i * 30)
    a.tick(snap(front="code"), T + 40 * MIN)            # one hop: not a switch
    a.tick(snap(), T + 40.5 * MIN)
    assert a.app == "brave" and not texts(a, "activity")
    a.tick(snap(front="code"), T + 41 * MIN)
    a.tick(snap(front="code"), T + 41.5 * MIN)          # held two ticks
    assert a.app == "code"
    assert texts(a, "activity") == ["he spent 41 min in brave"]
    # he stops touching anything: quiet is stamped when the input stopped
    a.tick(snap(front="code", idle=6 * MIN), T + 50 * MIN)
    assert a.him == "quiet" and abs(a.him_since - (T + 44 * MIN)) < 1
    assert "he went quiet (no keys, mouse or talk)" in texts(a, "presence")
    a.tick(snap(front="code", idle=3 * 3600), T + 4 * 3600)
    a.tick(snap(front="code", idle=2), T + 4 * 3600 + 30)
    back = [e for e in a.events() if e["text"].startswith("he's back")]
    assert a.him == "here" and len(back) == 1 and back[0]["sal"] == 3, back
    assert "3 h" in back[0]["text"]


def test_my_own_job_is_not_him():
    """My clicks reset Windows' idle clock, and a finished job resets the
    exchange clock: neither says he is here."""
    a = new()
    a.tick(snap(idle=10 * MIN), T)
    assert a.him == "quiet"
    a.tick(snap(idle=1, busy=True, quiet=5), T + 30)    # I'm clicking
    assert a.him == "quiet"
    a.tick(snap(idle=10 * MIN, quiet=3), T + 60)        # my job just ended
    assert a.him == "quiet"
    a.tick(snap(idle=10 * MIN, busy=True, heard=4, quiet=4), T + 90)
    assert a.him == "here"                              # he spoke


def test_body_changes_only():
    a = new()
    a.tick(snap(body={"phone": "phone not connected"}), T)
    assert not texts(a, "body"), "the first look is the baseline"
    a.tick(snap(body={"phone": "phone not connected",
                      "quota5": "5-hour quota high (83%, resets 17:00)"}), T + 30)
    a.tick(snap(body={"phone": "phone not connected",
                      "quota5": "5-hour quota high (84%, resets 17:00)"}), T + 60)
    a.tick(snap(), T + 90)                              # a tick without a look
    assert len(texts(a, "body")) == 1, texts(a, "body")  # 83 -> 84 is no news
    a.tick(snap(body={"phone": "phone not connected",
                      "limit": "hit Claude's usage limit, resets 17:00"}),
           T + 20 * MIN)
    got = texts(a, "body")
    assert "5-hour quota healthy again" in got and any("usage limit" in g for g in got)
    assert a.health()[0] == "limited"
    a.tick(snap(engine_down=True), T + 21 * MIN)
    a.tick(snap(engine_down=False), T + 22 * MIN)
    assert "my Claude engine is back" in texts(a, "body")


def test_wake_and_sleep():
    d = tempfile.mkdtemp()
    a = new(d)
    a.tick(snap(up=120), T)
    assert texts(a, "wake") == ["my awareness ran for the first time"]
    a.tick(snap(), T + 30)
    a.tick(snap(), T + 8 * 3600)                        # a hole between ticks
    assert any(t.startswith("the laptop slept from") for t in texts(a, "wake"))
    a._save(T + 8 * 3600)
    b = new(d)                                          # next life, 5 h later
    b.tick(snap(up=120, boot=T + 12 * 3600), T + 13 * 3600 + 120)
    woke = [t for t in texts(b, "wake") if t.startswith("I woke up")]
    assert woke and "off about 5 h" in woke[0] and "shut down" in woke[0], woke
    b._save(T + 13 * 3600 + 200)
    c = new(d)                                          # a quick restart
    c.tick(snap(up=60), T + 13 * 3600 + 300)
    assert any(t.startswith("I restarted at") for t in texts(c, "wake"))


def test_note_rides_once_after_silence():
    a = new()
    a.tick(snap(heard=30, quiet=30), T)
    post(a, T + 10, "alert", "Battery 18%, not charging", 5)
    assert a.since_note(T + 60) == "", "mid-talk: nothing to tell"
    post(a, T + 5 * MIN, "alert", "Download finished: setup.exe", 5)
    post(a, T + 6 * MIN, "body", "phone linked", 1)     # too small to tell
    a.tick(snap(idle=40 * MIN, heard=40 * MIN), T + 40 * MIN)
    a.tick(snap(idle=2, heard=41 * MIN), T + 41 * MIN)  # he's back
    note = a.since_note(T + 41 * MIN + 5)
    assert note.startswith("[while he was silent")
    assert "Download finished: setup.exe" in note and "he's back after" in note
    assert "Battery 18%" not in note, "told before the silence = old news"
    assert "phone linked" not in note
    assert a.since_note(T + 41 * MIN + 9) == "", "once only"
    for i in range(12):                                 # capped at NOTE_MAX
        post(a, T + 50 * MIN + i, "alert", f"thing {'abcdefghijkl'[i]}", 5)
    a.tick(snap(heard=20 * MIN), T + 70 * MIN)
    lines = a.since_note(T + 70 * MIN).splitlines()
    assert len(lines) == 1 + awareness.NOTE_MAX + 1 and "smaller things" in lines[-1]


def test_same_line_not_twice():
    a = new()
    assert a.note("alert", "Download finished: a1.zip", 5)
    assert not a.note("alert", "Download finished:  a1.zip", 5), "said already"
    assert a.note("alert", "Download finished: a2.zip", 5)
    post(a, time.time() + awareness.SAME_S + 1, "alert", "Download finished: a1.zip", 5)
    assert len(texts(a)) == 3, "after ten minutes it is news again"
    assert not a.note("x", "   ", 1)


def test_gate_is_a_dry_run():
    seen = []
    old_on, old_emit = metrics.ON, metrics.emit
    metrics.emit = lambda kind, **f: seen.append((kind, f))
    try:
        a = new()
        a.tick(snap(), T)
        for i in range(4):
            a.note("alert", f"alert {i}", 5)
        assert a.pressure >= awareness.THINK_AT
        a.tick(snap(), T + 30)
        assert a.pressure == 0 and a.gates == 1
        g = [f for k, f in seen if k == "aware_gate"]
        assert g and g[0]["dry"] is True and g[0]["events"] >= 4
        assert not any("worth a thought" in ln for ln in
                       a.since_note(T + 3600).splitlines()), "gate lines stay private"
        fired = []
        b = new(on_gate=lambda p, n: fired.append((p, n)))
        b.tick(snap(), T)
        for i in range(4):
            b.note("alert", f"alert {i}", 5)
        b.tick(snap(), T + 30)
        assert fired and fired[0][0] >= awareness.THINK_AT
        # the hourly roll-up
        a.tick(snap(), T + 3700)
        roll = [f for k, f in seen if k == "aware"]
        assert roll and roll[0]["ticks"] >= 3 and roll[0]["ms"] < 50
    finally:
        metrics.ON, metrics.emit = old_on, old_emit


def test_self_line_and_summary():
    a = new()
    a.tick(snap(up=3 * 3600, body={"ram": "I'm using 1.6 GB of RAM"}), T)
    a.note("alert", "Battery 18%, not charging", 5)
    line = a.self_line(T + 10 * MIN)
    assert line.startswith("[self — ") and "up 3 h" in line
    assert "he's here, in brave" in line and "health strained" in line
    assert "(1 alerts)" in line and "no model" in line
    s = metrics.summarize([
        {"t": 1, "kind": "aware", "ticks": 120, "ms": 0.5, "ms_max": 2.0, "events": 4},
        {"t": 2, "kind": "aware", "ticks": 120, "ms": 1.5, "ms_max": 9.0, "events": 8},
        {"t": 3, "kind": "aware_gate", "pressure": 21, "events": 9, "dry": True},
        {"t": 4, "kind": "spoke_first", "outcome": "answered", "why": "back"},
        {"t": 5, "kind": "spoke_first", "outcome": "passed", "why": "quiet"}])
    assert s["aware"] == {"hours": 2, "ticks": 240, "tick_ms": 1.0,
                          "tick_ms_max": 9.0, "events_per_day": 144.0,
                          "gates": 1, "gates_dry": 1,
                          "spoke_first": {"answered": 1, "passed": 1}}, s["aware"]
    assert metrics.summarize([])["aware"]["tick_ms"] is None


def test_trim():
    d = tempfile.mkdtemp()
    old, awareness.STREAM_MAX = awareness.STREAM_MAX, 4000
    try:
        a = new(d)
        for i in range(120):
            a.note("alert", f"line {i} " + "x" * 40, 0)
        assert os.path.getsize(a.stream) > 4000
        b = new(d)
        ev = b.events()
        assert os.path.getsize(b.stream) <= 2100 and ev[-1]["text"].startswith("line 119")
    finally:
        awareness.STREAM_MAX = old


def test_inner_life_wiring():
    """turn_note carries the notes; the tick looks around with real senses;
    a watcher alert lands in the stream; a line said first gets its outcome."""
    class Eng:
        _last_exchange = time.monotonic() - 3600
        busy = False
        loop = None
        phone = None
        suppressed = False

        def emit(self, *a):
            pass
    eng = Eng()
    x = inner_life.InnerLife.__new__(inner_life.InnerLife)
    x.app, x._prev_idle, x._mood, x._note = eng, 0.0, "steady", ""
    x._lock = threading.Lock()
    x.turn_note("open chrome")                          # settles the mood line
    assert x.turn_note("open chrome") == "", "no awareness yet: old behaviour"
    x.aware = awareness.Awareness(tempfile.mkdtemp())
    eng._inner = x
    x._perceive()                                       # real senses, real clock
    assert x._ticks == 1 and x.aware._last_tick is not None
    s = inner_life.sense(eng)
    assert s["quiet"] > 3500 and s["heard"] is None and isinstance(s["front"], str)
    watchers.Watchers(eng, tempfile.mkdtemp()).alert(
        "battery", "Battery's at 18 percent.", "Battery 18%, not charging")
    note = x.turn_note("how are you")
    assert "[while he was silent" in note and "Battery 18%" in note
    assert "[self — " in note
    assert "[while he was silent" not in x.turn_note("open chrome")
    # he answers a line I said first
    seen = []
    old = metrics.emit
    metrics.emit = lambda kind, **f: seen.append((kind, f))
    try:
        x._spoke = (time.monotonic() - 200, "back")
        eng._his_last = time.monotonic() - 100
        x._spoke_result()
        x._spoke = (time.monotonic() - 200, "quiet")
        eng._his_last = time.monotonic() - 900
        x._spoke_result()
        x._spoke = (time.monotonic() - 5, "quiet")      # too early to tell
        x._spoke_result()
    finally:
        metrics.emit = old
    assert [f["outcome"] for k, f in seen] == ["answered", "passed"], seen
    assert x._spoke is not None


def test_tick_is_cheap():
    a = new()
    a.tick(snap(), T)
    t0 = time.perf_counter()
    for i in range(1, 501):
        a.tick(snap(), T + i * 30)
    ms = (time.perf_counter() - t0) * 1000 / 500
    print(f"     awareness tick, nothing new: {ms:.3f} ms")
    assert ms < 5, ms
    eng = type("E", (), {"_last_exchange": time.monotonic(), "busy": False})()
    import psutil
    proc = psutil.Process()
    t0 = time.perf_counter()
    for _ in range(50):
        inner_life.sense(eng, proc)
    ms = (time.perf_counter() - t0) * 1000 / 50
    print(f"     real senses, one look: {ms:.3f} ms")
    assert ms < 25, ms


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for fn in (test_presence_and_app, test_my_own_job_is_not_him,
               test_body_changes_only, test_wake_and_sleep,
               test_note_rides_once_after_silence, test_same_line_not_twice,
               test_gate_is_a_dry_run, test_self_line_and_summary, test_trim,
               test_inner_life_wiring, test_tick_is_cheap):
        fn()
        print("PASS", fn.__name__)
