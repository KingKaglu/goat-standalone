"""GOAT's online voice: Microsoft neural TTS via the edge-tts package.

Two characters live here. "goat" is Ava Multilingual (Eka in Georgian) —
the default. "ultron" is the low synthetic one: a male neural voice pitched
down at the source, then coloured by _ultron() — a detuned ghost of itself
under every syllable, a short metallic comb, gentle drive and a dead-room
tail. The colour is applied to the local Piper fallback too, so the
character survives going offline.

Online-only — callers fall back to the local Piper voice when this raises,
so GOAT never goes mute.

Blocking API on purpose: the TTS pipeline runs one worker thread and piper's
synth is blocking too, so both voices share the same calling contract. Each
call runs its own short-lived asyncio loop; connection setup is a few hundred
ms, fine at sentence granularity.
"""
import asyncio
import io

import edge_tts
import numpy as np
import soundfile as sf
from scipy.signal import lfilter, resample_poly

# Who GOAT sounds like: a voice per language, the delivery edge-tts applies
# at the source, and an optional post-processing colour.
CHARACTERS = {
    "goat": {
        "voices": {"en": "en-US-AvaMultilingualNeural",
                   "ka": "ka-GE-EkaNeural"},  # Microsoft's Georgian neural voice
        # English accents, picked by set_accent(); "us" is the multilingual
        # Ava above, the rest are regional neurals of the same character.
        "accents": {"us": "en-US-AvaMultilingualNeural",
                    "uk": "en-GB-SoniaNeural",
                    "au": "en-AU-NatashaNeural",
                    "ie": "en-IE-EmilyNeural",
                    "in": "en-IN-NeerjaNeural"},
        "rate": "+10%",   # same slightly-brisk pace as piper's length_scale 0.85
        "pitch": "+0Hz",
        "fx": None,
    },
    "ultron": {
        # Andrew is the closest edge voice to Spader's measured calm; Giorgi
        # is the only male Georgian neural voice Microsoft ships.
        "voices": {"en": "en-US-AndrewMultilingualNeural",
                   "ka": "ka-GE-GiorgiNeural"},
        "accents": {"us": "en-US-AndrewMultilingualNeural",
                    "uk": "en-GB-RyanNeural",
                    "au": "en-AU-WilliamMultilingualNeural",
                    "ie": "en-IE-ConnorNeural",
                    "in": "en-IN-PrabhatNeural"},
        "rate": "-4%",     # unhurried — he has all the time in the world
        "pitch": "-28Hz",  # at the source, so formants stay believable
        "fx": "ultron",
    },
}

LANG = "en"
CHARACTER = "goat"
ACCENT = "uk"  # which English he hears; ignored for other languages
VOICES = CHARACTERS["goat"]["voices"]  # kept for callers reading the old table
VOICE = CHARACTERS["goat"]["accents"]["uk"]
RATE = CHARACTERS["goat"]["rate"]
PITCH = CHARACTERS["goat"]["pitch"]


def _apply():
    global VOICE, RATE, PITCH
    c = CHARACTERS.get(CHARACTER, CHARACTERS["goat"])
    VOICE = c["voices"].get(LANG, VOICE)
    if LANG == "en":
        VOICE = c.get("accents", {}).get(ACCENT, VOICE)
    RATE, PITCH = c["rate"], c["pitch"]


def set_accent(name: str) -> bool:
    """Which English accent GOAT speaks in. Unknown names change nothing."""
    global ACCENT
    name = (name or "").strip().lower()
    if name not in CHARACTERS[CHARACTER].get("accents", {}):
        return False
    ACCENT = name
    _apply()
    return True


def set_language(lang: str):
    """Switch the voice; unknown languages keep the current one."""
    global LANG
    if lang in CHARACTERS[CHARACTER]["voices"]:
        LANG = lang
        _apply()


def set_character(name: str) -> bool:
    """Who GOAT sounds like. Unknown names leave the voice untouched."""
    global CHARACTER
    if name not in CHARACTERS:
        return False
    CHARACTER = name
    _apply()
    return True


def _ultron(x: np.ndarray, rate: int) -> np.ndarray:
    """The synthetic edge. Tuned to stay fully intelligible — menace, not a
    broken speaker."""
    n = x.size
    t = np.arange(n, dtype=np.float32)

    # Ghost layer: the same words a hair flat and a hair behind, so every
    # syllable arrives twice. This is what reads as "not a person".
    ghost = np.interp(t * 0.993, t, x).astype(np.float32)
    d = int(rate * 0.019)
    ghost = np.concatenate([np.zeros(d, np.float32), ghost])[:n]
    y = x + 0.40 * ghost

    # Metallic comb: one short feedback delay — the resonance of a body that
    # isn't a chest. Mixed in part-strength so consonants survive it.
    c = max(1, int(rate * 0.0042))
    if n > c:
        a = np.zeros(c + 1, dtype=np.float32)
        a[0], a[c] = 1.0, -0.33
        y = 0.75 * y + 0.25 * lfilter([1.0], a, y).astype(np.float32)

    # Drive, then a small dead-room tail.
    y = np.tanh(y * 1.5).astype(np.float32) / np.float32(np.tanh(1.5))
    for delay_s, g in ((0.055, 0.16), (0.107, 0.08)):
        k = int(rate * delay_s)
        if n > k:
            y += g * np.concatenate([np.zeros(k, np.float32), y[:n - k]])

    # Match the level we came in at — a character change is not a volume change.
    peak, src = float(np.max(np.abs(y))), float(np.max(np.abs(x)))
    if peak > 0 and src > 0:
        y *= src / peak
    return np.clip(y, -1.0, 1.0).astype(np.float32)


def color(data: np.ndarray, rate: int = 16000) -> np.ndarray:
    """Apply the current character's colour. Identity for 'goat', so the
    fallback voice can call it unconditionally."""
    fx = CHARACTERS.get(CHARACTER, {}).get("fx")
    if fx != "ultron" or data.size == 0:
        return data
    return _ultron(data.astype(np.float32), rate)


async def _collect_mp3(text: str) -> bytes:
    com = edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH)
    chunks = []
    async for chunk in com.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    return b"".join(chunks)


# Hedged request (2026-09-25). Measured the same evening: a short clause
# normally lands whole in 0.8-1.7s, but one Georgian clause took 6.4s to its
# FIRST byte — the service, not the text. The spread is in connection setup
# and service queueing, so a second identical request fired after HEDGE_S
# almost always beats a stuck first one. The first to finish wins; the other
# is cancelled. Costs a duplicate request only on the slow ones.
HEDGE_S = 0.9


async def _collect_hedged(text: str) -> bytes:
    first = asyncio.ensure_future(_collect_mp3(text))
    done, _ = await asyncio.wait({first}, timeout=HEDGE_S)
    if done:
        return first.result()
    second = asyncio.ensure_future(_collect_mp3(text))
    pending = {first, second}
    err = None
    while pending:
        done, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t.exception() is None and len(t.result()) >= 200:
                for p in pending:
                    p.cancel()
                return t.result()
            err = t.exception() or err
    raise err or RuntimeError("edge-tts returned no audio")


# ---- one live connection, reused (2026-09-25, his goal: instant answers) ----
# edge_tts.Communicate opens a fresh HTTP session + websocket for EVERY clause:
# DNS, TCP, TLS and the WS upgrade each time. Measured on his line: 0.8-2.6s
# per first clause that way. The service happily takes request after request
# on one socket (Edge itself does this) — measured on one reused socket, 20s
# idle between rounds: 0.18-0.43s for the same clauses. So GOAT keeps one
# socket open on a private loop thread, re-dials it when it dies, and primes
# it the moment he starts talking so it is warm by the time he stops.
# Anything odd -> the per-clause path above (hedged), then Piper.
PERSIST = True
_REQ_TIMEOUT_S = 2.5     # a live socket answers a clause in well under 1s
_MAX_BYTES = 3000        # longer text goes the old way (service splits at 4096)


class _Live:
    def __init__(self):
        import threading
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True,
                         name="tts-edge-live").start()
        self.session = None
        self.ws = None
        self.configured = False
        self.lock = None

    async def _close(self):
        for obj in (self.ws, self.session):
            try:
                if obj is not None:
                    await obj.close()
            except Exception:  # noqa: BLE001
                pass
        self.ws = self.session = None
        self.configured = False

    async def _dial(self):
        import aiohttp
        from edge_tts import communicate as C
        from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
        from edge_tts.drm import DRM
        await self._close()
        t0 = asyncio.get_running_loop().time()
        self.session = aiohttp.ClientSession(trust_env=True)
        self.ws = await self.session.ws_connect(
            f"{WSS_URL}&ConnectionId={C.connect_id()}"
            f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
            f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}",
            compress=15, headers=DRM.headers_with_muid(WSS_HEADERS),
            ssl=C._SSL_CTX, timeout=aiohttp.ClientWSTimeout(ws_close=2.0))
        print(f"[tts] voice socket dialled in "
              f"{(asyncio.get_running_loop().time() - t0) * 1000:.0f}ms")

    async def _ensure(self):
        if self.ws is None or self.ws.closed:
            await self._dial()

    async def prime(self):
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            try:
                await self._ensure()
            except Exception:  # noqa: BLE001 — synth will retry or fall back
                await self._close()

    async def _once(self, text: str, voice: str, rate: str, pitch: str) -> bytes:
        import aiohttp
        from xml.sax.saxutils import escape
        from edge_tts import communicate as C
        from edge_tts.data_classes import TTSConfig
        await self._ensure()
        ws = self.ws
        if not self.configured:
            await ws.send_str(
                f"X-Timestamp:{C.date_to_string()}\r\n"
                "Content-Type:application/json; charset=utf-8\r\n"
                "Path:speech.config\r\n\r\n"
                '{"context":{"synthesis":{"audio":{"metadataoptions":{'
                '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
                '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n')
            self.configured = True
        tc = TTSConfig(voice, rate, "+0%", pitch, "SentenceBoundary")
        await ws.send_str(C.ssml_headers_plus_data(
            C.connect_id(), C.date_to_string(),
            C.mkssml(tc, escape(C.remove_incompatible_characters(text)))))
        out = []
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                # [2-byte header length][headers][\r\n][mp3] — the same
                # slicing as edge_tts.communicate.get_headers_and_data
                hl = int.from_bytes(msg.data[:2], "big")
                if len(msg.data) > hl + 2:
                    out.append(msg.data[hl + 2:])
            elif msg.type == aiohttp.WSMsgType.TEXT:
                if "Path:turn.end" in msg.data:
                    return b"".join(out)
            else:
                break
        raise ConnectionError("edge socket closed mid-clause")

    async def request(self, text, voice, rate, pitch) -> bytes:
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            t0 = asyncio.get_running_loop().time()
            try:
                return await asyncio.wait_for(
                    self._once(text, voice, rate, pitch), _REQ_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                await self._close()
                # A socket the service quietly dropped fails FAST — re-dial
                # once. A slow failure means the service is slow; don't pay
                # twice, let the hedged per-clause path race it instead.
                if asyncio.get_running_loop().time() - t0 > 0.5:
                    raise
            return await asyncio.wait_for(
                self._once(text, voice, rate, pitch), _REQ_TIMEOUT_S)


_live = None


def _get_live():
    global _live
    if _live is None:
        _live = _Live()
    return _live


def prime():
    """Open (or re-open) the live socket in the background. Non-blocking —
    GOAT calls it when he STARTS talking, so the dial hides under his voice."""
    if not PERSIST:
        return
    try:
        asyncio.run_coroutine_threadsafe(_get_live().prime(), _get_live().loop)
    except Exception:  # noqa: BLE001
        pass


def _persist_mp3(text: str) -> bytes | None:
    if not PERSIST or len(text.encode("utf-8")) > _MAX_BYTES:
        return None
    live = _get_live()
    fut = asyncio.run_coroutine_threadsafe(
        live.request(text, VOICE, RATE, PITCH), live.loop)
    try:
        mp3 = fut.result(timeout=2 * _REQ_TIMEOUT_S + 1.0)
    except Exception as e:  # noqa: BLE001 — the per-clause path takes over
        fut.cancel()
        print(f"[tts] live socket failed ({type(e).__name__}) — per-clause path")
        return None
    return mp3 if len(mp3) >= 200 else None


def synth(text: str, target_rate: int = 16000, timeout_s: float = 10.0) -> np.ndarray:
    """text → float32 mono at target_rate. Raises on any network/decode
    problem — caller decides on the fallback voice."""
    text = " ".join(text.split())
    if not text:
        return np.zeros(0, dtype=np.float32)

    mp3 = _persist_mp3(text)
    if mp3 is None:
        mp3 = asyncio.run(asyncio.wait_for(_collect_hedged(text),
                                           timeout=timeout_s))
    if len(mp3) < 200:
        raise RuntimeError("edge-tts returned no audio")

    # soundfile's bundled libsndfile (>=1.1) decodes mp3 natively — no ffmpeg.
    data, rate = sf.read(io.BytesIO(mp3), dtype="float32")
    if data.ndim > 1:
        data = data[:, 0]
    if rate != target_rate:
        g = int(np.gcd(int(rate), int(target_rate)))
        data = resample_poly(data, target_rate // g, rate // g).astype(np.float32)
    return color(data, target_rate)
