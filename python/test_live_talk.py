"""live_talk.py offline: the resampler joins pieces without clicks, and one
Live session routes audio, transcript and tool calls to the right turn —
including a turn he cut off, whose leftovers must never leak into the next.

A fake websocket stands in for Google. `--live` also runs one real turn on
his key (free tier). Run: py -3.13 test_live_talk.py [--live]
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import types
from collections import deque

import numpy as np

import dsp
import live_talk
import tts_edge  # noqa: F401 — GOAT has it loaded; a cold import would stall the loop

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


# ---- 1. resampler -----------------------------------------------------------
t = np.arange(24000 * 2) / 24000.0
x = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
whole = dsp.resample_poly(x, 2, 3)
rs = live_talk.Resampler()
rng = np.random.default_rng(3)
out, i = [], 0
while i < len(x):
    n = int(rng.integers(1, 2400))
    out.append(rs.feed(x[i:i + n]))
    i += n
out.append(rs.flush())
y = np.concatenate(out)
lag = 32                                        # its CTX*2/3 delay (2 ms)
check("24 kHz -> 16 kHz in random pieces keeps the length",
      abs(len(y) - lag - len(whole)) <= 2, (len(y), len(whole)))
mid = slice(200, len(whole) - 200)
err = float(np.max(np.abs(y[lag:][mid][:len(whole[mid])] - whole[mid][:len(y) - lag])))
check("...and is the same signal, no clicks at the joins", err < 1e-3, err)


# ---- 2. one session, several turns ------------------------------------------
def audio_msg(n=2400):
    pcm = (np.zeros(n) * 32767).astype("<i2").tobytes()
    return {"serverContent": {"modelTurn": {"parts": [
        {"inlineData": {"mimeType": "audio/pcm;rate=24000",
                        "data": base64.b64encode(pcm).decode()}}]}}}


def text_msg(s):
    return {"serverContent": {"outputTranscription": {"text": s}}}


# Google's real order, traced 2026-10-25: the sound ends with
# generationComplete; turnComplete trails it by 2-4 s; a function call's
# turn closes and THEN a spoken follow-up comes as a generation of its own;
# a turn sent while one is open gets "interrupted" + turnComplete for the
# old one before its own answer starts.
GEN = {"serverContent": {"generationComplete": True}}
END = {"serverContent": {"turnComplete": True}}
CUT = {"serverContent": {"interrupted": True}}


class FakeWS:
    """Plays a script per clientContent turn it receives."""

    def __init__(self, scripts):
        self.scripts = deque(scripts)
        self.sent = []
        self.close_code = None
        self.q: asyncio.Queue = asyncio.Queue()

    async def send(self, raw):
        m = json.loads(raw)
        self.sent.append(m)
        cc = m.get("clientContent")
        if cc and cc.get("turnComplete") and self.scripts:
            for item in self.scripts.popleft():
                await self.q.put(item)

    async def close(self):
        self.close_code = 1000
        await self.q.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.q.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, (int, float)):
            await asyncio.sleep(item)
            return json.dumps({})
        return json.dumps(item)


async def main():
    talker = types.SimpleNamespace(key="k", history=deque(maxlen=24),
                                   _system="sys", _refresh_system=lambda: None)
    lt = live_talk.LiveTalk(talker)
    ws = FakeWS([
        [text_msg("Sir, "), audio_msg(), text_msg("it's four."), audio_msg(), GEN, 0.6, END],
        [{"toolCall": {"functionCalls": [{"id": "c1", "name": "do_now",
                                           "args": {"command": "open notepad"}}]}}, GEN, END,
         0.3, text_msg("Sir, opening Notepad."), audio_msg(), GEN, 0.6, END],
        # a long answer he cuts off: its tail is still on the wire
        [text_msg("Once upon"), audio_msg(), 0.4, text_msg(" a time"), audio_msg()],
        # ...and his next turn: Google closes the old one first
        [CUT, END, text_msg("Nine."), audio_msg(), GEN, 0.6, END],
    ])

    async def fake_connect():
        lt.ws = ws
        lt._voice = lt._wanted_voice()   # as _setup would
        lt._reader = asyncio.create_task(lt._read(ws))
    lt._connect = fake_connect

    got = {"text": [], "audio": [], "calls": []}
    on_t = lambda s: got["text"].append(s)
    on_a = lambda a: got["audio"].append(len(a))
    on_c = lambda n, a: got["calls"].append((n, a))

    import time as _t
    t_a = _t.monotonic()
    res = await lt.reply([{"text": "Giorgi: two plus two"}], on_t, on_c, on_a)
    check("the turn ends when the sound does, not 2-4 s later at turnComplete",
          _t.monotonic() - t_a < 0.4, _t.monotonic() - t_a)
    check("a text turn comes back as transcript + 16 kHz audio",
          res["ok"] and "".join(got["text"]) == "Sir, it's four." and sum(got["audio"]) >= 3000,
          (res, got))
    check("first sound is timed", res["ttft"] is not None)

    got = {"text": [], "audio": [], "calls": []}
    res = await lt.reply([{"text": "Giorgi: open notepad"}], on_t, on_c, on_a)
    tr = [m for m in ws.sent if "toolResponse" in m]
    check("a tool call reaches GOAT's hands", got["calls"] == [("do_now", {"command": "open notepad"})],
          got["calls"])
    check("...and the spoken follow-up after it belongs to the same turn",
          "".join(got["text"]) == "Sir, opening Notepad." and got["audio"], got)
    check("...and is answered at once, SILENT",
          tr and tr[-1]["toolResponse"]["functionResponses"][0]["response"]["scheduling"] == "SILENT",
          tr[-1:])

    got = {"text": [], "audio": [], "calls": []}
    task = asyncio.create_task(lt.reply([{"text": "Giorgi: tell me a story"}], on_t, on_c, on_a))
    await asyncio.sleep(0.1)
    task.cancel()                      # his words cut it off
    try:
        await task
    except asyncio.CancelledError:
        pass
    story = list(got["text"])
    got = {"text": [], "audio": [], "calls": []}
    res = await lt.reply([{"text": "Giorgi: three times three"}], on_t, on_c, on_a)
    check("the cut-off story never leaks into the next answer",
          "".join(got["text"]) == "Nine." and " a time" not in story, (story, got["text"]))
    check("...and the old turn's 'interrupted' doesn't end the new one (no silence)",
          res["ok"] and got["audio"], (res, got))

    # a session that dies before answering: the turn fails, the app falls back
    lt2 = live_talk.LiveTalk(talker)
    async def no_connect():
        raise OSError("no network")
    lt2._connect = no_connect
    res = await lt2.reply([{"text": "Giorgi: hi"}], on_t, on_c, on_a)
    check("no connection -> ok False (the REST voice takes the turn)", not res["ok"], res)
    await lt2.reply([{"text": "Giorgi: hi"}], on_t, on_c, on_a)
    check("two failures in a row rest Live for a while", not lt2.usable())

    if "--live" in sys.argv:
        import talk_brain
        real = live_talk.LiveTalk(talk_brain.GeminiTalk())
        got = {"text": [], "audio": [], "calls": []}
        res = await real.reply([{"text": talk_brain.clock_note() + "\n\nGiorgi: what's two plus two?"}],
                               on_t, on_c, on_a)
        check("REAL Live turn on his key answers fast",
              res["ok"] and res["ttft"] is not None and res["ttft"] < 3.0 and sum(got["audio"]) > 8000,
              (res, "".join(got["text"])))
        print(f"[info] real: ttft {res['ttft']}, said {''.join(got['text'])!r}")

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


asyncio.run(main())
