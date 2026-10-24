"""Barge-in through the gaps between GOAT's spoken clauses.

His report 2026-10-24: "make sure I'm able to interrupt Gemini's talk". The
voice streams clause by clause; between clauses the speaker buffer runs dry
for a few hundred ms. Each gap used to wipe the barge vote and let the quiet
3-of-4 vote start a capture without stopping playback — so the next clause
played right over him. audio_io.SPEAKING_HOLD_S bridges the gaps.

Run: py -3.13 test_barge_gaps.py
"""
from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np

import audio_io
from audio_io import DuplexAudio, VAD_CHUNK, SAMPLE_RATE, VOTE_WINDOW

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


CHUNK_MS = VAD_CHUNK / SAMPLE_RATE * 1000
CHUNK = np.zeros(VAD_CHUNK, dtype=np.float32)


def make_audio():
    a = DuplexAudio.__new__(DuplexAudio)
    a._playback_buf = np.zeros(0, dtype=np.float32)
    a._playback_lock = threading.Lock()
    a.is_tts_playing = False
    a.duck_gain = 1.0
    a._duck_active = False
    a._veto_quiet_until = 0.0
    a.music_on = False
    a.voice_veto = None
    a._recent = deque(maxlen=120)
    a._vote_hist = deque(maxlen=VOTE_WINDOW)
    a._vote_hist_state = None
    a._preroll = deque(maxlen=31)
    a._utt_chunks = None
    a._utt_voiced_ms = a._utt_silence_ms = 0.0
    a._utt_soft_sent = False
    a.last_hangover_ms = 0.0
    a.ear_gain = 1.0
    a.sentence_state = None
    a.on_utterance = None
    a.on_utt_start = a.on_utt_audio = a.on_utt_abort = a.on_utt_soft_end = None
    a.cuts = []
    a.on_interrupt = lambda pre: a.cuts.append(time.monotonic())
    a.status = []
    a.on_status = a.status.append
    return a


def feed(a, n: int, voiced: bool, playing):
    """n VAD chunks; `playing` is a bool or a callable(i) -> bool."""
    for i in range(n):
        p = playing(i) if callable(playing) else playing
        if p:
            a._play_last_t = time.monotonic()   # what the audio callback does
        a._on_vad_chunk(CHUNK, voiced, CHUNK_MS, was_playing=p)


# 1. Him talking over a voice whose buffer empties every ~4 chunks (a clause
#    gap of ~130 ms) — before the fix the vote was wiped at every gap.
a = make_audio()
feed(a, 30, True, lambda i: i % 6 < 4)
check("talking over a clause-by-clause voice stops it", len(a.cuts) == 1, a.status[-3:])

# 2. A quiet gap must not start a capture that leaves the voice playing.
a = make_audio()
feed(a, 3, False, True)          # GOAT talking, he silent
feed(a, 4, True, False)          # 128 ms gap — he says one syllable
check("a syllable in a clause gap doesn't start a capture",
      a._utt_chunks is None and not a.cuts, (a._utt_chunks is not None, a.cuts))

# 3. A short "mm-hm" over the voice still doesn't cut it (his 2026-10-17 order).
a = make_audio()
feed(a, 8, True, True)
feed(a, 20, False, True)
check("an 'mm-hm' over the voice keeps it talking", not a.cuts)

# 4. Once GOAT has really finished (hold over), he starts a turn fast as before.
a = make_audio()
a._play_last_t = time.monotonic() - audio_io.SPEAKING_HOLD_S - 0.1
feed(a, 3, True, False)
check("after GOAT has finished, 3 chunks start his turn", a._utt_chunks is not None
      and not a.cuts)

# 5. He was already talking when GOAT started speaking over him.
a = make_audio()
feed(a, 3, True, False)                       # his turn starts
assert a._utt_chunks is not None
feed(a, 20, True, True)                       # GOAT starts talking over him
check("GOAT starting to talk over him gets cut too", len(a.cuts) == 1, a.status[-3:])

# 6. A barge clears the hold: the follow-up capture isn't treated as playback.
a = make_audio()
a._play_last_t = time.monotonic()
a.clear_playback()
check("clear_playback ends the hold", a._play_last_t == 0.0)

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
