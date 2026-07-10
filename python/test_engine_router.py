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
    return app


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

    # 11. regex sanity: stop orders and wake words
    check("STOP_RE hits stop orders",
          all(g.STOP_RE.search(t) for t in
              ("stop", "cancel that", "hold on", "never mind")))
    check("WAKE_RE hits name variants",
          all(g.WAKE_RE.search(t) for t in
              ("goat, you there", "hey goat", "okay goat run it")))
    check("WORK_RE ignores chat",
          not any(g.WORK_RE.search(t) for t in
                  ("good morning", "what do you think", "thanks a lot")))

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
