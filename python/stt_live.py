"""Georgian ear on Gemini Live's own transcription (2026-10-25).

His Georgian by voice failed all day: GOAT was in English mode, so it went
through the English-pinned Whisper; Gladia (the old Georgian ear) takes 5-9
s; ElevenLabs is out of credits. Gemini Live transcribes the audio it is
given ("inputAudioTranscription"), and on his own Georgian recording that
transcript was right where Gladia's was not ("გამარჯობა, გიორგი. ეს ჩემი
ახალი ხმაა." vs "ეს ჩემია ხალიხმა"). Free on his key.

transcribe(audio) -> text | None. Blocking: run it in a thread. One short
session per utterance: the audio goes in between activityStart/End (GOAT's
own VAD already cut it), the transcript comes back, the session closes —
nothing it might say is ever played.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import threading
import time

import numpy as np

import talk_brain

MODEL = os.environ.get("GOAT_STT_LIVE_MODEL", "gemini-3.8-live")
URL = ("wss://generativelanguage.googleapis.com/ws/"
       "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
TIMEOUT_S = 8.0
QUIET_S = 0.6        # no new transcript words this long after the end: done
CHUNK = 3200         # 200 ms of 16 kHz PCM per message


WARM_S = 60.0        # a dialled session older than this isn't trusted

_loop = None
_loop_lock = threading.Lock()
_warm: list = []     # [(monotonic time, ws)] — one session dialled ahead


def _bg_loop():
    """One asyncio loop of our own (the sessions live on it)."""
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True,
                             name="stt-live").start()
    return _loop


async def _dial(key: str):
    import websockets
    ws = await websockets.connect(f"{URL}?key={key}", max_size=8 * 1024 * 1024,
                                  open_timeout=5)
    await ws.send(json.dumps({"setup": {
        "model": f"models/{MODEL}",
        "generationConfig": {"responseModalities": ["AUDIO"],
                             "thinkingConfig": {"thinkingBudget": 0}},
        "systemInstruction": {"parts": [{"text": "Listen only. Reply with one word: ok."}]},
        # His two languages named: without it short Georgian came back as
        # Hindi, Spanish or Latin letters ("Romelisatia"); with it "რომელი
        # საათია?" (2026-10-26, his Georgian-mode clips).
        "inputAudioTranscription": {"languageCodes": ["ka-GE", "en-US"]},
        "realtimeInputConfig": {"automaticActivityDetection": {"disabled": True}},
    }}))
    first = json.loads(await asyncio.wait_for(ws.recv(), 5))
    if "setupComplete" not in first:
        await ws.close()
        raise RuntimeError(f"setup refused: {str(first)[:120]}")
    return ws


async def _prime(key: str):
    now = time.monotonic()
    while _warm and now - _warm[0][0] > WARM_S:
        _, old = _warm.pop(0)
        try:
            await old.close()
        except Exception:  # noqa: BLE001
            pass
    if _warm:
        return
    try:
        _warm.append((time.monotonic(), await _dial(key)))
    except Exception:  # noqa: BLE001 — the turn dials its own
        pass


def prime():
    """He just started talking: dial a session now, so a Georgian turn
    doesn't pay the ~1 s of connecting. Non-blocking; unused ones expire."""
    key = talk_brain.api_key()
    if key:
        asyncio.run_coroutine_threadsafe(_prime(key), _bg_loop())


async def _take(key: str):
    now = time.monotonic()
    while _warm:
        t, ws = _warm.pop(0)
        if now - t < WARM_S and getattr(ws, "close_code", None) is None:
            return ws
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    return await _dial(key)


async def _run(audio: np.ndarray, key: str) -> str | None:
    ws = await _take(key)
    try:
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        await ws.send(json.dumps({"realtimeInput": {"activityStart": {}}}))
        for i in range(0, len(pcm), CHUNK * 2):
            await ws.send(json.dumps({"realtimeInput": {"audio": {
                "mimeType": "audio/pcm;rate=16000",
                "data": base64.b64encode(pcm[i:i + CHUNK * 2]).decode()}}}))
        await ws.send(json.dumps({"realtimeInput": {"activityEnd": {}}}))
        words, last = "", time.monotonic()
        deadline = time.monotonic() + TIMEOUT_S
        while time.monotonic() < deadline:
            wait = QUIET_S if words else max(0.1, deadline - time.monotonic())
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), wait))
            except asyncio.TimeoutError:
                if words:
                    break
                continue
            sc = m.get("serverContent") or {}
            t = (sc.get("inputTranscription") or {}).get("text")
            if t:
                words += t
                last = time.monotonic()
            # the transcript is complete once the model starts its own turn
            if words and (sc.get("modelTurn") or sc.get("turnComplete")):
                break
        return _one_script(words.strip())
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


# Live now and then writes a Latin letter or two inside a Georgian word
# ("მითხari" for "მითხარი", 2026-10-25). Only words that mix both scripts
# are touched; a whole English word stays as it is.
_LAT2KA = str.maketrans({"a": "ა", "b": "ბ", "g": "გ", "d": "დ", "e": "ე", "v": "ვ",
                         "z": "ზ", "i": "ი", "k": "კ", "l": "ლ", "m": "მ", "n": "ნ",
                         "o": "ო", "p": "პ", "r": "რ", "s": "ს", "t": "ტ", "u": "უ",
                         "f": "ფ", "q": "ქ", "y": "ყ", "c": "ც", "j": "ჯ", "h": "ჰ",
                         "x": "ხ", "w": "წ"})


def _one_script(text: str) -> str:
    def fix(m):
        w = m.group(0)
        if re.search(r"[Ⴀ-ჿ]", w) and re.search(r"[A-Za-z]", w):
            return w.lower().translate(_LAT2KA)
        return w
    return re.sub(r"[\wჀ-ჿ]+", fix, text)


def transcribe(audio: np.ndarray, sample_rate: int = 16000) -> str | None:
    """His words, or None when Live couldn't be reached (caller falls back)."""
    key = talk_brain.api_key()
    if not key or len(audio) < sample_rate * 0.3:
        return None
    if sample_rate != 16000:
        return None
    try:
        fut = asyncio.run_coroutine_threadsafe(
            _run(audio.astype(np.float32), key), _bg_loop())
        return fut.result(TIMEOUT_S + 6)
    except Exception as e:  # noqa: BLE001 — the cloud ear takes over
        print(f"[stt-live-ka] failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return None
