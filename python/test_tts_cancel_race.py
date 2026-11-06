"""A barge-in can't let a stale sentence through (2026-11-06 audit B2), and
the Windows "peer hung up" noise is one log line (B3). No audio device.
Run: py -3.13 test_tts_cancel_race.py"""
import concurrent.futures as cf
import queue
import sys
import threading
import time

import numpy as np

import goat_app as g

fails = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  — {info}" if info and not ok else ""))
    if not ok:
        fails.append(name)


class FakeAudio:
    def __init__(self):
        self.played_samples = 0
        self.queued = []
        self.cleared = 0

    def queue_playback(self, s):
        self.queued.append(len(s))

    def clear_playback(self):
        self.cleared += 1
        self.queued.clear()


def make_tts():
    t = g.TtsPipeline.__new__(g.TtsPipeline)
    t.audio = FakeAudio()
    t.emit = lambda *a: None
    t._lock = threading.Lock()
    t.gen = 0
    t.q = queue.Queue()
    t._ordered = queue.Queue()
    t._synth_cache = {}
    t.gain = 1.0
    t._segments = []
    t._queued_end = 0
    t._epoch = 0
    t.enabled = True
    t.on_first_audio = t.on_filler_audio = t.on_cancel = None
    t._sounded = False
    t.local_mute = False
    t.muted_until = 0.0
    threading.Thread(target=t._player, daemon=True).start()
    return t


def say(t, text, n=1600):
    f = cf.Future()
    f.set_result(np.zeros(n, dtype=np.float32))
    t._ordered.put((t.gen, t._epoch, text, False, f))


def settle(t):
    for _ in range(100):
        if t._ordered.empty():
            break
        time.sleep(0.01)
    time.sleep(0.05)


def main() -> int:
    # normal: a sentence plays
    t = make_tts()
    t.on_sentence = None
    say(t, "Hello there.")
    settle(t)
    check("a sentence plays", t.audio.queued == [1600], t.audio.queued)

    # the race: he barges in at the exact moment the phone tap runs (after
    # the old gen check, before the speaker was fed)
    t = make_tts()
    t.on_sentence = lambda text, s: t.cancel()
    say(t, "This was being said when he talked over me.")
    settle(t)
    check("barge-in during the tap: nothing reaches the speaker",
          t.audio.queued == [] and t.audio.cleared == 1, (t.audio.queued, t.audio.cleared))
    t.on_sentence = None
    say(t, "The new answer.")
    settle(t)
    check("the next answer still plays", t.audio.queued == [1600], t.audio.queued)

    # B3: the loop handler
    class Loop:
        def __init__(self):
            self.forwarded = []

        def default_exception_handler(self, ctx):
            self.forwarded.append(ctx)
    lp = Loop()
    g._loop_errors(lp, {"message": "Exception in callback "
                        "_ProactorBasePipeTransport._call_connection_lost()",
                        "exception": ConnectionResetError(10054, "forcibly closed")})
    check("WinError 10054 from the proactor: one line, no traceback", lp.forwarded == [])
    g._loop_errors(lp, {"message": "Task exception was never retrieved",
                        "exception": ValueError("real bug")})
    check("a real error still gets the full report", len(lp.forwarded) == 1)
    g._loop_errors(lp, {"message": "socket send failed",
                        "exception": ConnectionResetError()})
    check("a reset elsewhere is still reported", len(lp.forwarded) == 2)

    print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
