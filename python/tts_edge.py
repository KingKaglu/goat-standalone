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
        "rate": "+10%",   # same slightly-brisk pace as piper's length_scale 0.85
        "pitch": "+0Hz",
        "fx": None,
    },
    "ultron": {
        # Andrew is the closest edge voice to Spader's measured calm; Giorgi
        # is the only male Georgian neural voice Microsoft ships.
        "voices": {"en": "en-US-AndrewMultilingualNeural",
                   "ka": "ka-GE-GiorgiNeural"},
        "rate": "-4%",     # unhurried — he has all the time in the world
        "pitch": "-28Hz",  # at the source, so formants stay believable
        "fx": "ultron",
    },
}

LANG = "en"
CHARACTER = "goat"
VOICES = CHARACTERS["goat"]["voices"]  # kept for callers reading the old table
VOICE = VOICES["en"]
RATE = CHARACTERS["goat"]["rate"]
PITCH = CHARACTERS["goat"]["pitch"]


def _apply():
    global VOICE, RATE, PITCH
    c = CHARACTERS.get(CHARACTER, CHARACTERS["goat"])
    VOICE = c["voices"].get(LANG, VOICE)
    RATE, PITCH = c["rate"], c["pitch"]


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


def synth(text: str, target_rate: int = 16000, timeout_s: float = 10.0) -> np.ndarray:
    """text → float32 mono at target_rate. Raises on any network/decode
    problem — caller decides on the fallback voice."""
    text = " ".join(text.split())
    if not text:
        return np.zeros(0, dtype=np.float32)

    mp3 = asyncio.run(asyncio.wait_for(_collect_mp3(text), timeout=timeout_s))
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
