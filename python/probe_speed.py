"""Live probe: where does the time go between "he stopped talking" and
"GOAT's first REAL word"?

The ledger in goat-app.log measured the backchannel ("Mm-hm" at 0.9s, from
the TTS cache) as first sound, so the real answer's latency was invisible.
This runs GOAT's own brain options (persona, model, tools, thinking) on a
throwaway session and times, per turn:
    first thinking delta / first text delta / first speakable clause
and then synthesises that clause with GOAT's voice to time the TTS leg.
Costs a little Claude usage.
    py -3.13 probe_speed.py [effort ...]
"""
import asyncio
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient,  # noqa: E402
                              ResultMessage)
from claude_agent_sdk.types import StreamEvent  # noqa: E402

import goat_app as g  # noqa: E402
import live_view  # noqa: E402
import screen_tools  # noqa: E402
import tts_edge  # noqa: E402
import os  # noqa: E402
EXTRA = os.environ.get("PROBE_APPEND", "")

TURNS = [
    ("ka", "გამარჯობა, როგორ ხარ?"),
    ("en", "What can we do now?"),
    ("ka", "რა მაქვს ახლა ეკრანზე?"),
    ("en", "What's your name?"),
]


def options(effort, thinking=True):
    return ClaudeAgentOptions(
        cwd=g.WORKSPACE, permission_mode="bypassPermissions",
        model=g.MODEL_FULL, effort=effort,
        thinking=(dict(g.THINKING_CFG, display=os.environ["PROBE_DISPLAY"]) if os.environ.get("PROBE_DISPLAY") else g.THINKING_CFG) if thinking else {"type": "disabled"},
        fallback_model=g.MODEL_PREV,
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": g.PERSONA + g.LANG_NOTE_AUTO + EXTRA},
        include_partial_messages=True, setting_sources=["project"],
        mcp_servers={"screen": screen_tools.SERVER},
        max_buffer_size=64 * 1024 * 1024)


async def turn(client, lang, text):
    note = live_view.note()
    send = note + "\n\n" + text
    if lang == "ka":
        send = g.KA_WORK_NOTE + "\n" + send
    t0 = time.monotonic()
    t_think = t_text = t_clause = None
    buf = ""
    clause = ""
    await client.query(send)
    async for m in client.receive_messages():
        now = time.monotonic() - t0
        if isinstance(m, StreamEvent):
            d = m.event.get("delta", {})
            if d.get("type") == "thinking_delta" and t_think is None:
                t_think = now
            if d.get("type") == "text_delta":
                if t_text is None:
                    t_text = now
                buf += d.get("text", "")
                if t_clause is None:
                    mm = g.SENTENCE_RE.match(buf)
                    mc = g.FIRST_CLAUSE_RE.match(buf)
                    if mm and mm.group(1).strip():
                        t_clause, clause = now, mm.group(1)
                    elif mc and len(mc.group(1).strip()) >= g.FIRST_CLAUSE_MIN:
                        t_clause, clause = now, mc.group(1)
        elif isinstance(m, ResultMessage):
            total = now
            break
    if t_clause is None and buf.strip():
        t_clause, clause = total, buf
    tts_edge.set_language(lang)
    s = time.monotonic()
    try:
        await asyncio.to_thread(tts_edge.synth, clause.strip())
        t_tts = time.monotonic() - s
    except Exception as e:  # noqa: BLE001
        t_tts = float("nan")
        print("  tts failed:", e)
    f = lambda x: f"{x:5.2f}" if x is not None else "  -  "  # noqa: E731
    print(f"  think {f(t_think)}  text {f(t_text)}  clause {f(t_clause)}  "
          f"+tts {t_tts:4.2f} = heard {f((t_clause or 0) + t_tts)}  "
          f"done {total:5.2f}  | {clause.strip()[:60]!r}")


async def main(efforts):
    for eff in efforts:
        thinking = not eff.endswith("-nothink")
        e = eff.replace("-nothink", "")
        print(f"== effort {e}  thinking={'adaptive' if thinking else 'off'}")
        t = time.monotonic()
        async with ClaudeSDKClient(options(e, thinking)) as c:
            print(f"  connect {time.monotonic() - t:.2f}s")
            if os.environ.get("PROBE_WARM"):
                t = time.monotonic()
                await c.query("[warm-up] You just (re)connected. This is not "
                              "from Giorgi — reply with only: OK")
                async for m in c.receive_messages():
                    if isinstance(m, ResultMessage):
                        break
                print(f"  warm-up {time.monotonic() - t:.2f}s (muted in GOAT)")
            for lang, text in TURNS:
                await turn(c, lang, text)


asyncio.run(main(sys.argv[1:] or ["high", "low"]))
