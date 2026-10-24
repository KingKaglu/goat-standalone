"""LIVE barge-in check on the real mic and speakers — bounded, ~40 s total.

GOAT's voice (piper) plays clause by clause with streaming gaps, exactly as
the voice speaks; a recording of Giorgi plays from the same speakers on a
second stream, which GOAT's echo canceller does not know about — to GOAT it
is a person in the room talking over it. Reports whether and how fast GOAT
stopped. Run 1 is a control (nobody talks: must NOT stop).

Close GOAT first (it would answer the recording). Plays sound out loud.
Run: py -3.13 probe_barge_live.py   (a probe, not a test: it plays sound —
never part of a test sweep)

Measured 2026-10-24 night: control never cut itself off; with the hold
~9/12 starts stopped GOAT in 1.2-1.4 s, without it 4/6. A voice played
from GOAT's own speakers is mostly stripped as echo and sits at the
detection edge, so this probe is pessimistic next to a real person.
"""
from __future__ import annotations

import os
import time

import numpy as np
import sounddevice as sd
import soundfile as sf

from audio_io import DuplexAudio, SAMPLE_RATE
from tts_piper import synth_to_16k

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIM = os.path.join(ROOT, "python", "stt-debug", "20261021-000346.wav")
CLAUSES = ["Sir,", "the build finished a minute ago,", "and every test is green.",
           "Claude also cleaned up the old logs,", "so the disk has room again.",
           "Shall I start on the next phase now,", "or would you rather look first?"]
GAP_S = 0.35
GOAT_GAIN = 0.3


def main():
    import audio_io
    if os.environ.get("GOAT_TEST_HOLD"):            # comparison runs only
        audio_io.SPEAKING_HOLD_S = float(os.environ["GOAT_TEST_HOLD"])
    # A person at the laptop is closer to the mic than its speakers are, and
    # louder than the echo (measured 2026-10-24: speaker->mic voice rms
    # 0.0069 vs room 0.0010). GOAT plays ~10 dB under "him" to model that.
    voice = [synth_to_16k(c) * GOAT_GAIN for c in CLAUSES]
    him, sr = sf.read(HIM, dtype="float32")
    him = (him if him.ndim == 1 else him[:, 0])[: int(sr * 4)]
    cuts = []
    a = DuplexAudio(on_interrupt=lambda pre: cuts.append(time.monotonic()))
    # The "person" here comes out of the same speakers, and GOAT also cancels
    # whatever else the speakers play (videos, music) through the loopback
    # tap — so in every clause gap the recording would be erased as echo,
    # which a real voice in the room never is. Without the tap, the
    # recording is what a person is to GOAT: sound it has no reference for.
    a.loopback.take = lambda n: None
    a.start()
    try:
        a.calibrate(seconds=2.0)
        a.warming_up = True                       # let the AEC learn the room
        a.queue_playback(synth_to_16k("Learning the room for a moment, stay quiet."))
        while a.is_tts_playing:
            time.sleep(0.05)
        a.warming_up = False
        time.sleep(0.8)
        results = []
        for run, him_at in enumerate([None, 1.2, 2.0, 2.9]):
            cuts.clear()
            a._utt_chunks = None
            t0 = time.monotonic()
            started = None
            for i, clip in enumerate(voice):
                if cuts:
                    break
                a.queue_playback(clip)
                while a.is_tts_playing and not cuts:
                    if him_at is not None and started is None and time.monotonic() - t0 >= him_at:
                        sd.play(him, sr)               # "him", talking over GOAT
                        started = time.monotonic()
                    time.sleep(0.01)
                end_gap = time.monotonic() + GAP_S     # next clause still synthesising
                while time.monotonic() < end_gap and not cuts:
                    if him_at is not None and started is None and time.monotonic() - t0 >= him_at:
                        sd.play(him, sr)
                        started = time.monotonic()
                    time.sleep(0.01)
            sd.stop()
            if him_at is None:
                results.append(("control (nobody talks)", not cuts,
                                "kept talking" if not cuts else "CUT ITSELF OFF"))
            else:
                ok = bool(cuts) and started is not None
                d = (cuts[0] - started) if ok else None
                results.append((f"he talks at {him_at:.1f}s", ok and d < 1.5,
                                f"stopped {d:.2f}s after he started" if ok else "never stopped"))
            a.clear_playback()
            time.sleep(1.5)
        fails = 0
        for name, ok, what in results:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {what}")
            fails += not ok
        print(f"\n{len(results) - fails} passed, {fails} failed")
    finally:
        a.stop()


if __name__ == "__main__":
    main()
