"""Two brains (2026-10-24): Gemini is the voice, Claude does the work.

No audio, no SDK subprocess, no API cost: Claude, Gemini and the TTS are
mocks. Verifies the relationship end to end:
- the voice hands Claude a job (hand_to_worker) and Claude runs it silently:
  its words go to the Activity panel, never the speaker;
- Claude's finished job reaches the watch and the voice reports it;
- the voice sees Claude's live job (steps, thinking) when he asks;
- stuck / repeating / failing / long jobs are noticed, at the right moment;
- a bare "stop" hushes the voice and leaves Claude's job running; "stop
  the job" (stop_worker) stops Claude without cutting the voice;
- a job given while Claude is busy becomes a note into the running job;
- Gemini down -> Claude covers the turn out loud;
- Fable 5.1 refused for credits -> the same job reruns on Opus 5.5;
- news that arrives while he talks is folded into the answer, not repeated.

Run:  python test_two_brains.py
"""
import asyncio
import os
import re
import time
import types
from collections import deque

os.environ.setdefault("GOAT_REFLEX", "off")

import tempfile

import goat_app as g
import talk_brain as tb
# Never write test exchanges into his real transcript (2026-10-24: a run did).
g.TRANSCRIPT_FILE = os.path.join(tempfile.gettempdir(), "goat_test_transcript.jsonl")
g.EFFORT_ROUTING = False
from claude_agent_sdk import (AssistantMessage, ResultMessage, StreamEvent,
                              TextBlock, ToolResultBlock, ToolUseBlock,
                              UserMessage)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


class MockTTS:
    def __init__(self):
        self.spoken = []
        self.gen = 0
        self.enabled = True
        self._sounded = False
        self.playing = False

    def mark_reply(self):
        pass

    def speaking(self):
        return self.playing

    def new_turn(self):
        self._sounded = False

    def cancel(self):
        self.gen += 1
        self.playing = False

    def say(self, text, filler=False):
        self.spoken.append(text)

    def say_lead(self, lead):
        pass


class MockClient:
    def __init__(self, script=None):
        self.queries, self.models, self.script = [], [], script or []
        self.interrupted = 0

    async def query(self, text, **kw):
        self.queries.append(text)

    async def set_model(self, model):
        self.models.append(model)

    async def interrupt(self):
        self.interrupted += 1

    async def receive_messages(self):
        for m in self.script:
            yield m


class FakeTalker:
    """Scripted Gemini: script(user_text_or_event) -> (chunks, calls) or None
    for a failure."""

    def __init__(self, script):
        self.script = script
        self.history = deque(maxlen=24)
        self.seen = []
        self.fail_streak = 0

    def label(self):
        return "Gemini 3.6 Flash"

    out = ""          # set to "09:00" to simulate every model out of quota

    def usable(self):
        return not self.out

    def out_until(self):
        return self.out

    def quota_note(self):
        return ""

    def remember(self, user, reply):
        self.history.append({"role": "user", "text": user})
        self.history.append({"role": "model", "text": reply})

    async def reply(self, parts, on_text, on_call):
        text = parts[0]["text"]
        self.seen.append(text)
        out = self.script(text)
        if out is None:
            self.fail_streak += 1
            return {"ok": False, "error": "down", "model": "", "ttft": None}
        chunks, calls = out
        for c in chunks:
            on_text(c)
        for name, args in calls:
            on_call(name, args)
        return {"ok": True, "error": "", "model": "gemini-3.6-flash", "ttft": 0.9}


def make_app(talker, client=None):
    app = g.GoatApp.__new__(g.GoatApp)
    app.events = []
    app.emit = lambda *a: app.events.append(a)
    app.client = client or MockClient()
    app.tts = MockTTS()
    app.audio = types.SimpleNamespace(is_tts_playing=False)
    app.work_model = "fable 5.1"
    app.hard_model = "fable 5.1"
    app.model = g.MODEL_FULL
    app.effort = "max"
    app.loop = None
    app.busy = False
    app.last_user_text = None
    app.claude_out = False
    app.claude_reset = ""
    app.claude_reset_at = 0.0
    app._limit_hit_at = 0.0
    app._resume_text = ""
    app._resume_watch = None
    app.suppressed = False
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
    app._reopen_only = False
    app._pending_handoff = ""
    app._compacting = False
    app._cut = False
    app._after_cut = None
    app._warming = False
    app.language = "en"
    app.turn_lang = "en"
    app._local_unseen = []
    app._side_until = 0.0
    app._side_probe = False
    app._quiet_state = "speak"
    app._quiet_probe = ""
    app._first_said = False
    app._stale_voice = False
    app._hushed_at = 0.0
    app._cut_in = None
    app.wake_enabled = False
    app._asleep = False
    app.mic_muted = False
    # two brains
    app.two_brains = True
    app.talker = talker
    app.fable = types.SimpleNamespace(ok=False, reason="needs usage credits",
                                      refused=lambda r: None)
    app._watch = tb.WorkerWatch()
    app._body = types.SimpleNamespace(note=lambda full=False: "")
    app._inner = types.SimpleNamespace(turn_note=lambda t: "")
    app._job_args = None
    app._front_task = None
    app._plan_task = None
    app._work_options = None
    app._body_client = None
    return app


async def settle():
    # Long enough for the to_thread desktop snapshot inside a dispatch.
    for _ in range(10):
        await asyncio.sleep(0.03)


def stream_text(t):
    return StreamEvent(uuid="u", session_id="s", event={
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": t}})


def result(is_error=False, text=None, ctx=1000):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                         is_error=is_error, num_turns=1, session_id="s",
                         usage={"input_tokens": ctx, "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0}, result=text)


def kinds(app):
    return [e[0] for e in app.events]


async def main():
    g.live_view.note = lambda *a, **k: "[live desktop] front: Brave"
    g.self_update.take_notice = lambda: ""
    async def _no_plan():
        return None
    g.GoatApp._pull_plan_usage = lambda self: _no_plan()

    # ---- 1. the voice hands Claude a job; Claude works silently ----------
    def script1(text):
        if "Giorgi: clean my downloads" in text:
            return (["Sir, handing that to Claude."],
                    [("hand_to_worker", {"task": "Delete installers older than 30 days "
                                                 "in Downloads.", "depth": "quick"})])
        if "[event] Claude FINISHED" in text:
            return (["Sir, nine old installers are gone."], [])
        return (["Sir, noted."], [])
    t = FakeTalker(script1)
    app = make_app(t)
    await app._talk("clean my downloads")
    await app._front_task
    await settle()
    q = app.client.queries[-1] if app.client.queries else ""
    check("voice speaks its hand-off line", "Sir, handing that to Claude." in " ".join(app.tts.spoken),
          app.tts.spoken)
    check("Claude gets a job with his own words and the voice's task",
          "[job from GOAT's voice" in q and 'Giorgi said: "clean my downloads"' in q
          and "Delete installers older than 30 days" in q, q[-300:])
    check("the job runs on Opus 5.5 while Fable has no credits",
          app.busy and app._job_silent and (app.model == g.MODEL_FULL), (app.model, app.client.models))
    check("Activity panel gets the job", "work_start" in kinds(app))
    check("watch knows Claude is running", app._watch.running())

    # Claude streams words, uses a tool, hits one error, then finishes.
    spoken_before = list(app.tts.spoken)
    app.client.script = [
        stream_text("Found 9 installers older than 30 days."),
        AssistantMessage(content=[TextBlock("Found 9 installers older than 30 days."),
                                  ToolUseBlock("t1", "PowerShell", {"command": "Remove-Item ..."})],
                         model="m"),
        UserMessage(content=[ToolResultBlock("t1", "ok", is_error=False)]),
        AssistantMessage(content=[TextBlock("Deleted all 9; freed 3.2 GB. Verified the "
                                            "folder no longer has them.")], model="m"),
        result(),
    ]
    await app._consume()
    check("Claude's words never reach the speaker", app.tts.spoken == spoken_before,
          app.tts.spoken[len(spoken_before):])
    check("Claude's words go to the panel", "work_text" in kinds(app))
    check("job finished, Claude free", not app.busy and not app._job_silent)
    ev = app._watch.due(quiet=True, engaged_s=999, held=False)
    check("the watch has a 'done' report to voice", ev is not None and ev["kind"] == "done", ev)
    check("Claude's report is kept for the voice",
          "freed 3.2 GB" in app._watch.job["report"], app._watch.job["report"])
    app._front_start("", "event", ev)
    await app._front_task
    check("the voice reports the result in its own words",
          [x for x in app.tts.spoken if x][-1] == "Sir, nine old installers are gone.",
          app.tts.spoken)
    check("the report opens its own chat line", "voice_note" in kinds(app))
    check("the report shows in the Activity log", "work_voice" in kinds(app))
    check("the voice's event prompt carried Claude's report",
          "freed 3.2 GB" in t.seen[-1], t.seen[-1][-400:])

    # ---- 2. "what's it doing?" — the voice sees the live job -------------
    t2 = FakeTalker(lambda text: (["Sir, it's running the build."], []))
    app = make_app(t2)
    app._watch.start("Build the APK", "build the apk", "Opus 5.5", "deep")
    app._watch.on_event("work_tool", "Bash — bash build.sh")
    app._watch.on_event("work_think", "Gradle is compiling the release variant.")
    await app._talk("what's it doing right now?")
    await app._front_task
    seen = t2.seen[-1]
    check("status note: Claude is working on the job",
          "WORKING for" in seen and "Build the APK" in seen, seen[-500:])
    check("status note: current step and latest thinking",
          "bash build.sh" in seen and "Gradle is compiling" in seen)
    check("status note names Claude's model and Fable's state",
          "Opus 5.5" in seen and "Fable 5.1" in seen)

    # ---- 3. the watch: stuck, repeating, failing, check-ins, holds -------
    w = tb.WorkerWatch()
    w.start("long job", "", "Opus 5.5", "deep")
    w.job["next_checkin"] = time.monotonic() + 999
    w.job["last"] = time.monotonic() - tb.STUCK_QUIET_S - 5
    ev = w.due(True, 999, False)
    check("silent too long -> stuck", ev and ev["kind"] == "stuck", ev)
    check("stuck is said once, not every second", w.due(True, 999, False) is None)
    w = tb.WorkerWatch()
    w.start("loop job", "", "Opus 5.5", "deep")
    w.job["next_checkin"] = time.monotonic() + 999
    for _ in range(tb.STUCK_REPEAT):
        w.on_event("work_tool", "Bash — npm test")
    ev = w.due(True, 999, False)
    check("same step over and over -> stuck", ev and "repeating" in ev["why"], ev)
    w = tb.WorkerWatch()
    w.start("err job", "", "Opus 5.5", "deep")
    w.job["next_checkin"] = time.monotonic() + 999
    for _ in range(tb.STUCK_ERRORS):
        w.on_tool_result(True)
    ev = w.due(True, 999, False)
    check("tool calls failing in a row -> stuck", ev and "failed" in ev["why"], ev)
    w = tb.WorkerWatch()
    w.start("slow job", "", "Opus 5.5", "deep")
    w.job["next_checkin"] = time.monotonic() - 1
    check("he's mid-conversation -> no unasked check-in", w.due(True, 10, False) is None)
    w.job["next_checkin"] = time.monotonic() - 1
    ev = w.due(True, 999, False)
    check("long job, he's quiet -> one progress check-in", ev and ev["kind"] == "checkin", ev)
    w.finish("done", "All good.")
    check("never talk over him (not quiet)", w.due(False, 999, False) is None)
    check("a call/side talk holds the news", w.due(True, 999, True) is None)
    ev = w.due(True, 999, False)
    check("news is said once the moment is right", ev and ev["kind"] == "done", ev)
    w = tb.WorkerWatch()
    w.start("stopped job", "", "Opus 5.5", "deep")
    w.finish("stopped", "")
    check("a job he stopped is not reported again", w.due(True, 999, False) is None)
    w = tb.WorkerWatch()
    w.start("noted job", "", "Opus 5.5", "deep")
    w.job["next_checkin"] = time.monotonic() + 999
    r1 = w.note_from_worker("Found the bug, fixing it now.")
    r2 = w.note_from_worker("Second note right away.")
    ev = w.due(True, 999, False)
    check("Claude's tell_voice note gets voiced", ev and ev["kind"] == "note"
          and "Found the bug" in ev["note"], (r1, ev))
    check("Claude's notes are rate-limited", "noted" in r2 and w.due(True, 999, False) is None, r2)

    # ---- 4. bare "stop" hushes the voice, Claude keeps working -----------
    app = make_app(FakeTalker(lambda text: (["Sir, ok."], [])))
    app._watch.start("Build", "", "Opus 5.5", "deep")
    app.busy = True
    app._job_silent = True
    app.tts.playing = True        # the voice is mid-sentence
    await app._talk("stop")
    check("bare stop while the voice talks: Claude untouched",
          app.client.interrupted == 0 and app.busy and app._watch.running())
    check("bare stop gets no reply", app.tts.spoken == [], app.tts.spoken)

    # ---- 5. "stop the job" -> stop_worker; the voice keeps its words -----
    t5 = FakeTalker(lambda text: (["Sir, stopping Claude now."], [("stop_worker", {})]))
    app = make_app(t5)
    app._watch.start("Build", "", "Opus 5.5", "deep")
    app.busy = True
    app._job_silent = True
    await app._talk("cancel the build job, I changed my mind")
    await app._front_task
    await settle()
    check("stop_worker interrupts Claude", app.client.interrupted == 1)
    check("the voice's own line survives the stop",
          "Sir, stopping Claude now." in " ".join(app.tts.spoken), app.tts.spoken)
    app.client.script = [result(is_error=True, text="interrupted")]
    await app._consume()
    check("stopped job closes quietly (no second report)",
          not app.busy and app._watch.job["state"] == "stopped"
          and app._watch.due(True, 999, False) is None)

    # ---- 6. a job while Claude is busy becomes a note --------------------
    t6 = FakeTalker(lambda text: (["Sir, I'll tell Claude."],
                                  [("hand_to_worker", {"task": "Also bump the version."})]))
    app = make_app(t6)
    app._watch.start("Build", "", "Opus 5.5", "deep")
    app.busy = True
    app._job_silent = True
    await app._talk("and bump the version too")
    await app._front_task
    await settle()
    q = app.client.queries[-1] if app.client.queries else ""
    check("busy Claude gets the new ask as a note into its job",
          q.startswith("[note from Giorgi via the voice") and "bump the version" in q, q)
    check("the note is shown and remembered by the watch",
          "work_add" in kinds(app) and app._watch.job["added"])

    # ---- 7. Gemini down -> Claude covers the turn out loud ---------------
    app = make_app(FakeTalker(lambda text: None))
    await app._talk("what's the capital of Peru?")
    await app._front_task
    await settle()
    check("Gemini down: Claude takes the turn", app.busy and app._cover_turn
          and app.client.queries and "capital of Peru" in app.client.queries[-1])
    app.client.script = [stream_text("Sir, it's Lima."), result()]
    await app._consume()
    check("Claude's cover answer is spoken", any("Lima" in s for s in app.tts.spoken),
          app.tts.spoken)
    check("cover flag cleared after the turn", not app._cover_turn)

    # ---- 8. Fable refused for credits -> same job on Opus 5.5 ------------
    app = make_app(FakeTalker(lambda text: ([], [])))
    refused = []
    app.fable = types.SimpleNamespace(ok=True, reason="available",
                                      refused=lambda r: (refused.append(r),
                                                         setattr(app.fable, "ok", False)))
    await app._work_job("Refactor the parser", "refactor it", "deep")
    check("with credits, the job runs on Fable 5.1", app.client.models[-1:] == [g.MODEL_FABLE],
          app.client.models)
    app.model = g.MODEL_FABLE
    app.client.script = [result(is_error=True, text="Fable 5.1 requires usage credits.")]
    await app._consume()
    await settle()
    check("Fable refused -> gate closed", refused == ["needs usage credits"], refused)
    check("the same job reruns on Opus 5.5",
          app.client.models[-1] == g.MODEL_FULL and "Refactor the parser" in app.client.queries[-1],
          (app.client.models, app.client.queries[-1][-80:]))

    # ---- 9. news folded into his turn, never said twice ------------------
    t9 = FakeTalker(lambda text: (["Sir, it's sunny. And Claude's done."], []))
    app = make_app(t9)
    app._watch.start("Fix the bug", "", "Opus 5.5", "deep")
    app._watch.finish("done", "Fixed it; tests pass.")
    await app._talk("how's the weather?")
    await app._front_task
    check("finished job folded into his answer",
          "[news to fold in" in t9.seen[-1] and "tests pass" in t9.seen[-1])
    check("...and not voiced again afterwards", app._watch.due(True, 999, False) is None)

    # ---- 10. a news turn he talks over is not lost -----------------------
    async def slow_reply(parts, on_text, on_call):
        await asyncio.sleep(5)
        return {"ok": True, "error": "", "model": "m", "ttft": 1}
    t10 = FakeTalker(lambda text: ([], []))
    t10.reply = slow_reply
    app = make_app(t10)
    app._watch.start("Job", "", "Opus 5.5", "deep")
    app._watch.finish("done", "Done.")
    ev = app._watch.due(True, 999, False)
    task = app._front_start("", "event", ev)
    await asyncio.sleep(0.05)
    app._front_cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check("unsaid news goes back in the queue", app._watch.pending and
          app._watch.pending[0]["kind"] == "done")

    # ---- 11. empty hand-off still gets a spoken line ---------------------
    t11 = FakeTalker(lambda text: ([], [("hand_to_worker", {"task": "Open Spotify on my phone"})]))
    app = make_app(t11)
    await app._talk("open spotify on my phone")
    await app._front_task
    await settle()
    check("silent hand-off -> the voice still says it", any("Claude" in s for s in app.tts.spoken),
          app.tts.spoken)

    # ---- 12. Gemini out of free quota ------------------------------------
    t12 = FakeTalker(lambda text: (["never"], []))
    t12.out = "09:00"
    app = make_app(t12)
    app._watch.start("Build the APK", "", "Opus 5.5", "deep")
    app._watch.on_event("work_tool", "Bash — bash build.sh")
    app.busy = True
    app._job_silent = True
    await app._talk("what's it doing?")
    await app._front_task
    said = " ".join(app.tts.spoken)
    check("quota out: status question answered from the watch",
          "still on it" in said and "Bash" in said and not t12.seen, said)
    app.tts.spoken.clear()
    await app._talk("tell me a joke")
    await app._front_task
    said = " ".join(app.tts.spoken)
    check("quota out + Claude busy: honest line with the reset time",
          "out of free requests until 09:00" in said, said)
    app = make_app(t12)
    await app._talk("tell me a joke")
    await app._front_task
    await settle()
    check("quota out + Claude free: Claude covers the turn",
          app._cover_turn and app.client.queries and "joke" in app.client.queries[-1])

    # ---- 13. the lie gate ---------------------------------------------------
    t13 = FakeTalker(lambda text: (["Sir, I've passed that to Claude."], []))
    app = make_app(t13)
    app._watch.start("Restyle settings", "", "Opus 5.5", "deep")
    app.busy = True
    app._job_silent = True
    await app._talk("oh and make the button blue")
    await app._front_task
    await settle()
    q = app.client.queries[-1] if app.client.queries else ""
    check("said 'passed to Claude' without the tool -> the note really goes",
          q.startswith("[note from Giorgi via the voice") and "button blue" in q, q)
    check("claims are recognised (EN + KA), plain talk isn't",
          tb.CLAIM_RE.search("Sir, I'll tell Claude right away.")
          and tb.CLAIM_RE.search("Sir, Claude's on it.")
          and tb.CLAIM_RE.search("ბატონო, Claude-ს გადავეცი.")
          and not tb.CLAIM_RE.search("Sir, Claude finished the build an hour ago."))

    # ---- 14. Google's back-off errors are understood ------------------------
    day = ('{"error":{"code":429,"details":[{"@type":"x.QuotaFailure","violations":[{"quotaId":'
           '"GenerateRequestsPerDayPerProjectPerModel-FreeTier","quotaValue":"20"}]},'
           '{"@type":"x.RetryInfo","retryDelay":"54079s"}]}}')
    minute = ('{"error":{"code":429,"details":[{"@type":"x.QuotaFailure","violations":[{"quotaId":'
              '"GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},'
              '{"@type":"x.RetryInfo","retryDelay":"17s"}]}}')
    check("daily cap -> rest until Google's reset", tb._cooldown(429, day) == (54079.0, "day"))
    check("per-minute cap -> rest a minute", tb._cooldown(429, minute) == (60.0, "minute"))
    check("overloaded -> short rest", tb._cooldown(503, "{}") == (tb.BUSY_DOWN_S, "busy"))
    gt = tb.GeminiTalk.__new__(tb.GeminiTalk)
    gt._down = {tb.CHAIN[0]: (time.time() + 999, "day")}
    check("a resting model is skipped in the chain", gt._models() == tb.CHAIN[1:])
    gt._down = {m: (time.time() + 999, "day") for m in tb.CHAIN}
    gt.key = "k"
    check("all resting -> not usable, with a back-at time",
          not gt.usable() and re.match(r"\d\d:\d\d", gt.out_until()))

    # ---- 15. personas -----------------------------------------------------
    check("worker prompt: no spoken-voice rules", "SOUND NATURAL" not in g.WORKER_PERSONA
          and "IDENTITY IS ABSOLUTE" not in g.WORKER_PERSONA
          and "spoken aloud" not in g.WORKER_BASE_PROMPT)
    check("worker prompt: knows its partner and how to report",
          "GOAT's VOICE is Gemini" in g.WORKER_PERSONA and "REPORT" in g.WORKER_PERSONA)
    check("voice prompt: knows its partner and its tools",
          "YOUR PARTNER: Claude" in tb.TALK_SYSTEM and "hand_to_worker" in tb.TALK_SYSTEM
          and "[event]" in tb.TALK_SYSTEM)

    # ---- 16. the side card's names ------------------------------------------
    import ui_qt
    check("card shows the voice by a readable name",
          [ui_qt.voice_display(m) for m in ("gemini-3.6-flash", "gemini-3-flash-preview",
                                            "gemini-3.1-flash-lite", "Opus 5.5")]
          == ["Gemini 3.6 Flash", "Gemini 3 Flash", "Gemini 3.1 Flash-Lite", "Opus 5.5"])

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
