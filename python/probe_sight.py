"""Live probe: does the ONE brain answer "what's on my screen" from sight?

Same options GOAT's brain runs with (persona, model, effort, tools), a
throwaway session, one Georgian question and one English one. Costs a little
Claude usage. Prints the answer and the time to first word.
    py -3.13 probe_sight.py
"""
import asyncio
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient,  # noqa: E402
                              StreamEvent, ResultMessage)

import goat_app as g  # noqa: E402
import live_view  # noqa: E402


async def ask(client, text, lang):
    view = live_view.note()
    send = view + "\n\n" + text
    if lang == "ka":
        send = g.KA_WORK_NOTE + send
    t0 = time.monotonic()
    first = None
    out = ""
    await client.query(send)
    async for m in client.receive_response():
        if isinstance(m, StreamEvent):
            d = (m.event or {}).get("delta", {}) or {}
            if d.get("type") == "text_delta" and d.get("text"):
                first = first or time.monotonic() - t0
                out += d["text"]
        elif isinstance(m, ResultMessage):
            break
    print(f"\nQ: {text}\n   first word {first:.1f}s, total "
          f"{time.monotonic() - t0:.1f}s\nA: {out.strip()}")
    return out


async def main():
    live_view.prime()
    await asyncio.sleep(0.5)
    opts = ClaudeAgentOptions(
        cwd=g.WORKSPACE, permission_mode="bypassPermissions",
        model=g.MODEL_FULL, effort="high", thinking=g.THINKING_CFG,
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": g.PERSONA + g.LANG_NOTE_AUTO},
        include_partial_messages=True, setting_sources=[])
    client = ClaudeSDKClient(opts)
    await client.connect()
    try:
        a1 = await ask(client, "რა მაქვს ახლა ეკრანზე?", "ka")
        a2 = await ask(client, "What app is using the most CPU right now?",
                       "en")
    finally:
        await client.disconnect()
    bad = ("can't see", "cannot see", "ვერ ვხედავ", "სამუშაო მხარ",
           "working side")
    blind = [a for a in (a1, a2) if any(b in a.lower() for b in bad)]
    print("\nRESULT:", "BLIND ANSWER FOUND" if blind else "sees the desktop")


asyncio.run(main())
