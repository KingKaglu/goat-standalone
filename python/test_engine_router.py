"""Engine logic tests — one brain with sight (2026-09-23 redesign).

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

    def say(self, text, filler=False):
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
    app._step_ctx = 0
    app._exchanges = deque(maxlen=g.HANDOFF_KEEP)
    app._reply_acc = ""
    app._rotate_only = False
    app._pending_handoff = ""
    app._compacting = False
    app._cut = False
    app._after_cut = None
    app._warming = False
    app.language = "en"
    app.turn_lang = "en"
    app._local_unseen = []
    return app


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
        # ---- ONE BRAIN (2026-09-23, his order) ----
        # The talking lane (Gemini / Sonnet) is gone. Every non-reflex turn
        # reaches the one Claude brain, carrying a live desktop note, and the
        # brain's own text is what GOAT says.
        g.BACKCHANNEL_MODE = "off"      # no stray listening-noise tasks here
        g.live_view = type("LV", (), {
            "note": staticmethod(lambda: "[live desktop, test note]"),
            "prime": staticmethod(lambda: None)})

        # 1. small talk reaches the brain, with sight attached
        c = MockClient()
        app = make_app(c)
        await app._talk("how are you doing")
        check("small talk reaches the one brain with the live desktop note",
              c.queries == ["[live desktop, test note]\n\nhow are you doing"]
              and app.busy, f"queries={c.queries}")

        # 2. the exact question the old talking brain refused
        c = MockClient()
        app = make_app(c)
        await app._talk("what's on my screen right now?")
        check("a screen question is answered by the brain that can see",
              c.queries and c.queries[0].startswith("[live desktop")
              and c.queries[0].endswith("what's on my screen right now?"),
              f"queries={c.queries}")

        # 3. work_model / hard_model still pick the model
        c = MockClient()
        app = make_app(c)
        app.work_model = "opus 5.5"
        await app._work("fix the scroll bug in the app")
        check("normal turn -> work_model",
              c.models == [g.MODEL_FULL] and app.busy
              and c.queries[0].endswith("fix the scroll bug in the app")
              and has(app, "work_start"), f"models={c.models}")
        c = MockClient()
        app = make_app(c)
        app.hard_model = "fable 5.1"
        await app._talk("hard brain, run the heavy migration")
        check("'hard brain' -> hard_model", c.models == [g.MODEL_FABLE],
              f"models={c.models}")

        # 4. Georgian goes to the same brain, told to answer in Georgian
        c = MockClient()
        app = make_app(c)
        app.turn_lang = "ka"
        await app._talk("რა არის ჩემს ეკრანზე?")
        check("georgian turn -> same brain, georgian note, sight attached",
              c.queries and c.queries[0].startswith("[language:")
              and "[live desktop" in c.queries[0]
              and c.queries[0].endswith("რა არის ჩემს ეკრანზე?"),
              f"queries={c.queries}")

        # 5. REFLEX LANE: a device command runs directly, no brain at all
        class StubReflex:
            def __init__(self):
                self.ran = []

            def match(self, text, lang="en"):
                if not text.lower().startswith("open "):
                    return None
                return _reflex_mod.Reflex(
                    "open_url", "example.com",
                    lambda t=text: self.ran.append(t) or "opened")

        real_reflex = g.reflex
        try:
            g.reflex = stub = StubReflex()
            c = MockClient()
            app = make_app(c)
            app._log_exchange = lambda *a: None   # keep his transcript clean
            await app._talk("open google")
            check("a reflex runs the action and skips the brain",
                  stub.ran == ["open google"] and not c.queries,
                  f"ran={stub.ran} queries={c.queries}")
            check("a reflex speaks a cached acknowledgement immediately",
                  app.tts.spoken and app.tts.spoken[0] in g.ACK_REFLEX["en"],
                  f"said={app.tts.spoken}")
            await app._talk("did you open it?")
            check("the brain is told what the reflex did",
                  c.queries and "[chat since your last turn" in c.queries[0]
                  and "open google" in c.queries[0], f"queries={c.queries}")

            g.reflex = stub = StubReflex()
            stub.match = lambda text, lang="en": (
                _reflex_mod.Reflex("open_path", "nope",
                                   lambda: "ERROR: no such file")
                if text.startswith("open ") else None)
            app = make_app(MockClient())
            app._log_exchange = lambda *a: None
            await app._talk("open nothing")
            check("a failed reflex says so instead of saying done",
                  g.REFLEX_FAIL["en"] in app.tts.spoken,
                  f"said={app.tts.spoken}")

            g.reflex = StubReflex()
            app = make_app(MockClient(script=[result_msg(is_error=True)]))
            app.busy = True
            await app._talk("stop")
            await app._consume()
            check("the stop brake still wins over the reflex lane",
                  not app.busy and "Stopped." in app.tts.spoken,
                  f"busy={app.busy} said={app.tts.spoken}")
        finally:
            g.reflex = real_reflex

        # 6. spoken 'stop' brakes a running turn. interrupt() ends the turn
        # with an error_during_execution result (measured) — that result must
        # be swallowed, not followed by "That one failed."
        c = MockClient(script=[result_msg(is_error=True)])
        app = make_app(c)
        app.busy = True
        interrupted = []

        async def fake_interrupt():
            interrupted.append(True)
        app._safe_interrupt = fake_interrupt
        await app._talk("stop")
        await app._consume()
        check("spoken stop brakes the running turn, says only 'Stopped.'",
              interrupted and app.tts.spoken == ["Stopped."]
              and not app.busy and c.queries == [] and has(app, "work_fail"),
              f"interrupted={interrupted} spoken={app.tts.spoken}")

        # 7. JARVIS mid-turn (his goal 2026-09-25: "it keeps saying ესეც
        # ჩავამატე"). Nothing he says mid-turn is ever answered with an
        # "adding that" line.
        # 7a. real work under way (tools ran) -> folded in, silently
        c = MockClient()
        app = make_app(c)
        app.busy = True
        app._turn_has_tools = True
        app.last_user_text = "original"
        app.turn_lang = "ka"
        await app._talk("ტესტებიც დაამატე")
        check("mid-work order folds in silently (no 'ესეც ჩავამატე')",
              c.queries == ["ტესტებიც დაამატე"]
              and app.last_user_text == "original\nტესტებიც დაამატე"
              and has(app, "work_add") and app.tts.spoken == [],
              f"queries={c.queries} spoken={app.tts.spoken}")

        # 7b. talking over a spoken answer -> old answer dropped, his new
        # words answered as a fresh turn
        c = MockClient(script=[result_msg(is_error=True)])
        app = make_app(c)
        app._log_exchange = lambda *a: None
        app.busy = True
        app.last_user_text = "tell me about the weather"
        app._reply_acc = "The weather today is"
        app._say_buf = "and tomorrow"
        interrupted = []
        app._safe_interrupt = fake_interrupt
        await app._talk("what time is it")
        mid = (app._cut, app.suppressed, list(c.queries))
        await app._consume()
        for _ in range(200):          # the fresh turn runs as its own task
            if c.queries:
                break
            await asyncio.sleep(0.01)
        check("talking over GOAT cuts the answer and answers him",
              interrupted and mid == (True, True, [])
              and len(c.queries) == 1
              and c.queries[0].endswith("what time is it")
              and app.busy and not app._cut and not app.suppressed
              and app.tts.spoken == [],
              f"mid={mid} queries={c.queries} spoken={app.tts.spoken}")

        # 7c. more words while the cut is in flight ride along with it
        c = MockClient(script=[result_msg(is_error=True)])
        app = make_app(c)
        app.busy = True
        app._safe_interrupt = fake_interrupt
        await app._talk("wait")
        await app._talk("open the other one")
        await app._consume()
        for _ in range(200):          # the fresh turn runs as its own task
            if c.queries:
                break
            await asyncio.sleep(0.01)
        check("words during a cut are answered together, once",
              len(c.queries) == 1
              and c.queries[0].endswith("wait\nopen the other one"),
              f"queries={c.queries}")
        check("the 'adding that' ack pool is gone",
              not hasattr(g, "ACK_ADD"))

        # 7d. warm-up (2026-09-25 speed goal): a muted turn after connect so
        # his first real turn doesn't pay the cold prefill (3.1s vs 0.8s)
        c = MockClient(script=[result_msg()])
        app = make_app(c)
        app._pending_handoff = "[context-handoff] earlier chat"
        await app._warm_brain()
        mid = (app.busy, app.suppressed, app._warming)
        await app._consume()
        check("warm-up runs muted, carries the handoff, then frees the brain",
              mid == (True, True, True) and len(c.queries) == 1
              and c.queries[0].startswith("[context-handoff] earlier chat")
              and "[warm-up]" in c.queries[0]
              and not app.busy and not app.suppressed and not app._warming
              and app._pending_handoff == "" and app.tts.spoken == [],
              f"mid={mid} queries={c.queries} spoken={app.tts.spoken}")

        # 7e. he speaks during the warm-up: it is cut, and HIS turn is not
        # swallowed as if it were the warm-up
        c = MockClient(script=[result_msg(is_error=True)])
        app = make_app(c)
        app._safe_interrupt = fake_interrupt
        await app._warm_brain()
        await app._talk("what time is it")
        await app._consume()
        for _ in range(200):
            if len(c.queries) > 1:
                break
            await asyncio.sleep(0.01)
        check("speaking during the warm-up cuts it and answers him",
              len(c.queries) == 2 and c.queries[1].endswith("what time is it")
              and app.busy and not app._warming and not app.suppressed,
              f"queries={c.queries} warming={app._warming}")

        # 8. Claude out -> no query, an honest spoken line, reflexes still named
        c = MockClient()
        app = make_app(c)
        app.claude_out = True
        app.claude_reset = "14:30"
        await app._talk("build something")
        check("claude out -> no query, spoken line with the reset time",
              c.queries == [] and not app.busy and has(app, "work_fail")
              and any("14:30" in s for s in app.tts.spoken),
              f"queries={c.queries} spoken={app.tts.spoken}")

        # 9. quota exhausted mid-turn
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

        c = MockClient(script=[result_msg(
            is_error=True,
            result="You've hit your session limit · resets 6:05pm "
                   "(Asia/Tbilisi)")])
        app = make_app(c)
        app.busy = True
        await app._consume()
        check("pm reset wording -> 18:05",
              app.claude_reset == "18:05", f"reset={app.claude_reset}")

        # 10. THE BRAIN SPEAKS: its streamed text is what GOAT says, in the
        #     middle, and the turn closes cleanly
        def _ev(delta):
            return StreamEvent(uuid="u", session_id="s",
                               event={"type": "content_block_delta",
                                      "delta": delta})
        think_ev = _ev({"type": "thinking_delta",
                        "thinking": "checking the config first"})
        c = MockClient(script=[
            think_ev,
            _ev({"type": "text_delta", "text": "Steam is closed. "}),
            _ev({"type": "text_delta", "text": "Ubisoft too"}),
            result_msg(ctx=5000)])
        app = make_app(c)
        app.busy = True
        app.claude_out = True           # a landing turn clears it
        app.last_user_text = "close steam and ubisoft"
        logged = []
        app._log_exchange = lambda u, r: logged.append((u, r))
        await app._consume()
        kinds = [e[0] for e in app.events]
        check("the brain's reply is spoken as it streams, tail included",
              "Steam is closed." in app.tts.spoken
              and any("Ubisoft too" in s for s in app.tts.spoken)
              and "delta" in kinds and "work_text" not in kinds,
              f"spoken={app.tts.spoken} events={kinds}")
        check("turn closes: work_done, quota flag cleared, exchange kept",
              has(app, "work_done") and app._reply_acc == ""
              and app._last_ctx == 5000 and app.claude_out is False,
              f"ctx={app._last_ctx} out={app.claude_out}")
        check("thinking still reaches the side panel, not the voice",
              "work_think" in kinds
              and not any("checking the config" in s for s in app.tts.spoken),
              f"events={kinds}")

        # 11. error with nothing produced -> work_fail, no crash
        c = MockClient(script=[result_msg(is_error=True, result="boom")])
        app = make_app(c)
        app.busy = True
        app.last_user_text = "task"
        await app._consume()
        check("brain error -> work_fail, no crash",
              has(app, "work_fail") and has(app, "work_done") and not app.busy,
              f"events={[e[0] for e in app.events]}")

        # ---- context economy ----
        # Every case here can rotate, and rotation deletes SESSION_FILE — so
        # point it at a temp file. (2026-09-25: this block ran against the
        # REAL .goat-session-py and deleted his live session pointer.)
        tmp2 = tempfile.NamedTemporaryFile(delete=False)
        tmp2.write(b"sid")
        tmp2.close()
        old_session = g.SESSION_FILE
        old_compact = g.COMPACT_CLI
        g.SESSION_FILE = tmp2.name
        try:
            # Default since the one-brain switch: rotate, never a blocking
            # /compact (it froze the only brain for 2m09s).
            check("compact is off by default", old_compact is False)
            c = MockClient(script=[result_msg(ctx=g.ROTATE_CTX + 5000)])
            app = make_app(c)
            app.busy = True
            app.last_user_text = "big work"
            wants_fresh = await app._consume()
            check("past 60k rotates at once, no /compact turn",
                  "/compact" not in c.queries and wants_fresh is True
                  and not app._compacting,
                  f"queries={c.queries} wants_fresh={wants_fresh}")

            with open(tmp2.name, "w") as f:
                f.write("sid")
            g.COMPACT_CLI = True    # opt-in path (GOAT_COMPACT=on)
            big = result_msg(ctx=g.ROTATE_CTX + 5000)
            small_after = result_msg(ctx=100)
            c = MockClient(script=[big, small_after], ctx_after=9000)
            app = make_app(c)
            app.busy = True
            app.last_user_text = "big work"
            wants_fresh = await app._consume()
            check("opt-in compact fires past 60k and session survives",
                  "/compact" in c.queries and wants_fresh is False
                  and app._last_ctx == 9000 and not app._compacting,
                  f"queries={c.queries} wants_fresh={wants_fresh} ctx={app._last_ctx}")

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
            g.COMPACT_CLI = old_compact
            if os.path.exists(tmp2.name):
                os.unlink(tmp2.name)

        c = MockClient(script=[result_msg(ctx=5000)])
        app = make_app(c)
        app.busy = True
        app.last_user_text = "hello"
        wants_fresh = await app._consume()
        check("small session untouched",
              wants_fresh is False and "/compact" not in c.queries
              and app._last_ctx == 5000, f"queries={c.queries}")
        ctx_ev = [e for e in app.events if e[0] == "work_ctx"]
        check("session fill reaches the work panel meter",
              ctx_ev and ctx_ev[-1][1] == f"5000|{g.ROTATE_CTX}",
              f"work_ctx={ctx_ev}")

        c = MockClient(script=[])
        app = make_app(c)
        app.set_effort("low")
        moved = (app.effort == "low" and app._effort_dirty
                 and has(app, "effort"))
        app._reopen_only = True
        wants_fresh = await app._consume()
        check("effort change asks for a session-keeping reopen",
              moved and wants_fresh is True,
              f"effort={app.effort} dirty={app._effort_dirty}")
        app = make_app(c)
        app.set_effort("maximum")
        check("unknown thinking level is refused",
              app.effort == g.DEFAULT_EFFORT and not app._effort_dirty,
              f"effort={app.effort} dirty={app._effort_dirty}")

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

    # ---- UI config: three brain roles + scale clamp ----
    import ui_qt
    check("UI default roles present (no talking brain)",
          "talk_brain" not in ui_qt.DEFAULT_CFG
          and ui_qt.DEFAULT_CFG["work_model"] == "opus 5.5"
          and ui_qt.LANGS.get("ორივე") == "auto"
          and ui_qt.EFFORT_OPTS[-1] == "max"
          and ui_qt.DEFAULT_CFG["effort"] == "high"
          and ui_qt.DEFAULT_CFG["hard_model"] == "opus 5.5")
    check("UI brain option lists",
          not hasattr(ui_qt, "TALK_OPTS")
          and ui_qt.WORK_OPTS == ["opus 5.5", "fable 5.1"])
    # Every name the drawer offers must resolve in the engine, or picking it
    # silently lands on a default and the footer starts lying again.
    check("UI rosters resolve in the engine",
          all(n in g.WORK_BRAINS for n in ui_qt.WORK_OPTS)
          and all(n in ui_qt.EFFORT_OPTS for n in g.EFFORT_LEVELS))
    check("every model id has a speakable footer name",
          all(m in g.MODEL_NAMES for m in g.WORK_BRAINS.values()))

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
