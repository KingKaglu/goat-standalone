"""Cloud Georgian hearing via Gladia (measured 2026-07-10: local whisper
can't do Georgian — base romanizes, small hallucinates; Gladia returns real
Georgian script in ~4s on short utterances, free tier 10h/month).

Used ONLY when GOAT is in Georgian mode AND a key exists — English mode
stays 100% local. Privacy note: in Georgian mode utterance audio goes to
Gladia's servers.

Key lives in .goat-secrets.json at the project root (gitignored via the
.goat-* pattern) as {"gladia_api_key": "..."} or in GLADIA_API_KEY env.
Same calling contract as stt_whisper.transcribe: '' = junk/silence (stay
quiet), None = the route is BROKEN (caller should fall back and/or say so).
"""
import io
import json
import os
import time

import httpx
import numpy as np
from scipy.io import wavfile

from goat_paths import GOAT_ROOT

SECRETS_FILE = os.path.join(GOAT_ROOT, ".goat-secrets.json")
BASE = "https://api.gladia.io/v2"


def _key() -> str | None:
    key = os.environ.get("GLADIA_API_KEY")
    if key:
        return key
    try:
        with open(SECRETS_FILE, encoding="utf-8") as f:
            return json.load(f).get("gladia_api_key") or None
    except (OSError, json.JSONDecodeError):
        return None


def available() -> bool:
    return _key() is not None


def transcribe(audio: np.ndarray, sample_rate: int = 16000,
               language: str = "ka"):
    """float32 [-1,1] mono → text in the requested language.
    Blocking (~4s) — call from a worker thread."""
    key = _key()
    if key is None:
        return None
    if len(audio) < sample_rate * 0.3:
        return ""
    buf = io.BytesIO()
    pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    wavfile.write(buf, sample_rate, pcm)
    try:
        up = httpx.post(f"{BASE}/upload", headers={"x-gladia-key": key},
                        files={"audio": ("u.wav", buf.getvalue(), "audio/wav")},
                        timeout=20.0)
        up.raise_for_status()
        job = httpx.post(f"{BASE}/pre-recorded", headers={"x-gladia-key": key},
                         json={"audio_url": up.json()["audio_url"],
                               "language_config": {"languages": [language],
                                                   "code_switching": False}},
                         timeout=20.0)
        job.raise_for_status()
        result_url = job.json()["result_url"]
        deadline = time.time() + 25
        while time.time() < deadline:
            r = httpx.get(result_url, headers={"x-gladia-key": key},
                          timeout=15.0)
            d = r.json()
            if d.get("status") == "done":
                text = (d["result"]["transcription"]["full_transcript"] or "").strip()
                # junk gate: silence/hallucinated crumbs stay silent
                if len(text) < 2:
                    return ""
                return text
            if d.get("status") == "error":
                print("[gladia] job error:", str(d)[:200])
                return None
            time.sleep(0.4)
        print("[gladia] result poll timed out")
        return None
    except httpx.HTTPError as e:
        print("[gladia] request failed:", e)
        return None
