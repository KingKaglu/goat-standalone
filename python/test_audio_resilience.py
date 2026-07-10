"""Simulation: exceptions inside the audio callback and the VAD thread must
be survived, not fatal — no hardware, no torch, no livekit needed.

Run:  cd C:/Users/user/goat-standalone/python && python test_audio_resilience.py
"""
import queue
import threading
from collections import deque

import numpy as np

from audio_io import DuplexAudio, BLOCK_SAMPLES


class BoomAec:
    """AEC stand-in that explodes on the first block only."""
    def __init__(self):
        self.calls = 0

    def process_block(self, ref, mic):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated APM failure")
        return mic.copy()


def make_audio():
    a = DuplexAudio.__new__(DuplexAudio)  # no __init__: no stream, no torch
    a.aec = BoomAec()
    a._playback_buf = np.zeros(0, dtype=np.float32)
    a._playback_lock = threading.Lock()
    a.is_tts_playing = False
    a.duck_gain = 1.0
    a.out_level = 0.0
    a.played_samples = 0
    a._cleaned_q = queue.Queue()
    a._cb_errors = 0
    a._vad_errors = 0
    a._preroll = deque(maxlen=31)
    a._raw_rms_ema = None
    a._running = False  # _vad_loop drains the preloaded queue, then exits
    return a


# 1. Callback: first block raises (AEC boom) — must not propagate; second
#    block must process normally and reach the VAD queue.
a = make_audio()
indata = np.ones((BLOCK_SAMPLES, 1), dtype=np.float32) * 0.1
outdata = np.ones((BLOCK_SAMPLES, 1), dtype=np.float32)
a._callback(indata, outdata, BLOCK_SAMPLES, None, None)   # boom, swallowed
assert a._cb_errors == 1, "callback error not counted"
assert float(np.abs(outdata).sum()) == 0.0, "failed block must output silence"
a._callback(indata, outdata, BLOCK_SAMPLES, None, None)   # healthy again
assert a._cleaned_q.qsize() == 1, "healthy block must reach the VAD queue"
print("[PASS] callback survives an AEC explosion and keeps streaming")

# 2. VAD thread: a poisoned queue item must be skipped, not thread-fatal.
a = make_audio()
processed = []
a._run_vad = lambda cleaned, was_playing: processed.append(len(cleaned))
a._cleaned_q.put("garbage that cannot unpack")            # poison
a._cleaned_q.put((np.zeros(BLOCK_SAMPLES, dtype=np.float32), False, 0.01))
a._cleaned_q.put(None)                                     # sentinel: exit
a._vad_loop()                                              # runs to completion
assert a._vad_errors == 1, "vad error not counted"
assert processed == [BLOCK_SAMPLES], "healthy chunk after poison must process"
print("[PASS] vad loop survives a poisoned chunk and keeps detecting")

print("\nall audio resilience checks passed")
