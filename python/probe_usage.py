"""Probe: can the SDK report plan usage (5-hour / weekly) via the CLI's
get_usage control request? Connects, asks, prints, disconnects. Sends no
prompt, so it costs no tokens."""
import asyncio
import json
import sys

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient


async def main():
    c = ClaudeSDKClient(options=ClaudeAgentOptions(cwd="."))
    await c.connect()
    try:
        r = await asyncio.wait_for(
            c._query._send_control_request({"subtype": "get_usage"}), 10)
        print(json.dumps(r, indent=1, default=str)[:1500])
    except Exception as e:  # noqa: BLE001
        print("FAIL", type(e).__name__, str(e)[:300])
    finally:
        await c.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
