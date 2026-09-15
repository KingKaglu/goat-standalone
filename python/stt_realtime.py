"""Streaming ear — transcribe WHILE he is still talking.

His goal, 2026-09-15: "use streaming architectures ... target sub-500ms
latency". The batch ear could never get there, and the measurement says why.
GOAT used to wait for the whole utterance, then upload the whole WAV, then
wait for the whole answer. Measured that day on his own captured audio:

    batch ElevenLabs scribe      1089-2594 ms   after he stopped speaking
    local whisper server         1745-3122 ms   (and cannot do Georgian)

That entire wait is dead air, and it is pure overlap waste: the first five
seconds of a six-second sentence could have been transcribed while he was
still saying the sixth. Scribe v2 Realtime streams over a WebSocket, so the
transcript is essentially finished the moment he stops. Same measurement,
same audio, same key:

    realtime, language pinned     134-554 ms   after he stopped speaking

which is the sub-500ms target, from the stage that was costing the most.

TWO THINGS LEARNED THE HARD WAY, both encoded below:

1. **The language must be pinned.** With auto-detect the realtime model heard
   his Georgian as RUSSIAN and handed back Cyrillic transliteration ("Ааа,
   мотхидэн, эрти уна иквэз..."). Pinned to `kat` the same audio comes back
   in proper Mkhedruli. This is the opposite of the BATCH endpoint, where
   auto-detect is the accurate mode (see stt_gladia's note) — so the two ears
   are configured differently on purpose. In bilingual "auto" mode there is
   no language to pin, so this module stays out of the way and the batch ear
   takes the turn.

2. **GOAT's own VAD owns the turn, not the server's.** With
   `commit_strategy=vad` the server split one sentence into several committed
   fragments on its own schedule. `manual` puts the boundary exactly where
   GOAT already decided the utterance ended, which is the same boundary the
   rest of the app reasons about.

Failure is always survivable: anything that goes wrong here returns None and
the caller falls back to the batch ear with the full audio it already has, so
the worst case is exactly today's latency, never a lost sentence.
"""
import asyncio
import base64
import json
import os
import time
import urllib.parse

import numpy as np

import stt_gladia

WS_URL = os.environ.get(
    "GOAT_STT_RT_URL",
    "wss://api.elevenlabs.io/v1/speech-to-text/realtime")
MODEL = os.environ.get("GOAT_STT_RT_MODEL", "scribe_v2_realtime")
# Off by default is the wrong default for a latency goal, but a bad ear is
# worse than a slow one — GOAT_STT_REALTIME=off restores the batch-only path.
ENABLED = os.environ.get("GOAT_STT_REALTIME", "on").strip().lower() not in (
    "off", "0", "false", "no")
# How long to wait for the committed transcript after the last chunk. Measured
# worst case was 554 ms; 2.5 s is slack for a bad network, and blowing it just
# means the batch ear takes over.
COMMIT_WAIT_S = float(os.environ.get("GOAT_STT_RT_WAIT", "2.5"))
CONNECT_TIMEOUT_S = 4.0

# ISO 639-3, which is what the realtime endpoint documents.
LANG3 = {"ka": "kat", "en": "eng"}

_down_until = [0.0]      # back off after a failure instead of retrying hot
_DOWN_S = 120.0
_SOFT = object()         # queue sentinel: commit early, keep the session open


# What to do in bilingual "auto", which is the mode he actually runs in.
#   dual — open BOTH ears and keep whichever one's alphabet answers. Correct
#          and fast; costs two streams of realtime audio instead of one.
#   off  — leave auto to the batch ear (today's behaviour, ~2.5s).
# There is deliberately no "guess from last turn" option. A ka-pinned ear
# handed English comes back EMPTY, which is detectable and safe — but an
# en-pinned ear handed Georgian comes back as confident Latin nonsense
# ("Isle of Man. Some tabby cat" for "ოთხი ტაბია"), which is not detectable
# at all. Guessing wrong in that direction would put words in his mouth.
AUTO_MODE = os.environ.get("GOAT_STT_RT_AUTO", "dual").strip().lower()

_KA_RE = None


def _has_georgian(text: str) -> bool:
    global _KA_RE
    if _KA_RE is None:
        import re
        _KA_RE = re.compile(r"[ა-ჿ]")
    return bool(_KA_RE.search(text or ""))


def _usable() -> bool:
    if not ENABLED or time.monotonic() < _down_until[0]:
        return False
    if stt_gladia._scribe_key() is None:
        return False
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


def available(lang: str) -> bool:
    """Realtime needs a pinned language (note 1). "auto" is served by running
    one pinned ear per language and letting the alphabet decide."""
    if not _usable():
        return False
    if lang in LANG3:
        return True
    return AUTO_MODE == "dual"


def _mark_down():
    _down_until[0] = time.monotonic() + _DOWN_S


class Session:
    """One utterance, streamed as it is spoken.

    Lives on GOAT's asyncio loop; `feed()` is called from the audio callback
    thread and must never block it — it only hands the block across.
    """

    def __init__(self, lang: str, loop, sample_rate: int = 16000):
        self.lang = lang
        self.loop = loop
        self.rate = sample_rate
        self.q: asyncio.Queue = asyncio.Queue()
        self.partials: list = []
        self.committed: list = []
        self.error: str | None = None
        self.ready = asyncio.Event()     # socket open, safe to stream
        self._done = asyncio.Event()     # every commit has been answered
        self._task = None
        self._closed = False
        self.t_first_partial = 0.0
        self.t_commit_sent = 0.0
        # Speculative commit bookkeeping. GOAT's VAD waits out a silence
        # hangover before it will declare the turn over, and until now the
        # ear sat idle through all of it and only then began finalising —
        # two waits in a row for one pause. A soft commit starts the
        # finalising EARLY, part-way into the hangover, so most of it is
        # already done by the time the turn is officially over. If he turns
        # out to be mid-thought and keeps talking, the extra speech simply
        # arrives as another committed segment and the pieces are joined.
        self._commits_sent = 0
        self._commits_got = 0
        self._final = False
        # Latest interim text, and a listener for it. The reflex lane reads
        # this to recognise a finished command before the turn is formally
        # over — see GoatApp._early_reflex.
        self.last_partial = ""
        self.on_partial = None

    # ---- called from the AUDIO thread ----
    def feed(self, chunk: np.ndarray):
        if self._closed:
            return
        try:
            self.loop.call_soon_threadsafe(self.q.put_nowait, chunk)
        except RuntimeError:
            pass    # loop gone — the batch ear still has the whole utterance

    def soft_commit(self):
        """He has gone quiet but the turn is not called yet — start finalising
        now so the hangover and the transcription overlap instead of queueing.
        Safe to call more than once; safe if he starts talking again."""
        if self._closed:
            return
        try:
            self.loop.call_soon_threadsafe(self.q.put_nowait, _SOFT)
        except RuntimeError:
            pass

    def end(self):
        """He stopped talking: commit and stop accepting audio."""
        if self._closed:
            return
        self._closed = True
        try:
            self.loop.call_soon_threadsafe(self.q.put_nowait, None)
        except RuntimeError:
            pass

    # ---- loop side ----
    def start(self):
        self._task = self.loop.create_task(self._run())
        return self

    async def _run(self):
        import websockets
        q = {"model_id": MODEL,
             "audio_format": f"pcm_{self.rate}",
             "commit_strategy": "manual",
             "language_code": LANG3[self.lang]}
        url = WS_URL + "?" + urllib.parse.urlencode(q)
        key = stt_gladia._scribe_key()
        try:
            async with websockets.connect(
                    url, additional_headers={"xi-api-key": key},
                    open_timeout=CONNECT_TIMEOUT_S,
                    max_size=4 * 1024 * 1024) as ws:
                self.ready.set()
                reader = asyncio.create_task(self._read(ws))
                try:
                    await self._pump(ws)
                    await asyncio.wait_for(self._done.wait(), COMMIT_WAIT_S)
                except asyncio.TimeoutError:
                    if not self.committed:
                        self.error = "no committed transcript in time"
                finally:
                    reader.cancel()
        except Exception as e:  # noqa: BLE001 — a dead ear must not kill a turn
            self.error = f"{type(e).__name__}: {e}"
            self.ready.set()
            _mark_down()
        self._done.set()

    async def _commit(self, ws):
        self._commits_sent += 1
        self.t_commit_sent = time.monotonic()
        await ws.send(json.dumps({
            "message_type": "input_audio_chunk",
            "audio_base_64": "",
            "commit": True,
            "sample_rate": self.rate}))

    async def _pump(self, ws):
        """Audio blocks out as they arrive; commits where GOAT's VAD says."""
        while True:
            item = await self.q.get()
            if item is None:
                break
            if item is _SOFT:
                await self._commit(ws)      # early, overlapping the hangover
                continue
            pcm = np.clip(item * 32767.0, -32768, 32767).astype(np.int16)
            await ws.send(json.dumps({
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(pcm.tobytes()).decode(),
                "sample_rate": self.rate}))
        # Final commit at the boundary GOAT's VAD actually chose, rather than
        # one the server invents. If a soft commit already carried the whole
        # sentence this one just closes an empty segment, which costs nothing.
        self._final = True
        await self._commit(ws)

    async def _read(self, ws):
        async for raw in ws:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            mt = m.get("message_type") or ""
            if mt == "partial_transcript":
                if m.get("text"):
                    if not self.t_first_partial:
                        self.t_first_partial = time.monotonic()
                    self.partials.append(m["text"])
                    self.last_partial = demtavruli(m["text"])
                    if self.on_partial:
                        try:
                            self.on_partial(self.last_partial)
                        except Exception:  # noqa: BLE001
                            pass
            elif mt.startswith("committed_transcript"):
                self._commits_got += 1
                if m.get("text"):
                    self.committed.append(m["text"])
                # Done only once the FINAL commit has been answered — a soft
                # commit's transcript is a piece of the turn, not the end of
                # it, and returning here would drop whatever he said next.
                if self._final and self._commits_got >= self._commits_sent:
                    self._done.set()
                    return
            elif mt == "commit_throttled":
                # Two commits too close together: the server keeps the audio,
                # it just refuses this extra boundary. Not an error for us.
                self._commits_sent = max(self._commits_got, self._commits_sent - 1)
                if self._final and self._commits_got >= self._commits_sent:
                    self._done.set()
                    return
            elif m.get("error") or "error" in mt:
                self.error = f"{mt}: {m.get('error')}"
                _mark_down()
                self._done.set()
                return

    async def result(self) -> str | None:
        """Committed text, or None to mean 'use the batch ear'."""
        try:
            await asyncio.wait_for(self._done.wait(), COMMIT_WAIT_S + 2.0)
        except asyncio.TimeoutError:
            return None
        if self.error or not self.committed:
            if self.error:
                print(f"[stt-rt] {self.error}")
            return None
        text = " ".join(t.strip() for t in self.committed if t.strip()).strip()
        text = demtavruli(text)
        if not text:
            return None
        ms = ((time.monotonic() - self.t_commit_sent) * 1000
              if self.t_commit_sent else -1)
        print(f"[stt-rt] committed in {ms:.0f}ms: {text[:60]!r}")
        return text


def start(lang: str, loop, sample_rate: int = 16000) -> Session | None:
    if not available(lang):
        return None
    try:
        return Session(lang, loop, sample_rate).start()
    except Exception as e:  # noqa: BLE001
        print(f"[stt-rt] could not start: {e}")
        _mark_down()
        return None


class Pair:
    """Two pinned ears for bilingual "auto", answered by the alphabet.

    This is the same rule GOAT already uses to decide the language of a turn
    (an alphabet is a fact; a confidence score is an opinion), just applied
    one stage earlier. The Georgian ear returns Mkhedruli or nothing; if it
    returns Georgian letters, he spoke Georgian.
    """

    __slots__ = ("ka", "en", "lang", "on_partial")

    def __init__(self, loop, sample_rate):
        self.ka = Session("ka", loop, sample_rate)
        self.en = Session("en", loop, sample_rate)
        self.lang = ""      # filled in once the alphabet has spoken
        self.on_partial = None

    @property
    def last_partial(self) -> str:
        """The alphabet decides here too: if the Georgian ear has heard
        Georgian letters, that is the live transcript."""
        ka = self.ka.last_partial if self.ka else ""
        if ka and _has_georgian(ka):
            return ka
        en = self.en.last_partial if self.en else ""
        return en or ka

    def _both(self):
        return (s for s in (self.ka, self.en) if s is not None)

    def start(self):
        for s in self._both():
            s.start()
        return self

    def feed(self, chunk):
        for s in self._both():
            s.feed(chunk)

    def soft_commit(self):
        for s in self._both():
            s.soft_commit()

    def end(self):
        for s in self._both():
            s.end()

    async def result(self):
        ka, en = await asyncio.gather(self.ka.result(), self.en.result())
        if ka and _has_georgian(ka):
            self.lang = "ka"
            return ka
        if en and not _has_georgian(en):
            self.lang = "en"
            return en
        # Georgian ear silent AND English ear gave something Georgian-looking
        # (or both empty) — don't guess, let the batch ear settle it.
        if ka:
            self.lang = "ka"
            return ka
        return None


def start_threadsafe(lang: str, loop, sample_rate: int = 16000):
    """Open a session FROM THE AUDIO THREAD.

    The object is built here so the very first block can be queued into it
    immediately; only the socket task is handed to the loop. Building it on
    the loop instead would drop the opening blocks of every sentence — the
    preroll, which is exactly the part that carries his first word.
    """
    if not available(lang):
        return None
    try:
        s = (Session(lang, loop, sample_rate) if lang in LANG3
             else Pair(loop, sample_rate))
        loop.call_soon_threadsafe(s.start)
        return s
    except Exception as e:  # noqa: BLE001
        print(f"[stt-rt] could not start: {e}")
        _mark_down()
        return None


# MTAVRULI (U+1C90-U+1CBF) is Georgian's all-caps form. The realtime model
# capitalises the first letter of a sentence with it, which is not how the
# language is written in running text — and GOAT's own persona forbids it in
# replies. Fold it back to Mkhedruli so the transcript reads like Georgian.
_MTAVRULI_SHIFT = 0x1C90 - 0x10D0


def demtavruli(text: str) -> str:
    return "".join(
        chr(ord(c) - _MTAVRULI_SHIFT) if 0x1C90 <= ord(c) <= 0x1CBF else c
        for c in (text or ""))
