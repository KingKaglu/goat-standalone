"""GOAT's primary voice: Microsoft "Ava Multilingual" neural (the same
en-US-AvaMultilingualNeural the Node app's /tts route now serves), via the
edge-tts package. Online-only — callers fall back to the local Piper voice
when this raises, so GOAT never goes mute offline.

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
from scipy.signal import resample_poly

VOICES = {
    "en": "en-US-AvaMultilingualNeural",
    "ka": "ka-GE-EkaNeural",  # Microsoft's Georgian neural voice
}
VOICE = VOICES["en"]
RATE = "+10%"  # same slightly-brisk pace as piper's length_scale 0.85


def set_language(lang: str):
    """Switch the voice; unknown languages keep the current one."""
    global VOICE
    VOICE = VOICES.get(lang, VOICE)


async def _collect_mp3(text: str) -> bytes:
    com = edge_tts.Communicate(text, VOICE, rate=RATE)
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
    return data
