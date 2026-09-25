"""Live probe: a real screenshot through the brain must not kill the reader.

2026-09-23 the first screenshot after the one-brain switch crashed the SDK
reader ("JSON message exceeded maximum buffer size of 1048576 bytes"). This
runs the brain with GOAT's screen tools and the raised buffer, asks for a
screenshot, and checks the turn completes with an answer.
    py -3.13 probe_screenshot.py
"""
import asyncio
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient,  # noqa: E402
                              ResultMessage, AssistantMessage, TextBlock,
                              ToolUseBlock)

import goat_app as g  # noqa: E402
import screen_tools  # noqa: E402


async def main():
    opts = ClaudeAgentOptions(
        cwd=g.WORKSPACE, permission_mode="bypassPermissions",
        model=g.MODEL_FULL, effort="high",
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": g.PERSONA},
        setting_sources=[], mcp_servers={"screen": screen_tools.SERVER},
        max_buffer_size=64 * 1024 * 1024)
    client = ClaudeSDKClient(opts)
    await client.connect()
    t0 = time.monotonic()
    tools, text, ok = [], "", False
    try:
        await client.query("Take ONE full screenshot with the computer tool "
                           "(action screenshot, do not hide or move anything) "
                           "and tell me in one sentence what app fills most of "
                           "the screen.")
        async for m in client.receive_response():
            if isinstance(m, AssistantMessage):
                for b in m.content:
                    if isinstance(b, ToolUseBlock):
                        tools.append(b.name)
                    elif isinstance(b, TextBlock):
                        text += b.text
            elif isinstance(m, ResultMessage):
                ok = not m.is_error
    finally:
        await client.disconnect()
    shot = any(t.endswith("computer") for t in tools)
    print(f"tools={tools}\nanswer={text.strip()}\n"
          f"{time.monotonic() - t0:.1f}s")
    print("RESULT:", "PASS" if ok and shot and text.strip() else "FAIL")


asyncio.run(main())
