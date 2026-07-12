"""Cloud Georgian hearing: ElevenLabs Scribe primary, Gladia fallback.

Measured 2026-07-10: local whisper can't do Georgian (base romanizes, small
hallucinates). Gladia's only ka model (Solaria-1) garbles real-mic speech —
replay-verified against captured WAVs. Scribe (scribe_v2) transcribed the
same WAVs near-perfectly, so it is the primary route; Gladia stays as
fallback if the Scribe key/route breaks.

Used ONLY when GOAT is in Georgian mode AND a key exists — English mode
stays 100% local. Privacy note: in Georgian mode utterance audio goes to
the cloud STT provider's servers.

Keys live in .goat-secrets.json at the project root (gitignored via the
.goat-* pattern) as {"elevenlabs_api_key": "...", "gladia_api_key": "..."}
or in ELEVENLABS_API_KEY / GLADIA_API_KEY env. NOTE: the ElevenLabs key
must have the speech_to_text permission enabled, else 401 missing_permissions.
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

import stt_whisper  # shares FIXES_FILE + _apply_fixes so both ears learn together
from goat_paths import GOAT_ROOT

SECRETS_FILE = os.path.join(GOAT_ROOT, ".goat-secrets.json")
SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"
GLADIA_BASE = "https://api.gladia.io/v2"
DEBUG_DIR = os.path.join(GOAT_ROOT, "python", "stt-debug")


def _debug_log(audio, sample_rate, text, note):
    """Persistent hearing trail: the VBS launcher discards stdout, so real-mic
    mishearings were undiagnosable. Keeps last 20 utterance WAVs + a log so
    a bad transcript can be replayed against the cloud APIs with other configs."""
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
        wavfile.write(os.path.join(DEBUG_DIR, stamp + ".wav"), sample_rate, pcm)
        wavs = sorted(f for f in os.listdir(DEBUG_DIR) if f.endswith(".wav"))
        for old in wavs[:-20]:
            os.remove(os.path.join(DEBUG_DIR, old))
        with open(os.path.join(DEBUG_DIR, "stt-debug.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"{stamp} {len(audio) / sample_rate:.1f}s "
                    f"[{note}] {text}\n")
    except OSError:
        pass


def _secret(env_name: str, json_key: str) -> str | None:
    key = os.environ.get(env_name)
    if key:
        return key
    try:
        with open(SECRETS_FILE, encoding="utf-8") as f:
            return json.load(f).get(json_key) or None
    except (OSError, json.JSONDecodeError):
        return None


def _scribe_key() -> str | None:
    return _secret("ELEVENLABS_API_KEY", "elevenlabs_api_key")


def _gladia_key() -> str | None:
    return _secret("GLADIA_API_KEY", "gladia_api_key")


def available() -> bool:
    return _scribe_key() is not None or _gladia_key() is not None


# Names/terms Scribe should be biased toward hearing; stt-fixes.json values
# are folded in too, so every learned correction also becomes a hint.
KEYTERM_SEED = ["GOAT", "გიორგი", "Claude", "Fable"]


def _keyterms() -> list[str]:
    terms = list(KEYTERM_SEED)
    try:
        with open(stt_whisper.FIXES_FILE, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if not k.startswith("_") and v not in terms:
                    terms.append(v)
    except (OSError, json.JSONDecodeError):
        pass
    return terms[:90]  # >100 keyterms triggers 20s minimum billing


def _scribe(key: str, wav: bytes, language: str) -> str | None:
    """One blocking POST (~2s); returns raw transcript or None on failure."""
    try:
        r = httpx.post(SCRIBE_URL, headers={"xi-api-key": key},
                       data={"model_id": "scribe_v2",
                             "language_code": language,
                             # sound labels like "(სიცილი)" would pollute chat
                             "tag_audio_events": "false",
                             "temperature": "0",
                             "keyterms": _keyterms()},
                       files={"file": ("u.wav", wav, "audio/wav")},
                       timeout=30.0)
        r.raise_for_status()
        return r.json().get("text") or ""
    except httpx.HTTPError as e:
        print("[scribe] request failed:", e)
        return None


def _gladia(key: str, wav: bytes, language: str) -> str | None:
    """upload -> job -> poll; returns raw transcript or None on failure."""
    try:
        up = httpx.post(f"{GLADIA_BASE}/upload", headers={"x-gladia-key": key},
                        files={"audio": ("u.wav", wav, "audio/wav")},
                        timeout=20.0)
        up.raise_for_status()
        job = httpx.post(f"{GLADIA_BASE}/pre-recorded",
                         headers={"x-gladia-key": key},
                         json={"audio_url": up.json()["audio_url"],
                               # en alongside: forcing ka-only makes Gladia
                               # hallucinate repetition loops on English speech
                               "language_config": {"languages": [language, "en"],
                                                   "code_switching": True}},
                         timeout=20.0)
        job.raise_for_status()
        result_url = job.json()["result_url"]
        deadline = time.time() + 25
        while time.time() < deadline:
            r = httpx.get(result_url, headers={"x-gladia-key": key},
                          timeout=15.0)
            d = r.json()
            if d.get("status") == "done":
                return d["result"]["transcription"]["full_transcript"] or ""
            if d.get("status") == "error":
                print("[gladia] job error:", str(d)[:200])
                return None
            time.sleep(0.4)
        print("[gladia] result poll timed out")
        return None
    except httpx.HTTPError as e:
        print("[gladia] request failed:", e)
        return None


def transcribe(audio: np.ndarray, sample_rate: int = 16000,
               language: str = "ka"):
    """float32 [-1,1] mono → text in the requested language.
    Blocking (~2-4s) — call from a worker thread."""
    scribe_key = _scribe_key()
    gladia_key = _gladia_key()
    if scribe_key is None and gladia_key is None:
        return None
    if len(audio) < sample_rate * 0.3:
        return ""
    buf = io.BytesIO()
    pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    wavfile.write(buf, sample_rate, pcm)
    wav = buf.getvalue()

    engine, text = "scribe", None
    if scribe_key is not None:
        text = _scribe(scribe_key, wav, language)
    if text is None and gladia_key is not None:
        engine, text = "gladia", _gladia(gladia_key, wav, language)
    if text is None:
        return None

    text = text.strip()
    # junk gate: silence/hallucinated crumbs stay silent
    if len(text) < 2:
        _debug_log(audio, sample_rate, text, f"dropped:junk:{engine}")
        return ""
    # repetition gate: ASR hallucination on noise comes out as
    # one token looped ("მინისის მინისის ..." x19) — drop it
    words = text.split()
    if len(words) >= 4 and len(set(words)) <= max(1, len(words) // 4):
        print(f"[{engine}] repetition hallucination dropped:", text[:60])
        _debug_log(audio, sample_rate, text, f"dropped:repetition:{engine}")
        return ""
    _debug_log(audio, sample_rate, text, f"ok:{engine}")
    # log keeps the RAW transcript (that's what we teach against);
    # the caller gets it with learned stt-fixes corrections applied.
    return stt_whisper._apply_fixes(text)
