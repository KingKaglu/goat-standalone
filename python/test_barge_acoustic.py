"""Barge-in through GOAT's REAL audio path, offline: WebRTC echo canceller +
Silero VAD + the vote, fed 10 ms blocks with a simulated clock.

GOAT's voice plays as clauses with streaming gaps between them (how the
voice speaks); the "mic" hears the room echo of it, and from 2.0 s on, a
real recording of Giorgi talking over it. Checks: no self-interrupt on its
own echo, and his voice stops GOAT — with SPEAKING_HOLD_S as shipped, and
(for contrast) with the hold off, which is how it behaved before
2026-10-24.

No microphone or speakers are used. Run: py -3.13 test_barge_acoustic.py
"""
from __future__ import annotations

import os
import sys
import types

import numpy as np
import soundfile as sf

import audio_io
from audio_io import BLOCK_SAMPLES, DuplexAudio, SAMPLE_RATE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOAT_VOICE = os.path.join(ROOT, "inbox", "ultron-en.wav")
HIM = os.path.join(ROOT, "python", "stt-debug", "20261021-000346.wav")

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


def load16k(path):
    a, sr = sf.read(path, dtype="float32")
    a = a if a.ndim == 1 else a[:, 0]
    if sr != SAMPLE_RATE:
        n = int(len(a) * SAMPLE_RATE / sr)
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    return a


class Clock:
    """Stands in for audio_io's `time`: simulated monotonic time."""
    def __init__(self):
        self.t = 1000.0
        self._real = __import__("time")

    def monotonic(self):
        return self.t

    def __getattr__(self, k):
        return getattr(self._real, k)


def run(hold_s: float, him_at_s: float | None, gap_ms: int = 350, seconds: float = 7.0,
        echo_gain: float = 0.35, him_gain: float = 0.6):
    if not (os.path.exists(GOAT_VOICE) and os.path.exists(HIM)):
        print("[SKIP] recordings missing")
        return None
    clock = Clock()
    audio_io.time = clock
    audio_io.SPEAKING_HOLD_S = hold_s
    cuts, status = [], []
    a = DuplexAudio(on_interrupt=lambda pre: cuts.append(clock.t), on_status=status.append)
    a.warming_up = False
    voice = load16k(GOAT_VOICE)
    # clause-sized pieces of GOAT's voice, ~0.6-0.9 s each
    clauses, i = [], 0
    while i < len(voice):
        n = int(SAMPLE_RATE * (0.6 + 0.3 * (len(clauses) % 2)))
        clauses.append(voice[i:i + n]); i += n
    clauses = clauses * 6
    him = load16k(HIM) * him_gain
    rng = np.random.default_rng(7)
    n_blocks = int(seconds * SAMPLE_RATE / BLOCK_SAMPLES)
    echo_hist = np.zeros(SAMPLE_RATE, dtype=np.float32)  # what left the speaker
    dry_at, ci, t0 = -1.0, 0, clock.t
    gaps = 0
    outdata = np.zeros((BLOCK_SAMPLES, 1), dtype=np.float32)
    first_cut = None
    for b in range(n_blocks):
        now = b * BLOCK_SAMPLES / SAMPLE_RATE
        # the voice streams: the next clause arrives gap_ms after the buffer
        # ran dry (the next sentence still being synthesised)
        if not cuts and ci < len(clauses) and len(a._playback_buf) == 0:
            if dry_at is None:
                dry_at = now
                gaps += 1
            if now >= dry_at + gap_ms / 1000.0:
                a.queue_playback(clauses[ci]); ci += 1
                dry_at = None
        # speaker output this block = what the callback writes; echo = delayed, attenuated
        mic = rng.normal(0, 0.002, BLOCK_SAMPLES).astype(np.float32)
        delay = int(0.03 * SAMPLE_RATE)
        mic += echo_gain * echo_hist[-delay - BLOCK_SAMPLES:-delay]
        if him_at_s is not None and now >= him_at_s:
            k = int((now - him_at_s) * SAMPLE_RATE)
            seg = him[k:k + BLOCK_SAMPLES]
            mic[:len(seg)] += seg
        clock.t = t0 + now
        a._callback(mic.reshape(-1, 1), outdata, BLOCK_SAMPLES, None, None)
        echo_hist = np.concatenate([echo_hist[BLOCK_SAMPLES:], outdata[:, 0].copy()])
        while not a._cleaned_q.empty():
            cleaned, was_playing, raw = a._cleaned_q.get()
            a._preroll.append(cleaned)
            a._recent.append(cleaned)
            a._run_vad(cleaned, was_playing)
        if cuts and first_cut is None:
            first_cut = now
    audio_io.time = clock._real
    status.append(f"(clause gaps: {gaps})")
    return first_cut, status


saved_hold = audio_io.SPEAKING_HOLD_S
got = run(saved_hold, None)
if got is not None:
    cut, st = got
    check("GOAT never cuts itself off on its own echo (no one talking)", cut is None,
          f"cut at {cut}s; {st[-3:]}")
    # His voice starting at 12 different moments, so some starts straddle a
    # clause gap — the case that failed before (2026-10-24 sweep through this
    # same pipeline: 8/16 and 7/16 stopped before the fix, 16/16 after).
    new, old = [], []
    for k in range(12):
        at = 1.0 + k * 0.17
        cut, _ = run(saved_hold, at, seconds=at + 2.0)
        new.append(None if cut is None else round(cut - at, 2))
        cut, _ = run(0.0, at, seconds=at + 2.0)
        old.append(None if cut is None else round(cut - at, 2))
    check("his voice stops GOAT at every start, through clause gaps, within 1.2 s",
          all(d is not None and d < 1.2 for d in new), new)
    print(f"[info] same starts before the fix (no hold): {old}")
audio_io.SPEAKING_HOLD_S = saved_hold
print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
