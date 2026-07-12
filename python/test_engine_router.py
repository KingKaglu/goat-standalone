"""Engine logic tests — router, sticky-full, handoff, /compact flow.

No audio, no SDK subprocess, no API cost: the client and TTS are mocks.
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


def make_app(client):
    app = g.GoatApp.__new__(g.GoatApp)
    app.emit = lambda *a: app.events.append(a)
    app.events = []
    app.client = client
    app.tts = MockTTS()
    app.busy = False
    app.model = g.MODEL_FAST
    app.last_user_text = None
    app.escalate_pending = False
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
    app.recep = None
    app._recep_busy = False
    app._last_ctx = 0
    app._exchanges = deque(maxlen=g.HANDOFF_KEEP)
    app._reply_acc = ""
    app._rotate_only = False
    app._pending_handoff = ""
    app._compacting = False
    app.language = "en"
    app._local_unseen = []
    app.brain_mode = "auto"
    app._pending_escalation = ""
    return app


class FakeLocal:
    """Stands in for local_llm — no Ollama, no GPU, deterministic."""
    def __init__(self, up=False, reply=None):
        self.up = up
        self.reply = reply
        self.chats = []
        self.noted = []
        self.offline_calls = []
        self.LOCAL_KA = False
        self.LOCAL_NAME = "local brain"

    def available(self):
        return self.up

    def chat(self, text, on_delta=None, lang="en", offline=False):
        self.chats.append(text)
        if offline:
            self.offline_calls.append(text)
        if self.reply and self.reply != "ESCALATE" and on_delta:
            on_delta(self.reply)
        return self.reply

    def recep_answer(self, status, text):
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
    # Local brain off for the legacy tests — they verify the cloud router.
    g.local_llm = FakeLocal(up=False)

    # 1. chat stays on the fast model, tagged
    c = MockClient()
    app = make_app(c)
    await app._send_user("how are you doing")
    check("chat -> fast + [fast-turn] tag",
          c.queries == ["[fast-turn] how are you doing"] and c.models == [],
          f"queries={c.queries} models={c.models}")

    # 2. work verb goes straight to the full model, untagged
    c = MockClient()
    app = make_app(c)
    await app._send_user("fix the scroll bug in the app")
    check("work verb -> full model direct",
          c.queries == ["fix the scroll bug in the app"]
          and c.models == [g.MODEL_FULL],
          f"queries={c.queries} models={c.models}")

    # 3. heavy session: chat sticks to the full model (cache thrash guard)
    c = MockClient()
    app = make_app(c)
    app.model = g.MODEL_FULL
    app._last_ctx = g.STICKY_FULL_CTX + 1
    await app._send_user("how are you doing")
    check("sticky-full past 25k: chat stays on full, no switch",
          c.queries == ["how are you doing"] and c.models == [],
          f"queries={c.queries} models={c.models}")

    # 4. pending handoff rides the next message and forces full
    c = MockClient()
    app = make_app(c)
    app._pending_handoff = "[context-handoff] previous stuff"
    await app._send_user("hello again")
    check("handoff prefixes next message + full model",
          c.queries and c.queries[0].startswith("[context-handoff]")
          and c.queries[0].endswith("hello again")
          and app._pending_handoff == "",
          f"queries={c.queries}")

    # 5. /compact triggers past ROTATE_CTX and success keeps the session
    big = result_msg(ctx=g.ROTATE_CTX + 5000)
    small_after = result_msg(ctx=100)  # the compact turn's own result
    c = MockClient(script=[big, small_after], ctx_after=9000)
    app = make_app(c)
    app.last_user_text = "big work"
    wants_fresh = await app._consume()
    check("compact fires past 60k and session survives",
          "/compact" in c.queries and wants_fresh is None
          and app._last_ctx == 9000 and not app._compacting,
          f"queries={c.queries} wants_fresh={wants_fresh} ctx={app._last_ctx}")

    # 6. compact that doesn't shrink -> hard rotation with handoff
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(b"sid")
    tmp.close()
    old_session = g.SESSION_FILE
    g.SESSION_FILE = tmp.name
    try:
        big = result_msg(ctx=g.ROTATE_CTX + 5000)
        still_big = result_msg(ctx=g.ROTATE_CTX + 5000)
        c = MockClient(script=[big, still_big], ctx_after=g.ROTATE_CTX + 4000)
        app = make_app(c)
        app.last_user_text = "big work"
        wants_fresh = await app._consume()
        check("failed compact falls back to rotation + handoff",
              wants_fresh is True and app._rotate_only
              and app._pending_handoff.startswith("[context-handoff]")
              and not os.path.exists(tmp.name),
              f"wants_fresh={wants_fresh} rotate={app._rotate_only}")
    finally:
        g.SESSION_FILE = old_session
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)

    # 7. small turns never compact or rotate
    c = MockClient(script=[result_msg(ctx=5000)])
    app = make_app(c)
    app.last_user_text = "hello"
    wants_fresh = await app._consume()
    check("small session untouched",
          wants_fresh is None and "/compact" not in c.queries
          and app._last_ctx == 5000,
          f"queries={c.queries}")

    # 8. busy + stop order -> interrupt, "Stopped.", no query
    c = MockClient()
    app = make_app(c)
    app.busy = True
    app._turn_has_tools = True
    interrupted = []

    async def fake_interrupt():
        interrupted.append(True)
    app._safe_interrupt = fake_interrupt
    await app._send_user("stop")
    check("stop order brakes work turn",
          interrupted and app.tts.spoken == ["Stopped."] and c.queries == [],
          f"interrupted={interrupted} spoken={app.tts.spoken}")

    # 9. busy work turn + question -> front desk answers, main untouched
    c = MockClient()
    app = make_app(c)
    app.busy = True
    app._turn_has_tools = True
    asked = []

    async def fake_recep(text):
        asked.append(text)
        return True
    app._receptionist_answer = fake_recep
    await app._send_user("how long will this take?")
    check("front desk fields questions mid-work",
          asked == ["how long will this take?"] and c.queries == [],
          f"asked={asked} queries={c.queries}")

    # 10. busy talk turn (no tools) -> steer into the in-flight turn
    c = MockClient()
    app = make_app(c)
    app.busy = True
    app._turn_has_tools = False
    app.last_user_text = "original"
    await app._send_user("also this")
    check("talk-turn steering appends to in-flight turn",
          c.queries == ["also this"] and app.last_user_text == "original\nalso this",
          f"queries={c.queries} last={app.last_user_text}")

    # 11. power watcher verdicts (pure function)
    pv = g.power_verdict
    check("power: AC drop -> jack warning",
          pv((80, True), (80, False)) is not None
          and "jack" in pv((80, True), (80, False)))
    check("power: steady AC -> quiet",
          pv((80, True), (81, True)) is None)
    check("power: low battery discharging -> warn",
          pv((25, False), (18, False)) is not None)
    check("power: back on AC -> quiet",
          pv((50, False), (50, True)) is None)
    check("power: unreadable battery -> quiet",
          pv(None, None) is None and pv((50, True), None) is None)

    # 12. transcript log append + trim safety
    import json as _json
    import tempfile as _tf
    tmp2 = _tf.NamedTemporaryFile(delete=False, suffix=".jsonl")
    tmp2.close()
    old_tr = g.TRANSCRIPT_FILE
    g.TRANSCRIPT_FILE = tmp2.name
    try:
        app = make_app(MockClient())
        app._log_exchange("hello there", "General reply.")
        app._log_exchange("[boot-briefing] ignored?", "no")  # logged (filter is at call site)
        rows = [_json.loads(x) for x in open(tmp2.name, encoding="utf-8")]
        check("transcript rows written",
              rows[0]["user"] == "hello there" and rows[0]["reply"] == "General reply.",
              f"rows={rows}")
    finally:
        g.TRANSCRIPT_FILE = old_tr
        os.unlink(tmp2.name)

    # 13. regex sanity: stop orders and wake words
    check("STOP_RE hits stop orders",
          all(g.STOP_RE.search(t) for t in
              ("stop", "cancel that", "hold on", "never mind")))
    check("WAKE_RE hits name variants",
          all(g.WAKE_RE.search(t) for t in
              ("goat, you there", "hey goat", "okay goat run it")))
    check("WORK_RE ignores chat",
          not any(g.WORK_RE.search(t) for t in
                  ("good morning", "what do you think", "thanks a lot")))

    # ---- third brain: local model routing (2026-07-11) ----
    import tempfile as _tf2
    tmp3 = _tf2.NamedTemporaryFile(delete=False, suffix=".jsonl")
    tmp3.close()
    old_tr2 = g.TRANSCRIPT_FILE
    g.TRANSCRIPT_FILE = tmp3.name
    try:
        # 14. local up + casual -> answered locally, Claude untouched
        g.local_llm = fake = FakeLocal(up=True, reply="All quiet here.")
        c = MockClient()
        app = make_app(c)
        await app._send_user("how are you doing")
        check("local answers casual, Claude untouched",
              fake.chats == ["how are you doing"] and c.queries == []
              and not app.busy and app._local_unseen
              and ("turn_done", "") in app.events,
              f"chats={fake.chats} queries={c.queries} busy={app.busy}")

        # 15. local says ESCALATE -> straight to Fable, untagged
        g.local_llm = fake = FakeLocal(up=True, reply="ESCALATE")
        c = MockClient()
        app = make_app(c)
        await app._send_user("please handle that thing we discussed")
        check("local ESCALATE -> full model direct",
              c.models == [g.MODEL_FULL]
              and c.queries == ["please handle that thing we discussed"],
              f"models={c.models} queries={c.queries}")

        # 16. local dies mid-turn -> old Sonnet fast path
        g.local_llm = fake = FakeLocal(up=True, reply=None)
        c = MockClient()
        app = make_app(c)
        await app._send_user("how are you doing")
        check("local failure falls back to Sonnet fast-turn",
              c.queries == ["[fast-turn] how are you doing"] and c.models == [],
              f"queries={c.queries} models={c.models}")

        # 17. bridge: work turn carries unseen local chat as context
        g.local_llm = fake = FakeLocal(up=True, reply="Sounds fun.")
        c = MockClient()
        app = make_app(c)
        await app._send_user("thinking about a beach day")
        await app._send_user("fix the scroll bug in the app")
        check("work turn bridges unseen local chat",
              c.queries and c.queries[0].startswith("[chat since your last turn")
              and c.queries[0].endswith("fix the scroll bug in the app")
              and app._local_unseen == [],
              f"queries={c.queries}")

        # 18. front desk stays on Sonnet — the local brain must NOT be
        # consulted mid-work (it invents answers under pressure, measured
        # 2026-07-11). The Sonnet client spawn is faked.
        g.local_llm = fake = FakeLocal(up=True, reply="should not be used")
        c = MockClient()
        app = make_app(c)
        app.busy = True
        app._turn_has_tools = True
        app._work_started = time.monotonic()

        class FakeRecep:
            def __init__(self):
                self.queries = []

            async def query(self, text, **kw):
                self.queries.append(text)

            async def receive_response(self):
                return
                yield

        recep = FakeRecep()

        async def fake_ensure():
            app.recep = recep
        app._ensure_recep = fake_ensure
        await app._receptionist_answer("how long left?")
        check("front desk skips local brain, queries Sonnet recep",
              fake.chats == [] and len(recep.queries) == 1,
              f"chats={fake.chats} recep_queries={recep.queries}")

        # 19b. typed Georgian in English mode skips the local brain (the 4B
        # garbles Georgian) and rides the Sonnet fast path instead
        g.local_llm = fake_ka = FakeLocal(up=True, reply="should not be used")
        c_ka = MockClient()
        app_ka = make_app(c_ka)
        await app_ka._send_user("გამარჯობა, როგორ ხარ?")
        check("typed Georgian skips local brain",
              fake_ka.chats == []
              and c_ka.queries == ["[fast-turn] გამარჯობა, როგორ ხარ?"],
              f"chats={fake_ka.chats} queries={c_ka.queries}")

        # 20. Claude quota gone -> local brain answers (reverse fallback)
        g.local_llm = fake = FakeLocal(up=True, reply="Still here. We chat free now.")
        quota = result_msg(is_error=True, result="usage limit reached|1893456000")
        c = MockClient(script=[quota])
        app = make_app(c)
        app.last_user_text = "so what now?"
        await app._consume()
        check("quota exhausted -> local brain takes over",
              fake.offline_calls == ["so what now?"]
              and ("turn_done", "") in app.events,
              f"offline={fake.offline_calls}")

        # 21. Claude API error with empty reply -> local brain answers
        g.local_llm = fake = FakeLocal(up=True, reply="Cloud is down, I'm local.")
        apierr = result_msg(is_error=True, result="API connection error")
        c = MockClient(script=[apierr])
        app = make_app(c)
        app.last_user_text = "you there?"
        await app._consume()
        check("api error -> local brain takes over",
              fake.offline_calls == ["you there?"]
              and ("turn_done", "") in app.events,
              f"offline={fake.offline_calls}")

        # 22. api error AND local dead -> spoken apology, no crash
        g.local_llm = fake = FakeLocal(up=False)
        c = MockClient(script=[result_msg(is_error=True, result="boom")])
        app = make_app(c)
        app.last_user_text = "you there?"
        await app._consume()
        check("api error + local down -> spoken apology",
              any("local brain is down" in s for s in app.tts.spoken),
              f"spoken={app.tts.spoken}")

        # 19. work verbs never consult the local brain at all
        g.local_llm = fake = FakeLocal(up=True, reply="should not be used")
        c = MockClient()
        app = make_app(c)
        await app._send_user("fix the scroll bug in the app")
        check("work verb skips local brain",
              fake.chats == [] and c.models == [g.MODEL_FULL],
              f"chats={fake.chats} models={c.models}")
    finally:
        g.TRANSCRIPT_FILE = old_tr2
        os.unlink(tmp3.name)

        # 23. brain pinned to fable: even chat goes to the full model
        g.local_llm = fake = FakeLocal(up=True, reply="should not be used")
        c = MockClient()
        app = make_app(c)
        app.brain_mode = "fable"
        await app._send_user("how are you doing")
        check("pin fable: chat -> full model, local skipped",
              fake.chats == [] and c.models == [g.MODEL_FULL]
              and c.queries == ["how are you doing"],
              f"chats={fake.chats} models={c.models}")

        # 24. brain pinned to sonnet: local skipped, untagged turn on the
        # fast model (pinned = stays put, no ESCALATE machinery)
        g.local_llm = fake = FakeLocal(up=True, reply="should not be used")
        c = MockClient()
        app = make_app(c)
        app.brain_mode = "sonnet"
        await app._send_user("how are you doing")
        check("pin sonnet: local skipped, untagged fast model",
              fake.chats == [] and c.queries == ["how are you doing"]
              and c.models == [],
              f"chats={fake.chats} queries={c.queries}")

        # 25. brain pinned to local: work verb runs LOCAL (full tools). An
        # ESCALATE now fires ONLY when he literally asked for the full model
        # (the local brain no longer self-punts), so it goes STRAIGHT to Fable
        # — no "say the word" approval prompt (2026-07-12: "do not escalate
        # until i say so").
        g.local_llm = fake = FakeLocal(up=True, reply="ESCALATE")
        c = MockClient()
        app = make_app(c)
        app.brain_mode = "local"
        await app._send_user("fix the scroll bug in the app")
        check("pin local: explicit ESCALATE -> straight to Fable, no ask",
              fake.chats == ["fix the scroll bug in the app"]
              and c.models == [g.MODEL_FULL]
              and c.queries == ["fix the scroll bug in the app"]
              and app._pending_escalation == "",
              f"chats={fake.chats} models={c.models} pend={app._pending_escalation!r}")

        # 26. pinned-local approval: a short "yes" escalates the stored task
        c = MockClient()
        app = make_app(c)
        app.brain_mode = "local"
        app._pending_escalation = "fix the scroll bug in the app"
        await app._send_user("yes do it")
        check("pin local approval -> escalates stored task to Fable",
              c.models == [g.MODEL_FULL]
              and c.queries == ["fix the scroll bug in the app"]
              and app._pending_escalation == "",
              f"models={c.models} queries={c.queries}")

        # 27. pinned-local, NOT approval: pending cleared, message handled
        g.local_llm = fake = FakeLocal(up=True, reply="Sure, here's a thought.")
        c = MockClient()
        app = make_app(c)
        app.brain_mode = "local"
        app._pending_escalation = "fix the scroll bug in the app"
        await app._send_user("actually tell me a joke instead")
        check("pin local non-approval drops pending, answers locally",
              app._pending_escalation == ""
              and fake.chats == ["actually tell me a joke instead"]
              and c.models == [],
              f"pend={app._pending_escalation!r} chats={fake.chats}")

        # 28. pinned sonnet: work verb stays on Sonnet, untagged, no Fable
        g.local_llm = fake = FakeLocal(up=True, reply="nope")
        c = MockClient()
        app = make_app(c)
        app.brain_mode = "sonnet"
        await app._send_user("fix the scroll bug in the app")
        check("pin sonnet: work stays on fast model, untagged, no escalate",
              fake.chats == [] and c.models == []
              and c.queries == ["fix the scroll bug in the app"],
              f"chats={fake.chats} models={c.models} queries={c.queries}")

    # ---- refusal net: "I can't" openers are caught (now -> forced local
    # retry, not escalation; regex still must match openers and spare answers)
    import local_llm as ll
    check("refusal regex catches refusal openers",
          all(ll.REFUSAL_RE.match(s.lower()) for s in (
              "I can't open files here",
              "Sorry, I cannot do that",
              "I'm unable to access your files",
              "I am not able to run commands",
              "Unfortunately, I don't have access to your screen",
              "That requires tools I don't have",
              "I won't be able to do that here")))
    check("refusal regex spares normal answers",
          not any(ll.REFUSAL_RE.match(s.lower()) for s in (
              "I can't wait to see it",
              "Sure — here's the plan",
              "It's 4 a.m., go to sleep",
              "YouTube is open",
              "The answer is 42")))

    # ---- local hands: whitelist safety (pure resolvers, no side effects) ----
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
    check("hands: unknown app in execute -> ERROR mentions working brain",
          "working brain" in lh.execute("open_app", {"app": "photoshop"}))

    # ---- hands v3: no command wall (2026-07-12: "no guardlines") ----
    check("hands: destructive wall removed (no DESTRUCTIVE_RE)",
          not hasattr(lh, "DESTRUCTIVE_RE"))
    check("hands: run_command executes, never pre-refuses",
          "goat-ok" in lh.execute(
              "run_command", {"command": "Write-Output goat-ok"}))
    check("hands: write_file + read_file round-trip",
          _hands_roundtrip(lh))
    check("hands: delete_file deletes file+folder, honest ERROR when gone",
          _hands_delete(lh))
    check("hands: list_dir shows real file sizes",
          _hands_sizes(lh))
    check("hands: fetch_url rejects non-web schemes",
          lh.execute("fetch_url", {"url": "file:///c:/x"}).startswith("ERROR"))

    # ---- resize_interface tool routes to the UI callback ----
    got = []
    lh.set_ui_scale_callback(lambda spec: got.append(spec))
    r1 = lh.execute("resize_interface", {"bigger": 1.5})
    r2 = lh.execute("resize_interface", {"percent": 150})
    lh.set_ui_scale_callback(None)
    check("resize_interface: relative + absolute reach the UI callback",
          got == ["*1.5", "1.5"] and "1.5x" in r1 and "150%" in r2,
          f"got={got}")
    check("resize_interface: no UI bound -> ERROR",
          lh.execute("resize_interface", {"percent": 150}).startswith("ERROR"))

    # ---- set_ui_color routes to the UI callback ----
    colhits = []
    lh.set_ui_color_callback(lambda p, c: (colhits.append((p, c)), True)[1])
    rc = lh.execute("set_ui_color", {"part": "text", "color": "blue"})
    rbad = lh.execute("set_ui_color", {"part": "sideways", "color": "blue"})
    lh.set_ui_color_callback(None)
    check("set_ui_color: valid part reaches callback",
          colhits == [("text", "blue")] and "blue" in rc,
          f"hits={colhits} rc={rc}")
    check("set_ui_color: bad part -> ERROR, no callback",
          rbad.startswith("ERROR"))
    check("set_ui_color: no UI bound -> ERROR",
          lh.execute("set_ui_color",
                     {"part": "text", "color": "blue"}).startswith("ERROR"))

    # ---- ui scale clamps to sane bounds ----
    import ui_qt
    def _clamped(v):
        c = dict(ui_qt.DEFAULT_CFG); c["scale"] = v
        # replicate load_ui_config's clamp
        return min(ui_qt.UI_SCALE_MAX, max(ui_qt.UI_SCALE_MIN, float(v)))
    check("ui scale clamps out-of-range requests",
          _clamped(9.0) == ui_qt.UI_SCALE_MAX
          and _clamped(0.1) == ui_qt.UI_SCALE_MIN
          and _clamped(1.5) == 1.5)

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
