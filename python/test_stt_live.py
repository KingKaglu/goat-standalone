"""stt_live offline: one Live session carries several sentences, a dead kept
session is retried fresh, and a quota refusal rests the ear instead of
costing every turn. A fake Live server, no network, no sound.
Run: py -3.13 test_stt_live.py
"""
from __future__ import annotations

import asyncio
import json
import sys

import numpy as np

import stt_live as sl

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


class FakeWS:
    """Live with audio in: on activityEnd it sends the transcript for THIS
    utterance, then its own reply, then turnComplete."""
    made = 0

    def __init__(self, words, die_after=None):
        FakeWS.made += 1
        self.words = list(words)
        self.q: asyncio.Queue = asyncio.Queue()
        self.close_code = None
        self.turns = 0
        self.die_after = die_after

    async def send(self, raw):
        m = json.loads(raw)
        if (m.get("realtimeInput") or {}).get("activityEnd") is not None:
            self.turns += 1
            if self.die_after is not None and self.turns > self.die_after:
                self.close_code = 1011
                raise ConnectionError("session closed")
            w = self.words.pop(0)
            for part in (w[:4], w[4:]):
                await self.q.put({"serverContent": {"inputTranscription": {"text": part}}})
            await self.q.put({"serverContent": {"modelTurn": {"parts": [{"inlineData": {}}]}}})
            await self.q.put({"serverContent": {"turnComplete": True}})

    async def recv(self):
        return json.dumps(await self.q.get())

    async def close(self):
        self.close_code = 1000


said = ["კარგი, მადლობა.", "რომელი საათია?", "ეხლა რა დროა?", "გამარჯობა"]
sessions = []


async def fake_dial(key):
    ws = FakeWS(said[len(sessions):] + said, die_after=None)
    sessions.append(ws)
    return ws

sl._dial = fake_dial
sl.talk_brain.api_key = lambda: "k"
audio = np.zeros(16000, dtype=np.float32)

got = [sl.transcribe(audio) for _ in range(3)]
check("three sentences, each its own words", got == said[:3], got)
check("...through ONE Live session (the free quota counts sessions)",
      len(sessions) == 1, len(sessions))

# the kept session dies: the sentence is retried on a fresh one, not lost
sessions[0].die_after = sessions[0].turns
r = sl.transcribe(audio)
check("a kept session that died: retried fresh, the words still come",
      r == said[1] and len(sessions) == 2, (r, len(sessions)))

# Google refuses: the ear rests, no dial per turn, Gladia takes over
sl._reuse.clear()
dials = []


async def refusing(key):
    dials.append(1)
    raise ConnectionError("received 1011 (internal error) You exceeded your current quota")

sl._dial = refusing
check("a quota refusal returns None (Gladia takes the turn)", sl.transcribe(audio) is None)
check("...and rests the ear", sl.resting() > 50, sl.resting())
for _ in range(3):
    sl.transcribe(audio)
check("resting: no doomed dial on the next turns", len(dials) == 1, len(dials))
first = sl._rest["next"]
sl._rest["until"] = 0
sl.transcribe(audio)
check("refused again: the rest doubles", sl._rest["next"] == min(sl._REST_MAX, first * 2),
      sl._rest)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
