"""Engine logic tests — the manual two-lane brain (2026-07-17 redesign).

No audio, no SDK subprocess, no API cost: the clients and TTS are mocks.
Verifies: talk lane (Gemini, middle) vs work lane (Claude, left), manual
dispatch (no escalation), graceful Claude-out, plus the unchanged pure bits
(compact/rotation, power watcher, local_hands, UI clamps).

Run:  python test_engine_router.py   (prints PASS/FAIL per case)
"""
import asyncio
import os
import tempfile
import time
from collections import deque

import goat_app as g
from claude_agent_sdk import ResultMessage


class MockTTS:
    def __init__(self):
        self.spoken = []

    def mark_reply(self):
        pass

    def new_turn(self):
        pass

    def cancel(self):
        pass

    def say(self, text):
        self.spoken.append(text)


class MockClient:
    def __init__(self, script=None, ctx_after=1000):
        self.queries = []
        self.models = []
        self.script = script or []
        self.ctx_after = ctx_after

    async def query(self, text, **kw):
        self.queries.append(text)

    async def set_model(self, model):
        self.models.append(model)

    async def get_context_usage(self):
        return {"totalTokens": self.ctx_after}

    async def receive_messages(self):
        for msg in self.script:
            yield msg

    async def receive_response(self):
        for msg in self.script:
            yield msg


def make_app(client):
    app = g.GoatApp.__new__(g.GoatApp)
    app.emit = lambda *a: app.events.append(a)
    app.events = []
    app.client = client
    app.tts = MockTTS()
    # talk lane
    app.talk_brain = "gemini flash"
    app.talk_busy = False
    app.talk_client = None
    app._talk_client_model = None
    app._talk_lock = asyncio.Lock()
    # work lane
    app.work_model = "sonnet 5"
    app.hard_model = "fable 5"
    app.model = g.MODEL_FAST
    app.busy = False
    app.last_user_text = None
    app.claude_out = False
    app.claude_reset = ""
    app.suppressed = False
    app._hold_deltas = False
    app._delta_buf = ""
    app._say_buf = ""
    app.usage_in = app.usage_out = 0
    app._limit_warned = False
    app._last_exchange = time.monotonic()
    app._current_task = ""
    app._work_started = 0.0
    app._last_tool = ""
    app._turn_has_tools = False
    app._last_ctx = 0
    app._exchanges = deque(maxlen=g.HANDOFF_KEEP)
    app._reply_acc = ""
    app._rotate_only = False
    app._pending_handoff = ""
    app._compacting = False
    app.language = "en"
    app._local_unseen = []
    return app


class FakeLocal:
    """Stands in for local_llm (Gemini transport) — deterministic, no network."""
    def __init__(self, up=True, reply=None):
        self.up = up
        self.reply = reply
        self.chats = []
        self.noted = []
        self.offline = []
        self.LOCAL_KA = False
        self.LOCAL_NAME = "gemini flash"

    def available(self):
        return self.up

    def chat(self, text, on_delta=None, lang="en", offline=False):
        self.chats.append(text)
        self.offline.append(offline)
        if self.reply and self.reply != "ESCALATE" and on_delta:
            on_delta(self.reply)
        return self.reply

    def note_exchange(self, user, reply):
        self.noted.append((user, reply))


def _hands_roundtrip(lh):
    import tempfile as _t
    p = os.path.join(_t.gettempdir(), "goat_hands_test.txt")
    w = lh.execute("write_file", {"path": p, "content": "hello goat"})
    r = lh.execute("read_file", {"path": p})
    try:
        os.remove(p)
        os.remove(p + ".goat-bak")
    except OSError:
        pass
    return w.startswith("wrote") and r == "hello goat"


def _hands_delete(lh):
    """delete_file really deletes (file + folder) and errors honestly."""
    import tempfile as _t
    p = os.path.join(_t.gettempdir(), "goat_del_test.txt")
    open(p, "w").write("x")
    d1 = lh.execute("delete_file", {"path": p})
    gone = not os.path.exists(p)
    d2 = lh.execute("delete_file", {"path": p})  # already gone -> ERROR
    folder = os.path.join(_t.gettempdir(), "goat_del_dir")
    os.makedirs(folder, exist_ok=True)
    open(os.path.join(folder, "inner.txt"), "w").write("x")
    d3 = lh.execute("delete_file", {"path": folder})
    folder_gone = not os.path.exists(folder)
    return (d1.startswith("deleted") and gone
            and d2.startswith("ERROR") and "no such" in d2
            and d3.startswith("deleted folder") and folder_gone)


def _hands_sizes(lh):
    """list_dir shows a real size next to files (anti size-hallucination)."""
    import tempfile as _t
    folder = os.path.join(_t.gettempdir(), "goat_size_dir")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "big.bin"), "wb") as f:
        f.write(b"\0" * 2048)
    out = lh.execute("list_dir", {"path": folder})
    lh.execute("delete_file", {"path": folder})
    return "2.0 KB" in out and "big.bin" in out


def result_msg(ctx=1000, is_error=False, result=None):
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=is_error, num_turns=1, session_id="test",
        usage={"input_tokens": ctx, "cache_read_input_tokens": 0,
               "cache_creation_input_tokens": 0},
        result=result)


def has(app, kind):
    return any(e[0] == kind for e in app.events)


PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


async def main():
    # Route transcript logging to a temp file for the whole run.
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
    tmp.close()
    old_tr = g.TRANSCRIPT_FILE
    g.TRANSCRIPT_FILE = tmp.name
    try:
        # ---- talk lane (middle, Gemini) ----
        # 1. plain talk -> Gemini answers, Claude work client untouched
        g.local_llm = fake = FakeLocal(up=True, reply="All quiet here.")
        c = MockClient()
        app = make_app(c)
        await app._talk("how are you doing")
        check("talk -> Gemini answers, work client untouched",
              fake.chats == ["how are you doing"] and c.queries == []
              and not app.busy and app._local_unseen
              and has(app, "turn_done"),
              f"chats={fake.chats} queries={c.queries}")

        # 2. talk keeps working even while a WORK turn is running (rule 2:
        #    Gemini talks while Fable works) — work client stays untouched
        g.local_llm = fake = FakeLocal(up=True, reply="About two minutes out.")
        c = MockClient()
        app = make_app(c)
        app.busy = True  # a work turn is in flight
        await app._talk("how's it going")
        check("talk works concurrently with a running work turn",
              fake.chats == ["how's it going"] and c.queries == []
              and app.busy,  # work turn left running
              f"chats={fake.chats} queries={c.queries} busy={app.busy}")

        # ---- work lane (left, Claude) ----
        # 3. work order -> working brain (work_model), tools, no Gemini
        g.local_llm = fake = FakeLocal(up=True, reply="nope")
        c = MockClient()
        app = make_app(c)
        app.work_model = "fable 5"
        await app._work("fix the scroll bug in the app")
        check("work -> working brain (work_model), Gemini skipped",
              fake.chats == [] and c.queries == ["fix the scroll bug in the app"]
              and c.models == [g.MODEL_FULL] and app.busy
              and has(app, "work_start"),
              f"chats={fake.chats} models={c.models} queries={c.queries}")

        # 4. hard dispatch -> hard_model, not work_model
        c = MockClient()
        app = make_app(c)
        app.work_model = "sonnet 5"
        app.hard_model = "opus 4.8"
        await app._work("do the heavy refactor", hard=True)
        check("hard work -> hard_model",
              c.models == [g.MODEL_OPUS]
              and c.queries == ["do the heavy refactor"],
              f"models={c.models} queries={c.queries}")

        # 5. NO auto-routing: a work verb typed as plain talk stays on Gemini
        #    (his rule 3 — nothing escalates itself; he dispatches by hand)
        g.local_llm = fake = FakeLocal(up=True, reply="Here's how I'd approach it.")
        c = MockClient()
        app = make_app(c)
        await app._talk("fix the scroll bug in the app")
        check("work verb in plain talk does NOT auto-route to Claude",
              fake.chats == ["fix the scroll bug in the app"]
              and c.queries == [] and not app.busy,
              f"chats={fake.chats} queries={c.queries}")

        # 6. manual voice dispatch: addressing the working brain by name goes
        #    straight to the work lane, Gemini skipped
        g.local_llm = fake = FakeLocal(up=True, reply="should not be used")
        c = MockClient()
        app = make_app(c)
        app.work_model = "fable 5"
        await app._talk("working brain, build the parser")
        check("addressing 'working brain' dispatches to the work lane",
              fake.chats == [] and c.queries == ["working brain, build the parser"]
              and c.models == [g.MODEL_FULL],
              f"chats={fake.chats} queries={c.queries} models={c.models}")

        # 7. addressing the HARD brain by name uses hard_model
        c = MockClient()
        app = make_app(c)
        app.hard_model = "opus 4.8"
        await app._talk("hard brain, run the heavy migration")
        check("addressing 'hard brain' uses hard_model",
              c.models == [g.MODEL_OPUS],
              f"models={c.models}")

        # 8. Gemini reply 'ESCALATE' (he literally asked) -> work lane
        g.local_llm = fake = FakeLocal(up=True, reply="ESCALATE")
        c = MockClient()
        app = make_app(c)
        app.work_model = "fable 5"
        await app._talk("please just handle that for me")
        check("Gemini ESCALATE hands the turn to the work lane",
              c.queries == ["please just handle that for me"]
              and c.models == [g.MODEL_FULL] and app.busy,
              f"queries={c.queries} models={c.models}")

        # 9. spoken 'stop' brakes a running work turn
        c = MockClient()
        app = make_app(c)
        app.busy = True
        interrupted = []

        async def fake_interrupt():
            interrupted.append(True)
        app._safe_interrupt = fake_interrupt
        await app._talk("stop")
        check("spoken stop brakes the work turn",
              interrupted and app.tts.spoken == ["Stopped."]
              and not app.busy and c.queries == []
              and has(app, "work_fail"),
              f"interrupted={interrupted} spoken={app.tts.spoken} busy={app.busy}")

        # 10. a second work order while busy folds into the running turn
        c = MockClient()
        app = make_app(c)
        app.busy = True
        app.last_user_text = "original"
        await app._work("also add tests")
        check("second work order folds into the running turn",
              c.queries == ["also add tests"]
              and app.last_user_text == "original\nalso add tests"
              and has(app, "work_add"),
              f"queries={c.queries} last={app.last_user_text}")

        # ---- graceful Claude-out (rule 4) ----
        # 11. dispatching work while Claude is spent -> no query to Claude;
        #     Gemini covers in the middle (offline mode, own tools live) and
        #     work_fail notes it on the left. The app never feels dead.
        g.local_llm = fake = FakeLocal(
            up=True, reply="Coding waits for Claude; here is what I can do.")
        c = MockClient()
        app = make_app(c)
        app.claude_out = True
        app.claude_reset = "14:30"
        await app._work("build something")
        check("work while Claude out -> work_fail + gemini offline cover",
              c.queries == [] and not app.busy and has(app, "work_fail")
              and fake.offline == [True]
              and "build something" in fake.chats[0],
              f"queries={c.queries} busy={app.busy} offline={fake.offline}")

        # 12. quota exhausted mid-work -> claude_out set, work_fail+work_done,
        #     no crash (talk lane keeps working elsewhere)
        g.local_llm = fake = FakeLocal(up=True, reply="x")
        quota = result_msg(is_error=True, result="usage limit reached|1893456000")
        c = MockClient(script=[quota])
        app = make_app(c)
        app.busy = True
        app.last_user_text = "big work"
        await app._consume()
        import re as _re
        check("quota exhausted mid-work -> claude_out + work_fail",
              app.claude_out is True and has(app, "work_fail")
              and has(app, "work_done")
              and bool(_re.fullmatch(r"\d\d:\d\d", app.claude_reset)),
              f"claude_out={app.claude_out} reset={app.claude_reset}")

        # 12b. the REAL CLI wording ("You've hit your session limit · resets
        #      2:30am (Asia/Tbilisi)") -> caught, reset parsed to 02:30, and
        #      the raw text NEVER reaches any UI event (his order 2026-07-17)
        g.local_llm = fake = FakeLocal(up=True, reply="x")
        real = result_msg(
            is_error=True,
            result="You've hit your session limit · resets 2:30am "
                   "(Asia/Tbilisi)")
        c = MockClient(script=[real])
        app = make_app(c)
        app.busy = True
        app.last_user_text = "big work"
        await app._consume()
        leak = any("hit your session" in str(e).lower()
                   or "asia/tbilisi" in str(e).lower() for e in app.events)
        check("real limit wording -> caught, reset 02:30, raw never shown",
              app.claude_out is True and app.claude_reset == "02:30"
              and not leak and has(app, "work_fail") and has(app, "limit"),
              f"out={app.claude_out} reset={app.claude_reset} leak={leak}")

        # 12c. pm reset wording parses to the 24h clock
        c = MockClient(script=[result_msg(
            is_error=True,
            result="You've hit your session limit · resets 6:05pm "
                   "(Asia/Tbilisi)")])
        app = make_app(c)
        app.busy = True
        await app._consume()
        check("pm reset wording -> 18:05",
              app.claude_reset == "18:05", f"reset={app.claude_reset}")

        # 12d. talk brain set to a Claude voice while Claude is out ->
        #      Gemini covers the talk turn; no Claude client is touched
        g.local_llm = fake = FakeLocal(up=True, reply="Covering for Claude.")
        c = MockClient()
        app = make_app(c)
        app.talk_brain = "sonnet 5"
        app.claude_out = True
        await app._talk("how are you")
        check("claude-out talk on a claude voice -> gemini covers",
              fake.chats == ["how are you"] and app.talk_client is None
              and c.queries == [],
              f"chats={fake.chats} talk_client={app.talk_client}")

        # 12e. Gemini replies ESCALATE while Claude is out -> friendly line
        #      spoken inline (no deadlock on the talk lock), no Claude query
        g.local_llm = fake = FakeLocal(up=True, reply="ESCALATE")
        c = MockClient()
        app = make_app(c)
        app.claude_out = True
        app.claude_reset = "02:30"
        await app._talk("please escalate that")
        check("ESCALATE while out -> friendly line, no deadlock, no query",
              c.queries == [] and not app.busy and has(app, "work_fail")
              and any("rate-limited" in s for s in app.tts.spoken),
              f"queries={c.queries} spoken={app.tts.spoken}")

        # 13. work success -> work_done, exchange logged, reply cleared, and
        #     quota flag cleared if it had been set
        g.local_llm = fake = FakeLocal(up=True, reply="x")
        c = MockClient(script=[result_msg(ctx=5000)])
        app = make_app(c)
        app.busy = True
        app.claude_out = True  # was out; a landing turn clears it
        app.last_user_text = "task"
        app._reply_acc = "did the thing "
        await app._consume()
        check("work success -> work_done, logged, quota flag cleared",
              has(app, "work_done") and app._reply_acc == ""
              and app._last_ctx == 5000 and app.claude_out is False
              and fake.noted and fake.noted[0][0] == "task",
              f"reply_acc={app._reply_acc!r} ctx={app._last_ctx} out={app.claude_out}")

        # 14. work error with nothing produced -> work_fail, no crash
        c = MockClient(script=[result_msg(is_error=True, result="boom")])
        app = make_app(c)
        app.busy = True
        app.last_user_text = "task"
        await app._consume()
        check("work error -> work_fail on the left, no crash",
              has(app, "work_fail") and has(app, "work_done") and not app.busy,
              f"events={[e[0] for e in app.events]}")

        # ---- context economy (unchanged machinery) ----
        # 15. /compact fires past ROTATE_CTX and success keeps the session
        big = result_msg(ctx=g.ROTATE_CTX + 5000)
        small_after = result_msg(ctx=100)
        c = MockClient(script=[big, small_after], ctx_after=9000)
        app = make_app(c)
        app.busy = True
        app.last_user_text = "big work"
        wants_fresh = await app._consume()
        check("compact fires past 60k and session survives",
              "/compact" in c.queries and wants_fresh is None
              and app._last_ctx == 9000 and not app._compacting,
              f"queries={c.queries} wants_fresh={wants_fresh} ctx={app._last_ctx}")

        # 16. compact that doesn't shrink -> hard rotation with handoff
        tmp2 = tempfile.NamedTemporaryFile(delete=False)
        tmp2.write(b"sid")
        tmp2.close()
        old_session = g.SESSION_FILE
        g.SESSION_FILE = tmp2.name
        try:
            big = result_msg(ctx=g.ROTATE_CTX + 5000)
            still_big = result_msg(ctx=g.ROTATE_CTX + 5000)
            c = MockClient(script=[big, still_big], ctx_after=g.ROTATE_CTX + 4000)
            app = make_app(c)
            app.busy = True
            app.last_user_text = "big work"
            wants_fresh = await app._consume()
            check("failed compact falls back to rotation + handoff",
                  wants_fresh is True and app._rotate_only
                  and app._pending_handoff.startswith("[context-handoff]")
                  and not os.path.exists(tmp2.name),
                  f"wants_fresh={wants_fresh} rotate={app._rotate_only}")
        finally:
            g.SESSION_FILE = old_session
            if os.path.exists(tmp2.name):
                os.unlink(tmp2.name)

        # 17. small session never compacts or rotates
        c = MockClient(script=[result_msg(ctx=5000)])
        app = make_app(c)
        app.busy = True
        app.last_user_text = "hello"
        wants_fresh = await app._consume()
        check("small session untouched",
              wants_fresh is None and "/compact" not in c.queries
              and app._last_ctx == 5000,
              f"queries={c.queries}")

        # 18. work bridges recent talk-lane chat as context
        g.local_llm = fake = FakeLocal(up=True, reply="Sounds fun.")
        c = MockClient()
        app = make_app(c)
        app.work_model = "fable 5"
        await app._talk("thinking about a beach day")
        await app._work("build the beach-day planner")
        check("work turn bridges unseen talk-lane chat",
              c.queries and c.queries[0].startswith("[chat since your last turn")
              and c.queries[0].endswith("build the beach-day planner")
              and app._local_unseen == [],
              f"queries={c.queries}")
    finally:
        g.TRANSCRIPT_FILE = old_tr
        os.unlink(tmp.name)

    # ---- pure helpers / regexes (no app state) ----
    # 19. tool-step describer
    class _Blk:
        name = "Edit"
        input = {"file_path": "C:/Users/user/goat-standalone/python/ui_qt.py"}
    check("_describe_tool renders 'Edit — ui_qt.py'",
          g._describe_tool(_Blk()) == "Edit — ui_qt.py",
          g._describe_tool(_Blk()))

    class _Blk2:
        name = "Bash"
        input = {"command": "git status"}
    check("_describe_tool renders a command step",
          g._describe_tool(_Blk2()) == "Bash — git status")

    # 20. work-dispatch address regex: hits addresses, spares questions
    check("WORK_DISPATCH_RE hits addresses",
          all(g.WORK_DISPATCH_RE.match(t) for t in
              ("fable, build it", "working brain: do y", "hard brain refactor",
               "opus run the migration", "full model take this")))
    check("WORK_DISPATCH_RE spares ordinary talk",
          not any(g.WORK_DISPATCH_RE.match(t) for t in
                  ("how does the working brain work?", "tell me about opus",
                   "what's the hard part here", "good morning")))
    check("WORK_HARD_RE distinguishes hard/opus",
          g.WORK_HARD_RE.search("hard brain go") and g.WORK_HARD_RE.search("opus now")
          and not g.WORK_HARD_RE.search("working brain go"))

    # 21. power watcher verdicts (pure function)
    pv = g.power_verdict
    check("power: AC drop -> jack warning",
          pv((80, True), (80, False)) is not None
          and "jack" in pv((80, True), (80, False)))
    check("power: steady AC -> quiet", pv((80, True), (81, True)) is None)
    check("power: low battery discharging -> warn",
          pv((25, False), (18, False)) is not None)
    check("power: back on AC -> quiet", pv((50, False), (50, True)) is None)
    check("power: unreadable battery -> quiet",
          pv(None, None) is None and pv((50, True), None) is None)

    # 22. regex sanity: stop orders and wake words
    check("STOP_RE hits stop orders",
          all(g.STOP_RE.search(t) for t in
              ("stop", "cancel that", "hold on", "never mind")))
    check("WAKE_RE hits name variants",
          all(g.WAKE_RE.search(t) for t in
              ("goat, you there", "hey goat", "okay goat run it")))

    # ---- local hands: whitelist safety (unchanged) ----
    import local_hands as lh
    check("hands: bare domain upgraded to https",
          lh.resolve_url("youtube.com") == "https://youtube.com")
    check("hands: https passes through",
          lh.resolve_url("https://fasmetri.ge") == "https://fasmetri.ge")
    check("hands: file/javascript/shell schemes blocked",
          all(lh.resolve_url(u) is None for u in
              ("file:///c:/windows", "javascript:alert(1)",
               "shell:startup", "ftp://x.com")))
    check("hands: known app resolves, unknown app refused",
          lh.resolve_app("spotify") == "spotify:"
          and lh.resolve_app("calc") == "calc.exe"
          and lh.resolve_app("malware.exe") is None)
    check("hands: bad volume action -> ERROR, no keypress",
          lh.execute("volume", {"action": "sideways"}).startswith("ERROR"))
    check("hands: unknown tool -> ERROR",
          lh.execute("run_shell", {"cmd": "del /f"}).startswith("ERROR"))
    check("hands: run_command executes, never pre-refuses",
          "goat-ok" in lh.execute(
              "run_command", {"command": "Write-Output goat-ok"}))
    check("hands: write_file + read_file round-trip", _hands_roundtrip(lh))
    check("hands: delete_file deletes file+folder, honest ERROR when gone",
          _hands_delete(lh))
    check("hands: list_dir shows real file sizes", _hands_sizes(lh))
    check("hands: fetch_url rejects non-web schemes",
          lh.execute("fetch_url", {"url": "file:///c:/x"}).startswith("ERROR"))

    # ---- resize_interface + set_ui_color route to UI callbacks ----
    got = []
    lh.set_ui_scale_callback(lambda spec: got.append(spec))
    r1 = lh.execute("resize_interface", {"bigger": 1.5})
    r2 = lh.execute("resize_interface", {"percent": 150})
    lh.set_ui_scale_callback(None)
    check("resize_interface: relative + absolute reach the UI callback",
          got == ["*1.5", "1.5"] and "1.5x" in r1 and "150%" in r2, f"got={got}")

    colhits = []
    lh.set_ui_color_callback(lambda p, c: (colhits.append((p, c)), True)[1])
    rc = lh.execute("set_ui_color", {"part": "text", "color": "blue"})
    rbad = lh.execute("set_ui_color", {"part": "sideways", "color": "blue"})
    lh.set_ui_color_callback(None)
    check("set_ui_color: valid part reaches callback",
          colhits == [("text", "blue")] and "blue" in rc, f"hits={colhits}")
    check("set_ui_color: bad part -> ERROR, no callback", rbad.startswith("ERROR"))

    # ---- refusal net regex (unchanged local_llm safety) ----
    import local_llm as ll
    check("refusal regex catches refusal openers",
          all(ll.REFUSAL_RE.match(s.lower()) for s in (
              "I can't open files here",
              "Sorry, I cannot do that",
              "I'm unable to access your files",
              "That requires tools I don't have")))
    check("refusal regex spares normal answers",
          not any(ll.REFUSAL_RE.match(s.lower()) for s in (
              "I can't wait to see it", "Sure — here's the plan",
              "The answer is 42")))

    # ---- gemini model fallback chain (2026-07-17): primary 429/503 ->
    #      same request on the stable fallback, then sticky for a while ----
    import io as _io
    import urllib.error as _ue

    def _fallback_probe(fail_code):
        seen = []

        def fake_do(payload, key, on_delta):
            seen.append(payload["model"])
            if payload["model"] == ll.GEMINI_MODEL:
                raise _ue.HTTPError("u", fail_code, "x", None,
                                    _io.BytesIO(b""))
            return "ok", []
        old_do, old_key = ll._do_stream, ll._api_key
        ll._do_stream, ll._api_key = fake_do, lambda: "k"
        try:
            reply, _ = ll._post_stream(
                [{"role": "user", "content": "hi"}], None, None)
            reply2, _ = ll._post_stream(
                [{"role": "user", "content": "hi again"}], None, None)
        finally:
            ll._do_stream, ll._api_key = old_do, old_key
            ll._primary_down[0] = 0.0
        return reply, reply2, seen

    r1, r2, seen = _fallback_probe(429)
    check("primary 429 -> same request lands on the fallback model",
          r1 == "ok" and seen[:2] == [ll.GEMINI_MODEL, ll.GEMINI_FALLBACK],
          f"seen={seen}")
    check("outage is sticky -> next turn skips the doomed primary",
          r2 == "ok" and seen[2:] == [ll.GEMINI_FALLBACK], f"seen={seen}")
    r1, _, seen = _fallback_probe(503)
    check("primary 503 (preview jammed) -> fallback too",
          r1 == "ok" and seen[:2] == [ll.GEMINI_MODEL, ll.GEMINI_FALLBACK],
          f"seen={seen}")

    # ---- UI config: three brain roles + scale clamp ----
    import ui_qt
    check("UI default roles present",
          ui_qt.DEFAULT_CFG["talk_brain"] == "gemini flash"
          and ui_qt.DEFAULT_CFG["work_model"] == "sonnet 5"
          and ui_qt.DEFAULT_CFG["hard_model"] == "fable 5")
    check("UI brain option lists",
          ui_qt.TALK_OPTS == ["gemini flash", "sonnet 5"]
          and ui_qt.WORK_OPTS == ["sonnet 5", "fable 5", "opus 4.8"])

    def _clamped(v):
        return min(ui_qt.UI_SCALE_MAX, max(ui_qt.UI_SCALE_MIN, float(v)))
    check("ui scale clamps out-of-range requests",
          _clamped(9.0) == ui_qt.UI_SCALE_MAX
          and _clamped(0.1) == ui_qt.UI_SCALE_MIN and _clamped(1.5) == 1.5)

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
