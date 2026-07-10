"""Deterministic simulation of TtsPipeline's word-sync reveal — no audio
hardware, no threads, no network. Drives the exact sequences that failed
live on 2026-07-10 (Giorgi's screenshots) and asserts the epoch fence holds.

Run:  cd C:/Users/user/goat-standalone/python && python test_reveal.py
"""
import queue
import threading

from goat_app import TtsPipeline

SENT_SAMPLES = 1000  # fake synth output length per sentence


class FakeAudio:
    """Stands in for DuplexAudio: a hand-cranked playback clock."""
    def __init__(self):
        self.played_samples = 0
        self.cleared = 0

    def clear_playback(self):
        self.cleared += 1

    def queue_playback(self, samples):
        pass


def make_pipeline():
    """TtsPipeline without __init__ — no Piper process, no worker thread.
    The worker is simulated by drain(): same unpack + register the real
    worker does, but synchronous and deterministic."""
    p = TtsPipeline.__new__(TtsPipeline)
    p.audio = FakeAudio()
    p.q = queue.Queue()
    p.gen = 0
    p._lock = threading.Lock()
    p._segments = []
    p._queued_end = 0
    p._epoch = 0
    return p


def drain(p, n_samples=SENT_SAMPLES):
    """Simulate the worker synthesizing everything currently queued."""
    while not p.q.empty():
        gen, epoch, text = p.q.get_nowait()
        if gen != p.gen:
            continue
        p._register(text, n_samples, epoch)


def check(name, got, want):
    status = "PASS" if got == want else "FAIL"
    print(f"[{status}] {name}: got {got!r}")
    assert got == want, f"{name}: wanted {want!r}"


# A. Plain turn reveals in order once the clock passes it.
p = make_pipeline()
p.new_turn()
p.say("First sentence.")
p.say("Second sentence.")
drain(p)
p.audio.played_samples = 2 * SENT_SAMPLES
check("A plain turn", p.spoken_text(), "First sentence. Second sentence.")

# B. THE LIVE FAILURE (screenshot 03:26): fast follow-up beat the synth.
# Turn A's sentences were queued but NOT yet synthesized when the mid-turn
# interjection arrived (busy path -> mark_reply). They register AFTER the
# mark — the old sample-position fence let them all leak into B's label.
p = make_pipeline()
p.new_turn()
p.say("The app starts on the fast model.")
p.say("Try typing or speaking something.")
# no drain — Ava hasn't finished a single sentence yet
p.mark_reply()                      # interjection: UI opened a fresh label
p.say("You're right, I may have missed something.")
drain(p)                            # worker now catches up on everything
p.audio.played_samples = 10 * SENT_SAMPLES
check("B interjection before synth", p.spoken_text(),
      "You're right, I may have missed something.")

# C. Barge-in: cancel() freezes the old reply; nothing stale leaks after.
p = make_pipeline()
p.new_turn()
p.say("A long reply he interrupts.")
p.say("Its tail must never resurface.")
drain(p)
p.audio.played_samples = SENT_SAMPLES // 2   # voice mid-sentence-one
p.cancel()                                    # barge-in
check("C spoken empty after cancel", p.spoken_text(), "")
p.new_turn()
p.say("Fresh answer.")
drain(p)
p.audio.played_samples += SENT_SAMPLES
check("C only fresh answer", p.spoken_text(), "Fresh answer.")

# D. Word-by-word: mid-sentence clock shows a prefix, not the whole thing.
p = make_pipeline()
p.new_turn()
p.say("one two three four five six seven eight")
drain(p)
p.audio.played_samples = SENT_SAMPLES // 2
partial = p.spoken_text()
full = "one two three four five six seven eight"
ok = partial and partial != full and full.startswith(partial)
print(f"[{'PASS' if ok else 'FAIL'}] D mid-sentence prefix: got {partial!r}")
assert ok

# E. Zero-length (unspeakable) segments keep their place in order.
p = make_pipeline()
p.new_turn()
p.say("Here is the path.")
drain(p)
p._register("C:/some/path.py", 0, p._epoch)   # what the worker does for code
p.audio.played_samples = SENT_SAMPLES
check("E unspeakable in order", p.spoken_text(),
      "Here is the path. C:/some/path.py")

# F. Double interjection: two marks in a row, only the newest epoch shows.
p = make_pipeline()
p.new_turn()
p.say("Answer one.")
p.mark_reply()
p.say("Answer two.")
p.mark_reply()
p.say("Answer three.")
drain(p)
p.audio.played_samples = 10 * SENT_SAMPLES
check("F double interjection", p.spoken_text(), "Answer three.")

print("\nall reveal simulations passed")
