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
from collections import deque

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
# After a function call Google closes the turn (turnComplete) and THEN
# speaks a follow-up as a generation of its own ("Sir, opening the demo page
# now."), ~0.5 s later, even with the response marked SILENT - traced on his
# key 2026-10-25. The turn waits this long for it, so the follow-up is
# voiced as part of it instead of leaking into his next turn (it came out as
# a lone "now." there).
TOOL_TAIL_S = 2.0
TOOL_RUN_S = 30.0          # one of the voice's own hands may take this long
DOWN_S = 120.0             # after a failure, the REST voice answers this long
SEED_TURNS = 12            # history messages replayed into a fresh session
OUT_RATE = 24000           # Live speaks 24 kHz PCM16; GOAT plays 16 kHz
# Live thinks before it speaks unless told not to: with GOAT's full prompt
# (persona + 14k of memory) its first sound came 2.0-3.1 s after the text;
# with thinkingBudget 0, 0.37-0.47 s — and it still routed every test order
# right (notepad -> do_now, builds/folders -> Claude, email -> email, chat
# answered itself). Measured 2026-10-25 on his key. ("thinkingLevel" is
# refused by this model.)
THINK_BUDGET = int(os.environ.get("GOAT_LIVE_THINK", "0"))


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
        # Which turn Google is talking about. It closes turns strictly in
        # order, each with a turnComplete that trails the last sound by 2-4 s
        # (generationComplete marks the end of the sound), and a turn sent
        # while an older one is still open gets that one closed first
        # ("interrupted", turnComplete) before its own answer starts. A
        # function call adds one more: Google closes the call's turn, then
        # speaks a follow-up as a generation of its own. So every message
        # belongs to the oldest entry still waiting for its turnComplete.
        # 2026-10-25, traced on his key: a yes/no "drop the old turn" flag
        # let an older turnComplete end a newer turn — silence ("Why have
        # you stopped talking?") — and a call's follow-up leaked into his
        # next answer as a lone "now.".
        self._q: deque = deque()      # [sink, "turn" | "follow"], oldest first
        # async (name, args) -> str | None. A string is the real result of
        # one of the voice's own hands (voice_hands: files, phone, terminal
        # Claude) and goes back to Google, which then SAYS it. None = an
        # action GOAT runs on its own ("started").
        self.tool_runner = None
        self.dropped = 0              # late pieces that belonged to no turn
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
                    "prebuiltVoiceConfig": {"voiceName": voice}}},
                "thinkingConfig": {"thinkingBudget": THINK_BUDGET}},
            "systemInstruction": {"parts": [{"text": self.talker._system}]},
            # (No googleSearch: on his free tier any Live session that
            # includes it is refused, "You exceeded your current quota" —
            # measured 2026-10-25. The voice's web_search hand does it.)
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
        self._q.clear()
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
                    print("[live] Google is closing the session soon - will resume",
                          flush=True)
                entry = self._q[0] if self._q else None
                sink = entry[0] if entry else None
                if "toolCall" in m:
                    await self._answer_calls(ws, m["toolCall"], entry)
                sc = m.get("serverContent") or {}
                if not sc:
                    continue
                content = bool((sc.get("modelTurn") or {}).get("parts")
                               or (sc.get("outputTranscription") or {}).get("text"))
                live = sink is not None and sink.active
                if content:
                    if live:
                        sink.server(sc)
                    else:
                        self.dropped += 1     # a turn he cut off: never voiced
                if live and (sc.get("generationComplete") or sc.get("interrupted")):
                    sink.gen_done()
                if sc.get("turnComplete") and entry is not None:
                    self._q.popleft()
                    if live:
                        sink.closed()
        except Exception as e:  # noqa: BLE001 - the next turn reconnects
            self.last_error = f"{type(e).__name__}: {e}"[:160]
        finally:
            if self.ws is ws:
                self.ws = None
            for sk, _ in list(self._q):
                sk.lost = True
                sk.ev.set()

    async def _answer_calls(self, ws, tc: dict, entry):
        """Every call is answered; Google then speaks a follow-up as a
        generation of its own, queued right behind the generation that
        called (it may call again: find, then read, then open)."""
        calls = tc.get("functionCalls") or []
        if not calls:
            return
        sink = entry[0] if entry else None
        live = sink is not None and sink.active
        if entry is not None:
            self._q.insert(self._q.index(entry) + 1, [sink, "follow"])
        if live:
            for fc in calls:
                sink.call(fc.get("name", ""), fc.get("args") or {})
            sink.pending += 1
        asyncio.create_task(self._respond(ws, calls, sink if live else None))

    async def _respond(self, ws, calls: list, sink):
        async def one(fc):
            name, args = fc.get("name", ""), fc.get("args") or {}
            result = None
            if sink is not None and self.tool_runner is not None:
                try:
                    result = await self.tool_runner(name, args)
                except Exception as e:  # noqa: BLE001
                    result = f"ERROR: {type(e).__name__}: {e}"
            if result is None:
                # an action GOAT runs itself and reports on in its own lines
                resp = {"result": "started", "scheduling": "SILENT"}
            else:
                resp = {"result": str(result)[:8000]}
            return {"id": fc.get("id"), "name": name, "response": resp}
        responses = await asyncio.gather(*(one(fc) for fc in calls))
        try:
            await ws.send(json.dumps({"toolResponse": {"functionResponses": responses}}))
        except Exception:  # noqa: BLE001
            pass
        if sink is not None:
            sink.answered()

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
            parts = [p for p in user_parts if p.get("text") or p.get("inlineData")]
            try:
                # queued before the send: a cut-in that lands during the send
                # still finds this turn's place in line
                self._q.append([sink, "turn"])
                await self.ws.send(json.dumps({"clientContent": {
                    "turns": [{"role": "user", "parts": parts}], "turnComplete": True}}))
                while not sink.over():
                    now = time.monotonic()
                    if not sink.t_first:
                        if now >= t0 + FIRST_S:
                            return self._fail(f"no answer in {FIRST_S:.0f}s")
                        wait = t0 + FIRST_S - now
                    elif sink.pending:
                        if now - sink.t_last >= TOOL_RUN_S:
                            break                 # a hand that never came back
                        wait = TOOL_RUN_S
                    elif now - sink.t_last >= IDLE_S:
                        break
                    elif sink.waiting_follow():
                        left = TOOL_TAIL_S - (now - sink.tail_from())
                        if left <= 0:
                            # no follow-up this time: give up its place(s)
                            self._q = deque(e for e in self._q
                                            if not (e[0] is sink and e[1] == "follow"))
                            break
                        wait = left
                    else:
                        wait = IDLE_S
                    sink.ev.clear()
                    try:
                        await asyncio.wait_for(sink.ev.wait(), max(0.05, wait))
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                # His words cut this turn off. It keeps its place in line, so
                # whatever Google still sends for it is recognised and dropped,
                # and his next turn makes Google close it.
                raise
            except Exception as e:  # noqa: BLE001
                return self._fail(f"{type(e).__name__}: {e}")
            finally:
                sink.active = False
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
        self.ev = asyncio.Event()     # something happened: re-check the end
        self.active = True            # False once reply() has let go of it
        self.t_first = 0.0            # first anything (sound, words or a call)
        self.t_last = 0.0
        self.called = False
        self.pending = 0              # tool calls not answered yet
        self.resp_t = 0.0             # when the last tool answer went back
        self.spoke_since = False      # sound/words since the last answer
        self.sound_done = False       # that generation's sound is over
        self.closed_t = 0.0           # Google's last turnComplete for it
        self.lost = False
        self._rs = Resampler()

    def _mark(self, content: bool = True):
        now = time.monotonic()
        self.t_first = self.t_first or now
        self.t_last = now
        if content:
            self.spoke_since = True
        self.ev.set()

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
        self._mark(content=False)
        self.called = True
        self.on_call(name, args)

    def answered(self):
        self.pending = max(0, self.pending - 1)
        self.resp_t = time.monotonic()
        self.spoke_since = False      # what counts now is the follow-up
        self.sound_done = False
        self.t_last = self.resp_t
        self.ev.set()

    def gen_done(self):
        if self.spoke_since:
            self.sound_done = True
        self.ev.set()

    def closed(self):
        self.closed_t = time.monotonic()
        self.ev.set()

    def tail_from(self) -> float:
        return max(self.resp_t, self.closed_t)

    def waiting_follow(self) -> bool:
        """A call was answered and its spoken follow-up hasn't started."""
        return self.called and not self.pending and not self.spoke_since

    def over(self) -> bool:
        if self.lost:
            return True
        if not self.t_first or self.pending:
            return False
        if self.called:
            return self.spoke_since and self.sound_done
        return self.sound_done or bool(self.closed_t)

    def finish(self):
        tail = self._rs.flush()
        if self.t_first and len(tail):
            self.on_audio(tail)
