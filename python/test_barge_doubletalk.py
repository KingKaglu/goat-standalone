"""Talking over GOAT on the laptop, through GOAT's REAL audio path offline:
WebRTC AEC3 + Silero VAD + the gates + the duck + the vote, 10 ms blocks on a
simulated clock. No microphone or speakers.

2026-11-02, his report: he could not cut GOAT off on the laptop. Measured
here: his own voice is ~88% voiced chunks clean, but during double-talk the
canceller's suppressor leaves ~43% of them voiced, while the confirm vote
asked for 16 of 20 (80%) — so he had to talk 2-3 s over GOAT or it never
stopped. Fix: once GOAT has ducked for a possible interrupt, the vote drops to
DUCKED_NEEDED and the duck is deeper (less echo = less suppression of him).

Echo = GOAT's voice (testdata/aec/me.wav) through a delayed, smeared room
path after one earlier reply (so the canceller is warm, as live). His voice =
testdata/aec/him_long.wav at -6/-12/-18 dB under the echo, started at 8
points in the reply. Run: py -3.13 test_barge_doubletalk.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import soundfile as sf

import audio_io
from audio_io import BLOCK_SAMPLES, DuplexAudio, SAMPLE_RATE

TD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "aec")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


def load(name):
    a, sr = sf.read(os.path.join(TD, name), dtype="float32")
    a = a if a.ndim == 1 else a[:, 0]
    assert sr == SAMPLE_RATE, sr
    return a


def rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


class Clock:
    def __init__(self):
        self.t = 1000.0
        self._real = __import__("time")

    def monotonic(self):
        return self.t

    def __getattr__(self, k):
        return getattr(self._real, k)


ME, HIM = load("me.wav"), load("him_long.wav")
WARM_S, GAP_S = 6.0, 2.0


def run(him_at: float | None, him_db: float = -12.0, echo_gain: float = 0.5,
        noise: float = 0.0003) -> float | None:
    """Seconds from his first word to GOAT stopping (None = never). him_at is
    measured from the start of the reply he talks over."""
    clock = Clock()
    audio_io.time = clock
    cuts: list = []
    try:
        a = DuplexAudio(on_interrupt=lambda p: cuts.append(clock.t),
                        on_status=lambda m: None)
        a._noise_floor = noise * 1.25
        a.warming_up = False
        lead = WARM_S + GAP_S
        a.queue_playback(np.concatenate([
            ME[-int(WARM_S * SAMPLE_RATE):],
            np.zeros(int(GAP_S * SAMPLE_RATE), np.float32), np.tile(ME, 2)]))
        h = HIM * (echo_gain * rms(ME) / rms(HIM) * 10 ** (him_db / 20))
        start = None if him_at is None else lead + him_at
        seconds = (start + 3.0) if start is not None else lead + 12.0
        rng = np.random.default_rng(7)
        hist = np.zeros(SAMPLE_RATE, dtype=np.float32)
        out = np.zeros((BLOCK_SAMPLES, 1), dtype=np.float32)
        d = int(0.03 * SAMPLE_RATE)
        t0 = clock.t
        for b in range(int(seconds * SAMPLE_RATE / BLOCK_SAMPLES)):
            now = b * BLOCK_SAMPLES / SAMPLE_RATE
            mic = rng.normal(0, noise, BLOCK_SAMPLES).astype(np.float32)
            mic += echo_gain * hist[-d - BLOCK_SAMPLES:-d]
            mic += 0.3 * echo_gain * hist[-d - 80 - BLOCK_SAMPLES:-d - 80]
            if start is not None and now >= start:
                k = int((now - start) * SAMPLE_RATE)
                seg = h[k:k + BLOCK_SAMPLES]
                mic[:len(seg)] += seg
            clock.t = t0 + now
            a._callback(mic.reshape(-1, 1), out, BLOCK_SAMPLES, None, None)
            hist = np.concatenate([hist[BLOCK_SAMPLES:], out[:, 0].copy()])
            while not a._cleaned_q.empty():
                c, wp, _r = a._cleaned_q.get()
                a._preroll.append(c)
                a._recent.append(c)
                a._run_vad(c, wp)
            if cuts:
                return None if start is None else round(cuts[0] - t0 - start, 2)
        return None if start is None else None
    finally:
        audio_io.time = clock._real


def sweep(db):
    return [run(0.3 + k * 0.37, him_db=db) for k in range(8)]


def med(r):
    ok = [x for x in r if x is not None]
    return float(np.median(ok)) if ok else 99.0


if __name__ == "__main__":
    # GOAT on its own echo, nobody talking: it must never stop itself.
    clock_free = run(None)
    check("GOAT never cuts itself off on its own (warm) echo", clock_free is None)

    shipped = (audio_io.DUCKED_NEEDED, audio_io.DUCK_GAIN)
    new = {db: sweep(db) for db in (-6, -12, -18)}
    # The behaviour before 2026-11-02, for contrast: no lower ducked vote,
    # duck 0.3.
    audio_io.DUCKED_NEEDED, audio_io.DUCK_GAIN = audio_io.BARGE_NEEDED, 0.3
    old = {db: sweep(db) for db in (-6, -12, -18)}
    audio_io.DUCKED_NEEDED, audio_io.DUCK_GAIN = shipped
    for db in new:
        print(f"[info] him {db:>3} dB under echo  new {new[db]}  old {old[db]}")

    check("his voice 6 dB under GOAT's echo stops it at every start within 0.8 s",
          all(x is not None and x <= 0.8 for x in new[-6]), new[-6])
    check("12 dB under: typical stop within 0.7 s (median)", med(new[-12]) <= 0.7,
          f"median {med(new[-12])}")
    # 18 dB under the echo is the extreme case (loud passages can erase him
    # in the canceller entirely); before the fix 5 of 8 starts never stopped.
    check("18 dB under: stops at 6+ of 8 starts, median within 1.5 s",
          sum(x is not None for x in new[-18]) >= 6 and med(new[-18]) <= 1.5,
          f"{new[-18]} median {med(new[-18])}")
    stopped_new = sum(x is not None for r in new.values() for x in r)
    stopped_old = sum(x is not None for r in old.values() for x in r)
    check("stops at least as often as before the fix",
          stopped_new >= stopped_old, f"{stopped_new} vs {stopped_old}")
    check("faster than before the fix at every level",
          all(med(new[db]) < med(old[db]) for db in new),
          {db: (med(new[db]), med(old[db])) for db in new})
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
