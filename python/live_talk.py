"""GOAT's voice on Gemini Live: one open session that hears his words and
answers in its own voice, ~0.5 s after the text reaches it.

Why (2026-10-25, his order: "the talking should feel more human to human —
when I am done giving the task I have the answer immediately"). The REST
voice measured 5-9 s from his last word to GOAT's first one that morning:
endpoint 0.5 s + ear 1.0-1.5 s + Gemini's first token 1.5-6 s + edge-tts's
first clause. Gemini Live on his key, same day: first audio 0.47-0.64 s after
a text turn, tool calls in 0.47 s, a new turn interrupting the one still
playing — and his Georgian recording heard as "გამარჯობა, გიორგი. ეს ჩემი
ახალი ხმაა." and answered in Georgian. It is also free, where the REST
voice's free tier is 20 requests a day per model and ran out by 11:10.

GOAT's own ears, gates and hands stay exactly as they were: the turn's TEXT
(the same message talk_brain would get — clock, desktop, Claude's job, his
words) goes into the session, and the session's audio goes into GOAT's own
player, so the word-by-word reveal, the phone, barge-in and the latency
ledger all keep working. Function calls are the same TOOLS. When Live can't
answer, the app falls back to the REST voice (and that to Claude), as before.

GOAT_LIVE=off brings back the REST voice. GOAT_LIVE_MODEL / GOAT_LIVE_VOICE
override the model and the voice.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time

import numpy as np

import dsp
import talk_brain

ENABLED = os.environ.get("GOAT_LIVE", "on").strip().lower() not in ("off", "0", "no")
MODEL = os.environ.get("GOAT_LIVE_MODEL", "gemini-3.8-live").strip()
# Close to the edge voices he knows: Ava (goat) is light and female, Andrew
# (ultron) low and male.
VOICES = {"goat": "Kore", "ultron": "Charon"}
URL = ("wss://generativelanguage.googleapis.com/ws/"
       "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
CONNECT_S = 6.0            # socket + setupComplete
FIRST_S = 6.0              # nothing at all by now: Live is down this turn
IDLE_S = 8.0               # a started answer silent this long is over
DOWN_S = 120.0             # after a failure, the REST voice answers this long
SEED_TURNS = 12            # history messages replayed into a fresh session
OUT_RATE = 24000           # Live speaks 24 kHz PCM16; GOAT plays 16 kHz


class Resampler:
    """24 kHz -> 16 kHz in pieces, without clicks at the joins: each call
    keeps CTX input samples either side so the filter never sees an edge
    inside the audio (2 ms of delay)."""

    CTX = 48                  # input samples; a multiple of 3

    def __init__(self):
        self._hist = np.zeros(2 * self.CTX)   # warm start: silence
        self._spare = np.zeros(0)             # samples short of a multiple of 3

    def feed(self, x: np.ndarray) -> np.ndarray:
        x = np.concatenate([self._spare, x.astype(np.float64)])
        cut = len(x) - len(x) % 3
        x, self._spare = x[:cut], x[cut:]
        if not len(x):
            return np.zeros(0, dtype=np.float32)
        buf = np.concatenate([self._hist, x])
        y = dsp.resample_poly(buf, 2, 3)
        a, b = (len(self._hist) - self.CTX) * 2 // 3, (len(buf) - self.CTX) * 2 // 3
        self._hist = buf[-2 * self.CTX:]
        return y[a:b].astype(np.float32)

    def flush(self) -> np.ndarray:
        tail = self.feed(np.zeros(self.CTX + (3 - len(self._spare) % 3) % 3))
        self.__init__()
        return tail


def _pcm(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype="<i2").astype(np.float32) / 32768.0


class LiveTalk:
    """One Gemini Live session, reconnected when it drops (Google closes a
    connection after ~10 min; resumption keeps the conversation)."""

    def __init__(self, talker: talk_brain.GeminiTalk):
        self.talker = talker          # history + key + prompt live there
        self.key = talker.key
        self.model = MODEL
        self.ws = None
        self._reader = None
        self._handle = None           # session resumption token
        self._sink = None             # the turn currently being answered
        self._generating = False
        self._stale = False           # drop the rest of a turn he cut off
        self._lock = asyncio.Lock()
        self._down_until = 0.0
        self.last_error = ""
        self.fail_streak = 0
        self.ttfts: list = []
        self._voice = ""              # the voice this session was opened with

    def label(self) -> str:
        return "Gemini Live"

    def usable(self) -> bool:
        return ENABLED and bool(self.key) and time.monotonic() >= self._down_until

    # ---- the session ----
    @staticmethod
    def _wanted_voice() -> str:
        import tts_edge
        return (os.environ.get("GOAT_LIVE_VOICE")
                or VOICES.get(getattr(tts_edge, "CHARACTER", "goat"), "Kore"))

    def _setup(self) -> dict:
        self.talker._refresh_system()
        voice = self._voice = self._wanted_voice()
        setup = {
            "model": f"models/{self.model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}}}},
            "systemInstruction": {"parts": [{"text": self.talker._system}]},
            "tools": talk_brain.TOOLS,
            "outputAudioTranscription": {},
            # text in, so no voice detection on Google's side: GOAT's own
            # (echo-proof) ears decide when he spoke
            "realtimeInputConfig": {"automaticActivityDetection": {"disabled": True}},
            "contextWindowCompression": {"slidingWindow": {}},
            "sessionResumption": {"handle": self._handle} if self._handle else {},
        }
        return {"setup": setup}

    async def _connect(self):
        import websockets
        ws = await asyncio.wait_for(websockets.connect(
            f"{URL}?key={self.key}", max_size=16 * 1024 * 1024,
            open_timeout=CONNECT_S), CONNECT_S)
        resumed = bool(self._handle)
        await ws.send(json.dumps(self._setup()))
        first = json.loads(await asyncio.wait_for(ws.recv(), CONNECT_S))
        if "setupComplete" not in first:
            await ws.close()
            raise RuntimeError(f"setup refused: {str(first)[:160]}")
        self.ws = ws
        self._generating = self._stale = False
        self._reader = asyncio.create_task(self._read(ws))
        if not resumed:
            await self._seed()
        print(f"[live] session {'resumed' if resumed else 'open'} on {self.model}",
              flush=True)

    async def _seed(self):
        """A fresh session knows nothing: replay the recent conversation."""
        turns = [{"role": m["role"], "parts": [{"text": m["text"]}]}
                 for m in list(self.talker.history)[-SEED_TURNS:]]
        while turns and turns[0]["role"] != "user":
            turns.pop(0)
        if turns:
            await self.ws.send(json.dumps({"clientContent": {
                "turns": turns, "turnComplete": False}}))

    async def _ensure(self):
        ws = self.ws
        if ws is not None and getattr(ws, "close_code", None) is None:
            if self._voice == self._wanted_voice():
                return
            # he switched character: a new session in the new voice
            self.ws, self._handle = None, None
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self.ws = None
        try:
            await self._connect()
        except Exception:
            if self._handle:          # a stale resumption token: start clean
                self._handle = None
                await self._connect()
            else:
                raise

    async def _read(self, ws):
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                upd = m.get("sessionResumptionUpdate")
                if upd and upd.get("resumable") and upd.get("newHandle"):
                    self._handle = upd["newHandle"]
                if "goAway" in m:
                    print("[live] Google is closing the session soon — will resume",
                          flush=True)
                sc = m.get("serverContent") or {}
                end = bool(sc.get("turnComplete") or sc.get("interrupted"))
                if self._stale:
                    if end:
                        self._stale = False
                    if "toolCall" in m:
                        await self._answer_calls(ws, m["toolCall"], None)
                    continue
                sink = self._sink
                if "toolCall" in m:
                    await self._answer_calls(ws, m["toolCall"], sink)
                if sink is not None and sc:
                    sink.server(sc)
                if end:
                    self._generating = False
                    if sink is not None:
                        sink.done.set()
        except Exception as e:  # noqa: BLE001 — the next turn reconnects
            self.last_error = f"{type(e).__name__}: {e}"[:160]
        finally:
            if self.ws is ws:
                self.ws = None
            s = self._sink
            if s is not None:
                s.lost = True
                s.done.set()

    async def _answer_calls(self, ws, tc: dict, sink):
        responses = []
        for fc in tc.get("functionCalls") or []:
            if sink is not None:
                sink.call(fc.get("name", ""), fc.get("args") or {})
            # Answered at once and SILENT: GOAT runs the hands itself and
            # reports back through its own lines. A voice that spoke again
            # after the answer would do it after turnComplete, into nobody's
            # turn — or into his next one.
            responses.append({"id": fc.get("id"), "name": fc.get("name"),
                              "response": {"result": "started",
                                           "scheduling": "SILENT"}})
        if responses:
            try:
                await ws.send(json.dumps({"toolResponse": {"functionResponses": responses}}))
            except Exception:  # noqa: BLE001
                pass

    async def warm(self):
        """Open the session ahead of the turn (he just started talking), so
        the turn pays ~0.5 s, not the ~1 s of a socket and setup."""
        if not self.usable() or self._lock.locked():
            return
        ws = self.ws
        if ws is not None and getattr(ws, "close_code", None) is None:
            return
        async with self._lock:
            try:
                await self._ensure()
            except Exception as e:  # noqa: BLE001 — the turn will say so
                self.last_error = f"warm: {type(e).__name__}: {e}"[:160]

    # ---- one turn ----
    async def reply(self, user_parts: list, on_text, on_call, on_audio) -> dict:
        """Same contract as GeminiTalk.reply, plus on_audio(float32 16 kHz).
        Returns {"ok", "model", "error", "ttft"}."""
        if not self.usable():
            return {"ok": False, "model": "", "error": "Live resting", "ttft": None}
        async with self._lock:
            t0 = time.monotonic()
            try:
                await self._ensure()
            except Exception as e:  # noqa: BLE001
                return self._fail(f"connect: {type(e).__name__}: {e}")
            sink = _Turn(on_text, on_call, on_audio)
            if self._generating:
                # the last answer is still coming: this turn cuts it off
                # (Google sends "interrupted" for it first)
                self._stale = True
            self._sink = sink
            parts = [p for p in user_parts if p.get("text") or p.get("inlineData")]
            try:
                # generating from the moment it is sent: a cut-in that lands
                # during the send must still drop this turn's leftovers
                self._generating = True
                await self.ws.send(json.dumps({"clientContent": {
                    "turns": [{"role": "user", "parts": parts}], "turnComplete": True}}))
                while True:
                    wait = IDLE_S if sink.t_first else max(0.05, t0 + FIRST_S - time.monotonic())
                    try:
                        await asyncio.wait_for(sink.done.wait(), wait)
                    except asyncio.TimeoutError:
                        if sink.t_first and time.monotonic() - sink.t_last < IDLE_S:
                            continue
                        if not sink.t_first:
                            self._stale = self._generating
                            return self._fail(f"no answer in {FIRST_S:.0f}s")
                        break
                    break
            except asyncio.CancelledError:
                # His words cut this turn off: whatever is still coming for
                # it is dropped, and his next turn interrupts it on Google's side.
                self._stale = self._generating
                raise
            except Exception as e:  # noqa: BLE001
                return self._fail(f"{type(e).__name__}: {e}")
            finally:
                if self._sink is sink:
                    self._sink = None
                sink.finish()
            if sink.lost and not sink.t_first:
                return self._fail(self.last_error or "session closed")
            ttft = (sink.t_first - t0) if sink.t_first else None
            if ttft is not None:
                self.ttfts.append(ttft)
                self.ttfts = self.ttfts[-20:]
            self.fail_streak = 0
            return {"ok": True, "model": self.model, "error": "", "ttft": ttft}

    def _fail(self, why: str) -> dict:
        self.fail_streak += 1
        self.last_error = why
        if self.fail_streak >= 2:
            self._down_until = time.monotonic() + DOWN_S
            print(f"[live] resting {DOWN_S:.0f}s after: {why}", flush=True)
        ws, self.ws = self.ws, None
        if ws is not None:
            asyncio.ensure_future(ws.close())
        return {"ok": False, "model": "", "error": why, "ttft": None}


class _Turn:
    def __init__(self, on_text, on_call, on_audio):
        self.on_text, self.on_call, self.on_audio = on_text, on_call, on_audio
        self.done = asyncio.Event()
        self.t_first = 0.0
        self.t_last = 0.0
        self.lost = False
        self._rs = Resampler()

    def _mark(self):
        now = time.monotonic()
        self.t_first = self.t_first or now
        self.t_last = now

    def server(self, sc: dict):
        for p in (sc.get("modelTurn") or {}).get("parts") or []:
            data = (p.get("inlineData") or {}).get("data")
            if data:
                self._mark()
                pcm = self._rs.feed(_pcm(data))
                if len(pcm):
                    self.on_audio(pcm)
        text = (sc.get("outputTranscription") or {}).get("text")
        if text:
            self._mark()
            self.on_text(text)

    def call(self, name: str, args: dict):
        self._mark()
        self.on_call(name, args)

    def finish(self):
        tail = self._rs.flush()
        if self.t_first and len(tail):
            self.on_audio(tail)
