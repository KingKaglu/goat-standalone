"""awareness.py: the stream GOAT keeps while he's silent, its measured self-
state, and the notes the brain gets. Offline, temp folder, no model.
Run: py -3.13 test_awareness.py"""
import os
import sys
import tempfile
import threading
import time

import awareness
import goals
import inner_life
import metrics
import watchers

T = time.time()
MIN = 60.0


def new(folder=None, **kw):
    a = awareness.Awareness(folder or tempfile.mkdtemp(), **kw)
    a.him_since = a._seen_t = a.thought_t = T
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


def test_think_gate():
    seen = []
    old_emit = metrics.emit
    metrics.emit = lambda kind, **f: seen.append((kind, f))
    try:
        a = new()
        a.tick(snap(), T)
        for i in range(25):                             # a pile of small things
            post(a, T + i, "activity", f"he spent {20 + i} min in brave", 1)
        assert a.pressure >= awareness.THINK_AT and not a.ready(), \
            "small things alone wait for one that matters"
        post(a, T + 60, "alert", "Battery 18%, not charging", 5)
        assert a.ready()
        lines = a.pending()
        assert len(lines) == 27 and lines[-1].endswith("Battery 18%, not charging")
        assert len(a.pending(limit=5)) == 5 and "Battery 18%" in a.pending(5)[-1]
        p, n = a.thought("world", dry=True, now=T + 120)
        assert p >= awareness.THINK_AT and n >= 26
        assert a.pressure == 0 and a.peak == 0 and a.gates == 1 and not a.ready()
        g = [f for k, f in seen if k == "aware_gate"]
        assert g and g[0]["dry"] is True and g[0]["why"] == "world"
        post(a, T + 200, "alert", "Download finished: x.zip", 5)
        assert a.pending() == [awareness._hm(T + 200) + " Download finished: x.zip"], \
            "only what came after the last thought"
        assert not any("a thought (" in ln for ln in
                       a.since_note(T + 3600).splitlines()), "gate lines stay private"
        b = awareness.Awareness(a.dir)                  # the mark survives a restart
        assert abs(b.thought_t - (T + 120)) < 1 and b.gates == 1
        a.tick(snap(), T + 3700)                        # the hourly roll-up
        roll = [f for k, f in seen if k == "aware"]
        assert roll and roll[0]["ticks"] >= 2 and roll[0]["ms"] < 50
    finally:
        metrics.emit = old_emit


def test_goals_bounds():
    g = goals.Goals(tempfile.mkdtemp())
    assert g.add("fix it", "he said so", "done") is None, "too vague"
    assert g.add("email him the hours file", "", "sent") is None, "no because"
    one = g.add("Email him the hours file.", "he asked at 21:02 and I never did",
                "the email shows in Sent", days=30, now=T)
    assert one["id"] == "g1" and one["expires"] == round(T + 7 * 86400), "7 days at most"
    assert g.add("email him the hours file", "x", "y", now=T) is None, "alike"
    for i, t in enumerate(("check the TikTok draft state", "rebuild the phone app",
                           "look at why Georgian speech was slow", "tidy the downloads folder")):
        assert g.add(t, "he asked", "he confirms", now=T + i)
    assert g.add("one goal too many for me", "he asked", "ok", now=T) is None
    assert len(g.active()) == goals.MAX_ACTIVE
    assert "g1: Email him the hours file (because: he asked at 21:02" in g.lines()
    assert g.close("G2", "dropped", "no longer matters")["state"] == "dropped"
    assert g.close("g2") is None and g.close("g99") is None
    # a machine check closes its goal; time closes the rest
    d = g.from_event({"kind": "alert", "what": "disk", "text": "C: is down to 20 GB free"}, T)
    assert d["check"] == "disk" and d["source"] == "drive"
    assert g.from_event({"kind": "alert", "what": "disk", "text": "again"}, T) is None
    assert g.from_event({"kind": "alert", "what": "battery", "text": "x"}, T) is None
    assert g.sweep(T + 60, {"disk": lambda: False}) == []
    done = g.sweep(T + 120, {"disk": lambda: True})
    assert [x["state"] for x in done] == ["done"] and done[0]["text"].startswith("free up")
    gone = g.sweep(T + 8 * 86400, {})
    assert len(gone) == 4 and all(x["state"] == "expired" for x in gone)
    assert g.active() == []
    again = goals.Goals(g.dir)                          # survives a restart
    assert again.d["n"] == 6 and "## Closed" in open(again.md, encoding="utf-8").read()


def test_parse_reply():
    r = inner_life.parse_reply(
        "- He asked twice for the hours email and I dropped it.\n"
        "- The charger fell out again at 14:02.\n"
        "SAY: Want me to send the hours file now?\n"
        "GOAL: send him the hours file | because he asked at 21:02 and it never "
        "went | done when the email is in Sent | days 2\n"
        "DONE: g3\nDROP: g1, G4")
    assert len(r["thoughts"]) == 2 and r["say"].startswith("Want me")
    assert r["goal"] == {"text": "send him the hours file",
                         "why": "he asked at 21:02 and it never went",
                         "test": "the email is in Sent", "days": 2.0}
    assert r["done"] == ["g3"] and r["drop"] == ["g1", "g4"]
    r = inner_life.parse_reply("- one thought\nSAY: none\nGOAL: be a better assistant")
    assert r["say"] == "" and r["goal"] is None, "a goal with no because/test is dropped"
    assert inner_life.parse_reply("gibberish")["thoughts"] == []


def test_world_thought():
    """A thought with no talk at all: gated, widened, and it moves my goals."""
    class Eng:
        _last_exchange = time.monotonic() - 3600
        busy = False
    d = tempfile.mkdtemp()
    x = inner_life.InnerLife.__new__(inner_life.InnerLife)
    x.app, x._note, x._lock = Eng(), "", threading.Lock()
    x._mood = inner_life.read_mood("", [], inner_life.datetime.datetime.now())[0]
    x.st = {"reflect_t": time.time() + 9, "say": []}    # no talk newer than this
    x.aware = awareness.Awareness(d)
    x.goals = goals.Goals(d)
    x.aware.on_event = x._on_event
    asked = []

    async def fake_ask(prompt, system):
        asked.append(prompt)
        return ("- The C drive has been low since this morning.\n"
                "SAY: Want me to clear old downloads? C: is down to 20 GB.\n"
                "GOAL: look at why Georgian speech was slow | because he repeated "
                "himself three times at 14:10 | done when I measured it | days 2\n"
                "DONE: g1")
    old = (inner_life.episodes._ask, inner_life.THOUGHTS, inner_life.write_thoughts,
           inner_life.STATE, inner_life._plan, os.environ.get("GOAT_AWARE_THINK"))
    inner_life.episodes._ask = fake_ask
    inner_life.THOUGHTS = os.path.join(d, "thoughts.md")
    inner_life.write_thoughts = lambda *a: None
    inner_life.STATE = os.path.join(d, "inner.json")
    inner_life._plan = lambda: {"five_hour": 40, "seven_day": 50}
    try:
        assert not x._world_ready(), "nothing noticed yet"
        x.aware.note("alert", "C: is down to 20 GB free", 5, what="disk")
        assert [g["text"] for g in x.goals.active()] == ["free up space on the C: drive"]
        for i in range(8):
            x.aware.note("presence", f"he's back after {3 + i} h", 3)
        assert x.aware.ready()
        os.environ["GOAT_AWARE_THINK"] = "off"
        assert not x._world_ready()
        os.environ["GOAT_AWARE_THINK"] = "on"
        Eng._last_exchange = time.monotonic() - 30      # mid-conversation
        assert not x._world_ready()
        Eng._last_exchange = time.monotonic() - 3600
        inner_life._plan = lambda: {"five_hour": 85}    # quota too high
        assert not x._world_ready() and x.aware.ready(), "the pressure waits"
        inner_life._plan = lambda: {"five_hour": 40}
        assert x._world_ready()
        x.reflect("world")
        p = asked[0]
        assert "(no new talk)" in p and "C: is down to 20 GB free" in p
        assert "- g1: free up space on the C: drive (because:" in p
        acts = {g["text"] for g in x.goals.active()}
        assert acts == {"look at why Georgian speech was slow",
                        "free up space on the C: drive"}, acts
        assert x.goals.get("g1")["state"] == "active", \
            "a goal with a machine check is closed by the check, not by a thought"
        assert x.st["worlds"] == 1 and x.st["reflects"] == 1 and len(x.st["say"]) == 1
        assert not x.aware.ready() and x.aware.pressure == 0
        assert "I thought about what I noticed" in " ".join(texts(x.aware, "thought"))
        assert "low since this morning" in open(inner_life.THOUGHTS, encoding="utf-8").read()
        assert "on your mind" in x.turn_note("open chrome")
        assert "[goals — " in x.turn_note("how are you")
        # an hour apart, three a day, and a failed call is not retried at once
        for i in range(8):
            x.aware.note("presence", f"he's back after {13 + i} h", 3)
        assert x.aware.ready() and not x._world_ready(), "too soon after the last"
        x.st["reflect_at"] = time.time() - 2 * 3600
        assert x._world_ready()
        x.st["worlds"] = inner_life.WORLD_PER_DAY
        assert not x._world_ready()
        x.st["worlds"] = 1

        async def boom(prompt, system):
            raise RuntimeError("no network")
        inner_life.episodes._ask = boom
        try:
            x.reflect("world")
            raise AssertionError("should have raised")
        except RuntimeError:
            pass
        x.st["reflect_at"] = time.time() - 2 * 3600
        assert not x._world_ready() and x.aware.ready(), "backs off, keeps the pressure"
        os.environ["GOAT_AWARE_THINK"] = "dry"
        x._think_fail = 0.0
        assert not x._world_ready() and not x.aware.ready(), "dry: logged, nothing spent"
        assert len(asked) == 1
    finally:
        (inner_life.episodes._ask, inner_life.THOUGHTS, inner_life.write_thoughts,
         inner_life.STATE, inner_life._plan) = old[:5]
        if old[5] is None:
            os.environ.pop("GOAT_AWARE_THINK", None)
        else:
            os.environ["GOAT_AWARE_THINK"] = old[5]


def test_rollback_on_waking():
    a = new()
    a.tick(snap(body={"phone": "phone not connected",
                      "rollback": "watchdog rolled back a bad build of me at 02:00"}), T)
    assert texts(a, "body") == ["watchdog rolled back a bad build of me at 02:00"]
    assert goals.DRIVES[("body", "rollback")][3] is None

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
                          "thoughts": {"talk": 0, "world": 0},
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
               test_think_gate, test_goals_bounds, test_parse_reply,
               test_world_thought, test_rollback_on_waking,
               test_self_line_and_summary, test_trim,
               test_inner_life_wiring, test_tick_is_cheap):
        fn()
        print("PASS", fn.__name__)
