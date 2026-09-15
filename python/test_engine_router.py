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

import numpy as np

# The reflex lane runs REAL actions (launches apps, moves the volume, opens
# folders). Several cases below feed it genuine phrases — "open chrome",
# "turn the volume up" — to prove they stay off the work lane, and those must
# not actually fire on his machine mid-test. Off by default here; the reflex
# routing case switches it on with a stubbed matcher whose actions only
# record. Must be set BEFORE goat_app imports reflex.
os.environ.setdefault("GOAT_REFLEX", "off")

import goat_app as g
import reflex as _reflex_mod  # the real Reflex class, kept reachable while
                              # g.reflex is swapped for a stub
from claude_agent_sdk import ResultMessage, StreamEvent


class MockTTS:
    def __init__(self):
        self.spoken = []
        # Mirrors the real pipeline's turn bookkeeping — the backchannel and
        # the latency ledger both read these.
        self.gen = 0
        self.enabled = True
        self._sounded = False

    def mark_reply(self):
        pass

    def new_turn(self):
        self._sounded = False

    def cancel(self):
        pass

    def say(self, text):
        self.spoken.append(text)

    def prewarm(self, lines):
        self.warmed = list(lines)


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
    app.work_model = g.DEFAULT_WORK
    app.hard_model = g.DEFAULT_WORK
    app.model = g.MODEL_FAST
    app.effort = g.DEFAULT_EFFORT
    app.loop = None
    app._work_options = None
    app._effort_dirty = False
    app._reopen_only = False
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
    app._work_done_at = 0.0
    app._work_failed = False
    app._last_work_summary = ""
    app._last_ctx = 0
    app._exchanges = deque(maxlen=g.HANDOFF_KEEP)
    app._reply_acc = ""
    app._rotate_only = False
    app._pending_handoff = ""
    app._compacting = False
    app.language = "en"
    app.turn_lang = "en"
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
        self.statuses = []
        self.LOCAL_KA = False
        self.LOCAL_NAME = "gemini flash"

    def available(self):
        return self.up

    def chat(self, text, on_delta=None, lang="en", offline=False, status=""):
        self.chats.append(text)
        self.offline.append(offline)
        self.statuses.append(status)
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
        app.work_model = "opus 5"
        await app._work("fix the scroll bug in the app")
        check("work -> working brain (work_model), Gemini skipped",
              fake.chats == [] and c.queries == ["fix the scroll bug in the app"]
              and c.models == [g.MODEL_FULL] and app.busy
              and has(app, "work_start"),
              f"chats={fake.chats} models={c.models} queries={c.queries}")

        # 4. hard dispatch -> hard_model, not work_model
        c = MockClient()
        app = make_app(c)
        app.work_model = "opus 5"
        app.hard_model = "fable 5.1"
        await app._work("do the heavy refactor", hard=True)
        check("hard work -> hard_model",
              c.models == [g.MODEL_FABLE]
              and c.queries == ["do the heavy refactor"],
              f"models={c.models} queries={c.queries}")

        # 5. ORDERS ARE OBEYED (2026-09-14 goal — supersedes the 2026-07-17
        #    "manual dispatch only" rule): a plain imperative goes STRAIGHT to
        #    the work lane, no name needed, and GOAT says so out loud at once.
        g.local_llm = fake = FakeLocal(up=True, reply="should not be used")
        c = MockClient()
        app = make_app(c)
        await app._talk("fix the scroll bug in the app")
        check("a plain order goes straight to the work lane",
              c.queries == ["fix the scroll bug in the app"]
              and fake.chats == [] and app.busy,
              f"chats={fake.chats} queries={c.queries}")
        check("an order is acknowledged out loud immediately",
              app.tts.spoken and app.tts.spoken[0] in g.ACK_ORDER["en"],
              f"said={app.tts.spoken}")

        # 5b. talking ABOUT work is still talk — a question, a statement, or
        #     an opinion must never be mistaken for an order.
        for line in ("how do I fix the scroll bug?", "i fixed the scroll bug",
                     "should i deploy this?", "that build check was useful"):
            g.local_llm = fake = FakeLocal(up=True, reply="talking.")
            c = MockClient()
            app = make_app(c)
            await app._talk(line)
            if c.queries:
                break
        check("questions and statements about work stay talk",
              not c.queries and fake.chats, f"queries={c.queries}")

        # 5c. quick actions AND trivial topics stay with the talking brain
        #     (it has hands and answers in ~1s; the work brain at max effort
        #     would spend twenty seconds telling him the time)
        for line in ("open chrome", "turn the volume up", "what time is it",
                     "check the time", "check the weather", "შეამოწმე ამინდი"):
            g.local_llm = fake = FakeLocal(up=True, reply="done.")
            c = MockClient()
            app = make_app(c)
            await app._talk(line)
            if c.queries:
                break
        check("quick actions stay on the fast lane",
              not c.queries, f"queries={c.queries}")

        # 5c-bis. REFLEX LANE (2026-09-15): a recognised device command must
        #     be executed directly and reach NEITHER brain. Before this lane,
        #     "open google" cost two Gemini round trips on a model measured
        #     that day at 8-19s to first token, and "open the file gg" fell
        #     through to the work lane, which hunted the icon with
        #     screenshots. The matcher is stubbed so the test never actually
        #     opens anything; what's under test is the ROUTING.
        class StubReflex:
            def __init__(self):
                self.ran = []
                self.asked = []

            def match(self, text, lang="en"):
                self.asked.append(text)
                if not text.lower().startswith("open "):
                    return None
                rx = _reflex_mod.Reflex(
                    "open_url", "example.com",
                    lambda t=text: self.ran.append(t) or "opened")
                return rx

        real_reflex = g.reflex
        try:
            g.reflex = stub = StubReflex()
            g.local_llm = fake = FakeLocal(up=True, reply="should not be used")
            c = MockClient()
            app = make_app(c)
            app._log_exchange = lambda *a: None   # keep his transcript clean
            await app._talk("open google")
            check("a reflex runs the action and skips both brains",
                  stub.ran == ["open google"] and not fake.chats
                  and not c.queries,
                  f"ran={stub.ran} chats={fake.chats} queries={c.queries}")
            check("a reflex speaks a cached acknowledgement immediately",
                  app.tts.spoken and app.tts.spoken[0] in g.ACK_REFLEX["en"],
                  f"said={app.tts.spoken}")
            check("a reflex is remembered, so 'did you open it?' has an answer",
                  fake.noted and fake.noted[0][0] == "open google",
                  f"noted={fake.noted}")

            # Anything the matcher declines must land on the brains unchanged.
            g.local_llm = fake = FakeLocal(up=True, reply="talking.")
            c = MockClient()
            app = make_app(c)
            await app._talk("what is the capital of Georgia")
            check("a non-reflex still reaches the talking brain",
                  fake.chats == ["what is the capital of Georgia"]
                  and not c.queries, f"chats={fake.chats}")

            # A reflex that FAILS must not claim success.
            g.reflex = stub = StubReflex()
            stub.match = lambda text, lang="en": (
                _reflex_mod.Reflex("open_path", "nope",
                                lambda: "ERROR: no such file")
                if text.startswith("open ") else None)
            g.local_llm = FakeLocal(up=True, reply="unused")
            app = make_app(MockClient())
            app._log_exchange = lambda *a: None
            await app._talk("open nothing")
            check("a failed reflex says so instead of saying done",
                  g.REFLEX_FAIL["en"] in app.tts.spoken,
                  f"said={app.tts.spoken}")

            # A spoken "stop" still brakes a work turn — the reflex check
            # must sit BEHIND the brake, never in front of it.
            g.reflex = StubReflex()
            g.local_llm = FakeLocal(up=True, reply="unused")
            app = make_app(MockClient())
            app.busy = True
            app.client = MockClient()
            await app._talk("stop")
            check("the stop brake still wins over the reflex lane",
                  not app.busy and "Stopped." in app.tts.spoken,
                  f"busy={app.busy} said={app.tts.spoken}")
        finally:
            g.reflex = real_reflex

        # 5d. Georgian imperatives dispatch exactly the same way
        g.local_llm = fake = FakeLocal(up=True, reply="should not be used")
        c = MockClient()
        app = make_app(c)
        app.turn_lang = "ka"
        await app._talk("გაასწორე ბილდი")
        check("georgian order dispatches and is acknowledged in georgian",
              c.queries and c.queries[0].endswith("გაასწორე ბილდი")
              and app.tts.spoken and app.tts.spoken[0] in g.ACK_ORDER["ka"],
              f"queries={c.queries} said={app.tts.spoken}")

        # 6. manual voice dispatch: addressing the working brain by name goes
        #    straight to the work lane, Gemini skipped
        g.local_llm = fake = FakeLocal(up=True, reply="should not be used")
        c = MockClient()
        app = make_app(c)
        app.work_model = "opus 5"
        await app._talk("working brain, build the parser")
        check("addressing 'working brain' dispatches to the work lane",
              fake.chats == [] and c.queries == ["working brain, build the parser"]
              and c.models == [g.MODEL_FULL],
              f"chats={fake.chats} queries={c.queries} models={c.models}")

        # 7. addressing the HARD brain by name uses hard_model
        c = MockClient()
        app = make_app(c)
        app.hard_model = "fable 5.1"
        await app._talk("hard brain, run the heavy migration")
        check("addressing 'hard brain' uses hard_model",
              c.models == [g.MODEL_FABLE],
              f"models={c.models}")

        # 7b. mid-sentence address dispatches too (2026-07-18: his real
        #     sentence got a stuck line instead of the work lane)
        g.local_llm = fake = FakeLocal(up=True, reply="should not answer")
        c = MockClient()
        app = make_app(c)
        app.hard_model = "fable 5.1"
        await app._talk("okay, okay, thank you, how are you? please, ask "
                        "the opus 4.8 to update goat's readme on github, "
                        "okay?")
        check("mid-sentence 'ask the opus' dispatches to the HARD brain",
              fake.chats == [] and len(c.queries) == 1
              and c.models == [g.MODEL_FABLE],
              f"chats={fake.chats} models={c.models}")

        # 7c. talking ABOUT the brains does not dispatch
        g.local_llm = fake = FakeLocal(up=True, reply="They split the work.")
        c = MockClient()
        app = make_app(c)
        await app._talk("tell me about the working brain and fable")
        check("talking about the brains stays plain talk",
              fake.chats and c.queries == [],
              f"chats={fake.chats} queries={c.queries}")

        # 7d. status QUESTION about the working brain stays TALK, and Gemini
        #     receives the live status (2026-07-18: "hey what is working
        #     brain doing" got ignored — no window into the work lane)
        g.local_llm = fake = FakeLocal(up=True, reply="It's on the readme now.")
        c = MockClient()
        app = make_app(c)
        app.busy = True
        app._current_task = "update goat's readme on github"
        app._work_started = time.monotonic() - 130
        app._last_tool = "Edit"
        await app._talk("hey what is working brain doing")
        check("status question stays talk; live status reaches Gemini",
              c.queries == [] and len(fake.chats) == 1
              and "update goat's readme" in fake.statuses[0]
              and "busy" in fake.statuses[0]
              and "Edit" in fake.statuses[0],
              f"queries={c.queries} statuses={fake.statuses}")

        # 7e. Gemini punts ESCALATE on a status question -> deterministic
        #     spoken status, never dispatching the QUESTION as a job
        g.local_llm = fake = FakeLocal(up=True, reply="ESCALATE")
        c = MockClient()
        app = make_app(c)
        app.busy = True
        app._current_task = "refactor the parser"
        app._work_started = time.monotonic()
        await app._talk("what is the working brain doing right now?")
        check("ESCALATE on a status question -> spoken status, no dispatch",
              c.queries == []
              and any("refactor the parser" in s for s in app.tts.spoken),
              f"queries={c.queries} spoken={app.tts.spoken}")

        # 7f. after the work turn ends, the status note carries the outcome
        g.local_llm = fake = FakeLocal(up=True, reply="Yes — it wrapped up.")
        c = MockClient()
        app = make_app(c)
        app._current_task = "run the test suite"
        app._work_done_at = time.monotonic() - 30
        app._last_work_summary = "all 58 tests green"
        await app._talk("is fable done with the work?")
        check("finished-job outcome reaches the talking brain",
              c.queries == [] and "finished" in fake.statuses[0]
              and "all 58 tests green" in fake.statuses[0],
              f"queries={c.queries} statuses={fake.statuses}")

        # 7g. the status regex never eats a real dispatch that happens to
        #     contain an outcome verb ("…to finish the tests")
        g.local_llm = fake = FakeLocal(up=True, reply="should not answer")
        c = MockClient()
        app = make_app(c)
        app.hard_model = "fable 5.1"
        await app._talk("how are you? please ask the opus to finish the tests")
        check("dispatch containing an outcome verb still dispatches",
              fake.chats == [] and len(c.queries) == 1
              and c.models == [g.MODEL_FABLE],
              f"chats={fake.chats} queries={c.queries}")

        # 8. Gemini reply 'ESCALATE' (he literally asked) -> work lane
        g.local_llm = fake = FakeLocal(up=True, reply="ESCALATE")
        c = MockClient()
        app = make_app(c)
        app.work_model = "opus 5"
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
              "/compact" in c.queries and wants_fresh is False
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
              wants_fresh is False and "/compact" not in c.queries
              and app._last_ctx == 5000,
              f"queries={c.queries}")

        # 17a. …and the panel is told how full the session is (the meter)
        ctx_ev = [e for e in app.events if e[0] == "work_ctx"]
        check("session fill reaches the work panel meter",
              ctx_ev and ctx_ev[-1][1] == f"5000|{g.ROTATE_CTX}",
              f"work_ctx={ctx_ev}")

        # 17b. thinking depth (2026-09-14): the dial moves, the session is
        #      kept, and _consume asks run() for a rebuild instead of exiting.
        c = MockClient(script=[])
        app = make_app(c)
        app.set_effort("low")
        moved = (app.effort == "low" and app._effort_dirty
                 and has(app, "effort"))
        app._reopen_only = True
        wants_fresh = await app._consume()
        check("effort change asks for a session-keeping reopen",
              moved and wants_fresh is True,
              f"effort={app.effort} dirty={app._effort_dirty} "
              f"wants_fresh={wants_fresh}")

        # 17c. an unknown level is ignored (no reopen on a typo / bad voice
        #      transcription — that would drop the session for nothing).
        app = make_app(c)
        app.set_effort("maximum")
        check("unknown thinking level is refused",
              app.effort == g.DEFAULT_EFFORT and not app._effort_dirty,
              f"effort={app.effort} dirty={app._effort_dirty}")

        # 17d. summarized thinking reaches the left panel as work_think
        def _ev(delta):
            return StreamEvent(uuid="u", session_id="s",
                               event={"type": "content_block_delta",
                                      "delta": delta})
        think_ev = _ev({"type": "thinking_delta",
                        "thinking": "checking the config first"})
        text_ev = _ev({"type": "text_delta", "text": "done"})
        c = MockClient(script=[think_ev, text_ev, result_msg()])
        app = make_app(c)
        app.busy = True
        app.last_user_text = "task"
        await app._consume()
        kinds = [e[0] for e in app.events]
        check("thinking summaries stream to the work panel",
              "work_think" in kinds and "work_text" in kinds
              and kinds.index("work_think") < kinds.index("work_text"),
              f"events={kinds}")

        # 17e. thinking is muted while a /compact turn runs (it is not his
        #      work and must not litter the ledger)
        c = MockClient(script=[think_ev, result_msg()])
        app = make_app(c)
        app.busy = True
        app._compacting = True
        app.last_user_text = "task"
        await app._consume()
        check("compact turn does not leak thinking into the ledger",
              "work_think" not in [e[0] for e in app.events],
              f"events={[e[0] for e in app.events]}")

        # ---- bilingual hearing (2026-09-14, his goal: "listen to my
        #      georgian and not hear english when I speak georgian") ----
        class FakeEar:
            """Stands in for the cloud ear; records how it was called."""
            def __init__(self, text, lang):
                self.text, self.lang, self.calls = text, lang, []
            def available(self):
                return True
            def transcribe_lang(self, audio, sr=16000, force=None):
                self.calls.append(force)
                return self.text, self.lang
            def script_lang(self, text):
                return g.stt_gladia.__class__ and ("ka" if any(
                    "Ⴀ" <= c <= "ჿ" for c in text) else "en")

        class FakeWhisper:
            def __init__(self):
                self.calls = 0
            def transcribe(self, audio):
                self.calls += 1
                return "english words"

        real_ear, real_whisper, real_tts = g.stt_gladia, g.stt_whisper, g.tts_edge

        class FakeTTSEdge:
            def __init__(self):
                self.lang = "en"
            def set_language(self, lang):
                self.lang = lang

        async def hear(mode, ear_text, ear_lang):
            """One utterance through the real routing with fake ears."""
            app = make_app(MockClient())
            app.language = mode
            app.wake_enabled = False
            app.audio = type("A", (), {"is_tts_playing": False})()
            spoken = []
            async def fake_talk(text, echo=True):
                spoken.append(text)
            app._talk = fake_talk
            g.stt_gladia = FakeEar(ear_text, ear_lang)
            g.stt_whisper = FakeWhisper()
            g.tts_edge = FakeTTSEdge()
            await app._handle_utterance(np.zeros(16000, dtype=np.float32))
            return app, spoken, g.stt_gladia, g.stt_whisper, g.tts_edge

        try:
            # 17f. auto mode + Georgian speech -> Georgian turn and voice,
            #      and the ear is NOT pinned to a language
            app, spoken, ear, whis, tts = await hear(
                "auto", "გამარჯობა, როგორ ხარ?", "ka")
            check("bilingual: georgian speech -> georgian turn + voice",
                  app.turn_lang == "ka" and tts.lang == "ka"
                  and ear.calls == [None] and whis.calls == 0
                  and spoken == ["გამარჯობა, როგორ ხარ?"],
                  f"lang={app.turn_lang} voice={tts.lang} ear={ear.calls}")

            # 17g. same session, English speech -> back to English
            app, spoken, ear, whis, tts = await hear(
                "auto", "what time is it", "en")
            check("bilingual: english speech -> english turn",
                  app.turn_lang == "en" and tts.lang == "en"
                  and ear.calls == [None],
                  f"lang={app.turn_lang} voice={tts.lang}")

            # 17h. Georgian MODE pins the ear and the reply language even if
            #      the transcript came back English
            app, spoken, ear, whis, tts = await hear("ka", "hello there", "en")
            check("georgian mode stays georgian and pins the ear",
                  app.turn_lang == "ka" and ear.calls == ["ka"],
                  f"lang={app.turn_lang} ear={ear.calls}")

            # 17i. English mode never sends audio to the cloud
            app, spoken, ear, whis, tts = await hear("en", "unused", "ka")
            check("english mode keeps hearing local (no cloud audio)",
                  ear.calls == [] and whis.calls == 1 and app.turn_lang == "en",
                  f"ear={ear.calls} whisper={whis.calls}")
        finally:
            g.stt_gladia, g.stt_whisper, g.tts_edge = real_ear, real_whisper, real_tts

        # 17j. a Georgian turn tells the WORK lane to answer in Georgian
        c = MockClient()
        app = make_app(c)
        app.turn_lang = "ka"
        await app._work("ფასმეტრი შეამოწმე")
        check("georgian turn carries the language into the work lane",
              c.queries and c.queries[0].startswith("[language:")
              and "ქართული" in c.queries[0]
              and c.queries[0].endswith("ფასმეტრი შეამოწმე"),
              f"queries={c.queries}")

        # 17k. english turn adds nothing
        c = MockClient()
        app = make_app(c)
        await app._work("check fasmetri")
        check("english turn leaves the work order untouched",
              c.queries == ["check fasmetri"], f"queries={c.queries}")

        # 18. work bridges recent talk-lane chat as context
        g.local_llm = fake = FakeLocal(up=True, reply="Sounds fun.")
        c = MockClient()
        app = make_app(c)
        app.work_model = "opus 5"
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
    # 20b. the same dispatch vocabulary, spoken in Georgian (2026-09-14)
    ka_dispatch = ("ოპუს, გაასწორე ბილდი", "მუშა ტვინი, ფასმეტრი შეამოწმე",
                   "ფეიბლ, დაწერე სკრიპტი", "მძიმე ტვინი, დაიწყე რეფაქტორინგი")
    ka_status = ("რას აკეთებს მუშა ტვინი?", "მუშა ტვინმა დაასრულა?",
                 "როგორ მიდის საქმე?")
    ka_talk = ("გამარჯობა, როგორ ხარ?", "მომწონს მუშა ტვინის იდეა")
    check("georgian addresses dispatch to the work lane",
          all(g.WORK_DISPATCH_RE.match(t) and not g.WORK_STATUS_ASK_RE.search(t)
              for t in ka_dispatch),
          f"missed={[t for t in ka_dispatch if not g.WORK_DISPATCH_RE.match(t)]}")
    check("georgian status questions stay talk",
          all(g.WORK_STATUS_ASK_RE.search(t) for t in ka_status),
          f"missed={[t for t in ka_status if not g.WORK_STATUS_ASK_RE.search(t)]}")
    check("plain georgian talk never dispatches",
          not any(g.WORK_DISPATCH_RE.match(t) or g.WORK_ASK_RE.search(t)
                  for t in ka_talk), "a talk line dispatched")
    check("georgian 'მძიმე'/'ოპუს' pick the hard brain",
          g.WORK_HARD_RE.search("მძიმე ტვინი დაიწყე")
          and g.WORK_HARD_RE.search("ოპუს, გააკეთე")
          and not g.WORK_HARD_RE.search("მუშა ტვინი, გააკეთე"),
          "hard-brain detection off in georgian")
    check("georgian stop-orders brake the work lane",
          all(g.STOP_RE.search(t) for t in ("გაჩერდი", "შეწყვიტე", "მოიცა"))
          and not g.STOP_RE.search("გამარჯობა"),
          "georgian brake words not recognized")

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
          and ui_qt.DEFAULT_CFG["work_model"] == "opus 5"
          and ui_qt.LANGS.get("ორივე") == "auto"
          and ui_qt.EFFORT_OPTS[-1] == "max"
          and ui_qt.DEFAULT_CFG["effort"] == "max"
          and ui_qt.DEFAULT_CFG["hard_model"] == "opus 5")
    check("UI brain option lists",
          ui_qt.TALK_OPTS == ["gemini flash", "sonnet 5"]
          and ui_qt.WORK_OPTS == ["opus 5", "fable 5.1"])
    # Every name the drawer offers must resolve in the engine, or picking it
    # silently lands on a default and the footer starts lying again.
    check("UI rosters resolve in the engine",
          all(n in g.TALK_BRAINS for n in ui_qt.TALK_OPTS)
          and all(n in g.WORK_BRAINS for n in ui_qt.WORK_OPTS)
          and all(n in ui_qt.EFFORT_OPTS for n in g.EFFORT_LEVELS))
    check("every talk/work model id has a speakable footer name",
          all(m == "gemini" or m in g.MODEL_NAMES
              for m in g.TALK_BRAINS.values())
          and all(m in g.MODEL_NAMES for m in g.WORK_BRAINS.values()))

    # ---- ESCALATE recognition (the talking brain punting to the work lane) ----
    # Exact-match used to be the rule, so a garnished signal was SPOKEN to him
    # as though it were an answer. These are the shapes seen in the wild.
    for raw in ("ESCALATE", "escalate", "  ESCALATE  ", "ESCALATE.",
                "ESCALATE!", "Escalate — handing that to the working brain."):
        check(f"escalate recognised: {raw.strip()!r}", g.is_escalation(raw))
    for raw in ("", None, "I'd escalate this to the working brain if you want "
                "me to actually run it, but here is what I think is happening: "
                "the confidence gate is set too high.",
                "That escalated quickly.", "Escalators are stairs that move."):
        check(f"not an escalation: {str(raw)[:34]!r}", not g.is_escalation(raw))

    # ---- spoken outcome shape ----
    # "Done." is only added when the work brain didn't already report one, and
    # never in front of a question — "Done. Which branch?" is a lie + a question.
    check("outcome: plain sentence gets the lead",
          g.outcome_line("x", "Done.", "The gate is aligned at 85.")
          == "Done. The gate is aligned at 85.")
    check("outcome: brain's own report is not doubled",
          g.outcome_line("x", "Done.", "Fixed — the gate compared 90 to 85.")
          == "Fixed — the gate compared 90 to 85.")
    check("outcome: a question is spoken alone",
          g.outcome_line("x", "Done.", "Which branch should I push to?")
          == "Which branch should I push to?")
    check("outcome: georgian report is not doubled",
          g.outcome_line("x", "მზადაა.", "გასწორდა — ბილდი მწვანეა.")
          == "გასწორდა — ბილდი მწვანეა.")
    check("outcome: empty reply falls back to the lead alone",
          g.outcome_line("", "Done.", "") == "Done.")

    def _clamped(v):
        return min(ui_qt.UI_SCALE_MAX, max(ui_qt.UI_SCALE_MIN, float(v)))
    check("ui scale clamps out-of-range requests",
          _clamped(9.0) == ui_qt.UI_SCALE_MAX
          and _clamped(0.1) == ui_qt.UI_SCALE_MIN and _clamped(1.5) == 1.5)

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
