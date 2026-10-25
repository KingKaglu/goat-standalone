"""Barge-in against HIS real room: 10 minutes recorded 2026-10-25 while GOAT
talked (left = laptop mic, right = what the speakers played), replayed through
GOAT's real path — DuplexAudio's callback with the speaker track as GOAT's
own playback, WebRTC AEC, Silero VAD, the gates and the vote — on a
simulated clock.

Checks that the lower while-talking gate (PLAY_FLOOR_MARGIN / PLAY_GATE_MIN)
never lets GOAT's own echo stop it, in the real echo of his real speakers.
No microphone or speakers are used. Skips when the recording is absent
(python/stt-debug is not in git). Run: py -3.13 test_barge_room.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import soundfile as sf

import audio_io
from audio_io import BLOCK_SAMPLES, DuplexAudio

HERE = os.path.dirname(os.path.abspath(__file__))
ROOM = os.path.join(HERE, "stt-debug", "room-duplex-20261025.flac")
# Moments he really spoke (heard on the cleaned track): GOAT may stop there.
HIM_AT = [(272.8, 275.0)]

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


class Clock:
    def __init__(self):
        self.t = 1000.0
        self._real = __import__("time")

    def monotonic(self):
        return self.t

    def __getattr__(self, k):
        return getattr(self._real, k)


def replay(seconds: float | None = None):
    x, sr = sf.read(ROOM, dtype="float32")
    mic, spk = x[:, 0], x[:, 1]
    n = len(mic) if seconds is None else int(seconds * sr)
    clock = Clock()
    audio_io.time = clock
    # The echo is already IN the recording at full volume, so ducking here
    # would shrink the canceller's reference but not the echo — a mismatch no
    # live room has. Ducking is checked in test_barge_acoustic, whose echo is
    # made from what GOAT actually played.
    saved_duck, audio_io.DUCK_GAIN = audio_io.DUCK_GAIN, 1.0
    cuts, ducks = [], []
    a = DuplexAudio(on_interrupt=lambda pre: cuts.append(clock.t - 1000.0),
                    on_status=lambda m: ducks.append(clock.t - 1000.0)
                    if "possible interrupt" in m else None)
    a.warming_up = False
    out = np.zeros((BLOCK_SAMPLES, 1), dtype=np.float32)
    try:
        for i in range(0, n - BLOCK_SAMPLES, BLOCK_SAMPLES):
            clock.t = 1000.0 + i / sr
            blk = spk[i:i + BLOCK_SAMPLES]
            if float(np.abs(blk).max()) > 1e-4:
                a.queue_playback(blk.copy())   # GOAT's own voice, block by block
            a._callback(mic[i:i + BLOCK_SAMPLES].reshape(-1, 1), out,
                        BLOCK_SAMPLES, None, None)
            while not a._cleaned_q.empty():
                cleaned, was_playing, raw = a._cleaned_q.get()
                a._preroll.append(cleaned)
                a._recent.append(cleaned)
                a._run_vad(cleaned, was_playing)
            if a._utt_chunks is not None and not a.is_tts_playing:
                a._utt_chunks = None      # no brain here: drop captures
    finally:
        audio_io.time = clock._real
        audio_io.DUCK_GAIN = saved_duck
    return cuts, ducks


if not os.path.exists(ROOM):
    print("[SKIP] room recording missing")
    sys.exit(0)

def own_cuts(cuts):
    return [round(c, 1) for c in cuts if not any(a <= c <= b for a, b in HIM_AT)]


# The bar: the shipped while-talking gate adds no self-interrupt over the old
# 3x gate. (Measured 2026-10-25: neither stops itself in these 10 min; 1.5x
# with a 0.0005 minimum did once, at 478.7 s.) Compared rather than asserted
# absolute because the two tracks came from two recorders that drift ~10 ms
# against each other, which one duplex stream never does.
shipped = (audio_io.PLAY_FLOOR_MARGIN, audio_io.PLAY_GATE_MIN)
audio_io.PLAY_FLOOR_MARGIN, audio_io.PLAY_GATE_MIN = audio_io.NOISE_FLOOR_MARGIN, 0.0
base, _ = replay()
audio_io.PLAY_FLOOR_MARGIN, audio_io.PLAY_GATE_MIN = shipped
cuts, ducks = replay()
extra = [c for c in own_cuts(cuts)
         if not any(abs(c - b) <= 1.0 for b in own_cuts(base))]
check("the lower while-talking gate adds no self-interrupt in his room (10 min)",
      not extra, f"new self-cuts at {extra}")
check("he still stops GOAT where he really spoke over it",
      any(a <= c <= b for c in cuts for a, b in HIM_AT))
print(f"[info] old gate: {[round(c, 1) for c in base]}; "
      f"shipped: {[round(c, 1) for c in cuts]}, {len(ducks)} duck(s)")
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
