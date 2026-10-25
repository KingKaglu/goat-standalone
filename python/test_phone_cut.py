"""The phone's "cut" (orb tap / Stop button) with two brains: an orb tap
hushes the voice and never touches Claude's job; only the Stop button stops
the job. 2026-10-25 22:1x-22:2x: orb taps cut three jobs silently and the
voice told him "you stopped it".
Run: py -3.13 test_phone_cut.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import types

import phone_server

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


class FakeApp:
    def __init__(self, two_brains: bool, busy: bool):
        self.two_brains = two_brains
        self.busy = busy
        self._say_buf = "x"
        self.calls = []
        self.tts = types.SimpleNamespace(cancel=lambda: self.calls.append("tts.cancel"))

    def _front_cancel(self):
        self.calls.append("front_cancel")

    async def _stop_worker(self):
        self.calls.append("stop_worker")

    async def _cut_turn(self, then):
        self.calls.append("cut_turn")


async def cut(app, msg: dict):
    srv = object.__new__(phone_server.PhoneServer)
    srv.app = app
    srv._command({}, json.dumps(msg))
    await asyncio.sleep(0)       # let a created task run
    return app.calls


async def main():
    c = await cut(FakeApp(True, True), {"type": "cut", "what": "voice"})
    check("orb tap while Claude works: the voice stops",
          "tts.cancel" in c and "front_cancel" in c, c)
    check("orb tap while Claude works: Claude's job keeps running",
          "stop_worker" not in c and "cut_turn" not in c, c)
    c = await cut(FakeApp(True, True), {"type": "cut"})
    check("an old page's bare cut never stops the job",
          "stop_worker" not in c and "cut_turn" not in c, c)
    c = await cut(FakeApp(True, True), {"type": "cut", "what": "job"})
    check("Stop button while Claude works: stop_worker (logged, voice told)",
          "stop_worker" in c and "cut_turn" not in c, c)
    c = await cut(FakeApp(True, False), {"type": "cut", "what": "job"})
    check("Stop button with Claude idle: nothing to stop", "stop_worker" not in c, c)
    c = await cut(FakeApp(False, True), {"type": "cut", "what": "voice"})
    check("one brain: a cut still ends the speaking turn", "cut_turn" in c, c)


asyncio.run(main())
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
