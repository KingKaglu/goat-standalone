"""Verification harness for the echo-cancellation + interrupt loop — run this
directly, no STT/Claude/TTS pipeline wired up yet (on purpose, per the
incremental-build instruction).

Test checklist:
  (a) At full/boosted speaker volume, say nothing — GOAT should never
      interrupt itself.
  (b) Talk over it at normal conversational volume — it should stop within
      ~300ms, no yelling required.
  (c) When it stops, check the preroll printout — your first word should be
      in the "captured before interrupt" window, not clipped.

Ctrl+C to stop.
"""
import time

from audio_io import DuplexAudio, SAMPLE_RATE
from tts_piper import synth_to_16k

TEST_TEXT = (
    "This is a test of the echo cancellation loop. "
    "I'm going to keep talking for a while so you have time to try "
    "interrupting me at a normal, conversational volume, without shouting. "
    "If this echo canceller is working, my own voice should never trigger "
    "a false interrupt, no matter how loud your speakers are turned up. "
    "But your real voice, at normal volume, should cut me off quickly."
)

WARMUP_TEXT = (
    "Give me a moment to learn this room's echo before we start testing. "
    "Please stay quiet while I talk to myself for a few seconds."
)


def on_interrupt(preroll_audio):
    ms = len(preroll_audio) / SAMPLE_RATE * 1000
    print(f"  -> captured {ms:.0f}ms of preroll audio before the interrupt (check your first word isn't clipped)")


def main():
    print("Synthesizing test speech with piper...")
    speech = synth_to_16k(TEST_TEXT)
    warmup_speech = synth_to_16k(WARMUP_TEXT)
    print(f"Got {len(speech) / SAMPLE_RATE:.1f}s of audio.\n")

    audio = DuplexAudio(on_interrupt=on_interrupt)
    audio.start()
    audio.calibrate(seconds=2.0)

    print("--- AEC warm-up: letting the filter learn this room's echo path, "
          "stay quiet ---")
    audio.warming_up = True
    audio.queue_playback(warmup_speech.copy())
    while audio.is_tts_playing:
        time.sleep(0.1)
    audio.warming_up = False
    print("--- warm-up done — real test starts now ---\n")

    try:
        while True:
            print("--- playing test speech — try talking over it, or just listen at high volume ---")
            audio.queue_playback(speech.copy())
            while audio.is_tts_playing:
                time.sleep(0.1)
            print("--- done — 3s of quiet before the next round ---\n")
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    finally:
        audio.stop()


if __name__ == "__main__":
    main()
