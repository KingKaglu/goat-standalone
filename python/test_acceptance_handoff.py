"""Acceptance tests for the two-brain fix (his order 2026-11-01).

1. "What's 2+2" -> direct answer, no handoff.
2. "Find my largest files in Downloads" -> handoff < 300 ms, one-line reply,
   result announced when done.
3. "What's it doing?" mid-task -> accurate current step.
4. Task needing his input -> voice says it's stuck/asking and why.
5. "Cancel that" -> cancelled and confirmed.
6. "Delete the test folder" -> confirmation asked first.
7. Chat-turn voice latency no worse than before (router on vs off).
8. Logs show every action claim next to the tools really called.

Same mocks as test_two_brains.py (no audio, no SDK, no API cost).
Run:  py -3.13 test_acceptance_handoff.py
"""
import asyncio
import contextlib
import io
import os
import statistics
import time

# The harness half of test_two_brains.py (mocks, make_app), without its run.
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_two_brains.py"),
            encoding="utf-8").read().split("async def main():")[0]
exec(compile(_src, "test_two_brains.py", "exec"))   # noqa: S102 — our own test file

import close_guard  # noqa: E402
import task_router  # noqa: E402

RESULTS = []


def report(n, name, ok, detail=""):
    RESULTS.append((n, name, ok, detail))
    check(f"#{n} {name}", ok, detail)


def said(app):
    return [x for x in app.tts.spoken if x]


async def wait_for(cond, timeout=2.0):
    t0 = time.perf_counter()
    while not cond():
        if time.perf_counter() - t0 > timeout:
            return None
        await asyncio.sleep(0.001)
    return time.perf_counter() - t0


async def main():
    g.live_view.note = lambda *a, **k: "[live desktop] front: Explorer"
    g.self_update.take_notice = lambda: ""

    async def _no_plan():
        return None
    g.GoatApp._pull_plan_usage = lambda self: _no_plan()
    handoffs = set(task_router.HANDOFF_LINES["en"])

    # ---- 1. chat: direct answer, no handoff ---------------------------------
    t1 = FakeTalker(lambda text: (["Sir, four."], []))
    app = make_app(t1)
    await app._talk("What's 2+2")
    await app._front_task
    await settle()
    report(1, "chat answered by the voice, nothing sent to Claude",
           said(app) == ["Sir, four."] and not app.client.queries
           and len(t1.seen) == 1, (app.tts.spoken, app.client.queries))

    # ---- 2. big task: instant handoff, one line, result announced -----------
    def script2(text):
        if "[event] Claude FINISHED" in text:
            return (["Sir, Claude's done: the biggest is a 4 GB Windows ISO, then two "
                     "installers."], [])
        if "[event] Claude asks Giorgi" in text:
            return (["Sir, Claude needs you: should it include hidden files?"], [])
        if "[event] The watch thinks Claude may be stuck" in text:
            return (["Sir, Claude seems stuck listing a locked folder — keep going or stop?"], [])
        if "Giorgi: What's it doing?" in text:
            step = "sorting" if "Sort-Object Length" in text else "?"
            return ([f"Sir, it's {step} your Downloads by size."], [])
        return (["Sir, okay."], [])
    t2 = FakeTalker(script2)
    app = make_app(t2)
    t0 = time.perf_counter()
    await app._talk("Find my largest files in Downloads")
    t_said = time.perf_counter() - t0
    dt = await wait_for(lambda: app.client.queries)
    handoff_ms = (t_said + (dt or 9)) * 1000
    q = app.client.queries[-1] if app.client.queries else ""
    report(2, f"handoff to Claude in {handoff_ms:.1f} ms (< 300)",
           dt is not None and handoff_ms < 300 and "largest files in Downloads" in q,
           q[-200:])
    report(2, "one-line reply, voice model never asked to do it",
           len(said(app)) == 1 and said(app)[0] in handoffs and not t2.seen,
           (app.tts.spoken, len(t2.seen)))

    # ---- 3. mid-task status ---------------------------------------------------
    app._watch.on_event("work_tool", "PowerShell — Get-ChildItem ~/Downloads -Recurse")
    app._watch.on_event("work_tool", "PowerShell — Sort-Object Length -Descending | "
                                     "Select -First 10")
    await app._talk("What's it doing?")
    await app._front_task
    await settle()
    seen = t2.seen[-1] if t2.seen else ""
    report(3, "status answered from the live job's current step",
           "current step: PowerShell — Sort-Object Length" in seen
           and said(app)[-1] == "Sir, it's sorting your Downloads by size."
           and len(app.client.queries) == 1, said(app)[-1:])

    # ---- 4. needs his input / stuck --------------------------------------------
    app._watch.job["next_checkin"] = time.monotonic() + 999
    app._watch.note_from_worker("Should I include hidden files too?", kind="question")
    ev = app._watch.due(quiet=True, engaged_s=999, held=False)
    app._front_start("", "event", ev)
    await app._front_task
    report(4, "Claude's question reaches him through the voice",
           ev is not None and ev.get("nkind") == "question"
           and "hidden files" in said(app)[-1], said(app)[-1:])
    app._watch.job["last"] = time.monotonic() - tb.STUCK_QUIET_S - 5
    app._watch.job["stuck_at"] = 0.0
    ev = app._watch.due(quiet=True, engaged_s=999, held=False)
    app._front_start("", "event", ev)
    await app._front_task
    report(4, "a stalled job is announced as stuck, with why",
           ev is not None and ev["kind"] == "stuck" and "no new step" in ev["why"]
           and "stuck" in said(app)[-1], (ev, said(app)[-1:]))

    # ...and the result when it lands (test 2's last clause)
    app.client.script = [
        stream_text("Top 10 by size: Win11.iso 4.1 GB, ..."),
        AssistantMessage(content=[TextBlock("Top 10 by size: Win11.iso 4.1 GB, two "
                                            "installers at 900 MB each.")], model="m"),
        result(),
    ]
    await app._consume()
    ev = app._watch.due(quiet=True, engaged_s=999, held=False)
    app._front_start("", "event", ev)
    await app._front_task
    report(2, "result announced when Claude finishes",
           ev is not None and ev["kind"] == "done" and "4 GB" in said(app)[-1],
           said(app)[-1:])

    # ---- 5. cancel -----------------------------------------------------------------
    t5 = FakeTalker(lambda text: (["Sir, stopping it."], [("stop_worker", {})]))
    app = make_app(t5)
    await app._talk("Find my largest files in Downloads")
    await wait_for(lambda: app.client.queries)
    await settle()
    was_running = app._watch.running() and app.busy
    await app._talk("Cancel that")
    if app._front_task is not None:
        await app._front_task
    await settle()
    report(5, "'Cancel that' stops Claude's job and says so",
           was_running and app._watch.job["state"] == "stopped" and (not app.busy or app.client.interrupted)
           and said(app)[-1] and said(app)[-1] not in handoffs,
           (app._watch.job["state"], said(app)[-2:]))

    # ---- 6. destructive: confirmation first -------------------------------------------
    t6 = FakeTalker(lambda text: (["Sir, okay."], []))
    app = make_app(t6)
    await app._talk("Delete the test folder")
    await wait_for(lambda: app.client.queries)
    q = app.client.queries[-1] if app.client.queries else ""
    r = task_router.route("Delete the test folder")
    asked = []
    real_ask = close_guard.ask
    close_guard.ask = lambda reason, **kw: asked.append(reason)
    try:
        out = await g._close_guard_hook(
            {"tool_name": "PowerShell",
             "tool_input": {"command": "Remove-Item -Recurse -Force C:\\Users\\giorg\\test"}},
            "t1", None)
        free = await g._close_guard_hook(
            {"tool_name": "PowerShell",
             "tool_input": {"command": "Get-ChildItem C:\\Users\\giorg\\test"}}, "t2", None)
    finally:
        close_guard.ask = real_ask
    denied = out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    report(6, "delete: Claude is told to confirm, and the shell gate blocks until yes",
           r["needs_confirmation"] and "confirm" in q and denied and asked and free == {},
           (asked, out, free))

    # ---- 7. chat latency: router on vs off ------------------------------------------------
    async def chat_ms(router_on: bool) -> float:
        g.TASK_ROUTER = router_on
        got = []

        def s(text):
            got.append(time.perf_counter())
            return (["Sir, fine."], [])
        a = make_app(FakeTalker(s))
        t = time.perf_counter()
        await a._talk("how are you doing today")
        await a._front_task
        return (got[0] - t) * 1000 if got else 9e9
    on, off = [], []
    for _ in range(30):
        off.append(await chat_ms(False))
        on.append(await chat_ms(True))
    g.TASK_ROUTER = True
    m_on, m_off = statistics.median(on), statistics.median(off)
    per = statistics.median(task_router.route("how are you doing today")["ms"]
                            for _ in range(200))
    report(7, f"chat turn to voice model: {m_on:.2f} ms with router vs {m_off:.2f} ms "
              f"without (router itself {per:.3f} ms)",
           m_on - m_off < 2.0, (m_on, m_off))

    # ---- 8. claim audit in the log ---------------------------------------------------------------
    buf = io.StringIO()

    def liar(text):
        if len(t8.seen) == 1:
            return (["Sir, opening your downloads folder now."], [])
        return (["Sir, here it is."], [("open_path", {"path": "downloads"})])
    t8 = FakeTalker(liar)
    app = make_app(t8)
    real_exec = g.voice_hands.execute
    g.voice_hands.execute = lambda n, a: "opened"
    try:
        with contextlib.redirect_stdout(buf):
            await app._talk("show me my downloads folder")
            if app._front_task is not None:
                await app._front_task
            await settle()
    finally:
        g.voice_hands.execute = real_exec
    log = buf.getvalue()
    audit = [ln for ln in log.splitlines() if ln.startswith("[claim-audit]")]
    report(8, "every action claim is logged with the tools really called; the "
              "unbacked one is caught and made true",
           any("UNBACKED" in a for a in audit) and len(t8.seen) >= 2, audit)

    print("\n==== acceptance summary ====")
    for n in range(1, 9):
        rows = [r for r in RESULTS if r[0] == n]
        ok = rows and all(r[2] for r in rows)
        print(f"  {n}. {'PASS' if ok else 'FAIL'} — " + "; ".join(r[1] for r in rows))
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
