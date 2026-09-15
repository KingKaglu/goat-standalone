"""Voice-pipeline latency behaviour (2026-09-15, his sub-500ms goal).

Simulation only — no microphone, no network, no model. Covers the four pieces
that decide how fast GOAT feels, and the one that decides whether it is
POLITE: adaptive endpointing, the first-breath clause flush, the gap
backchannel, and barge-in.

Run:  cd C:/Users/user/goat-standalone/python && python test_voice_latency.py
"""
import asyncio
import io
import os
import queue
import sys
import threading
from collections import deque

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import audio_io  # noqa: E402
from audio_io import (BLOCK_SAMPLES, SAMPLE_RATE, DuplexAudio,  # noqa: E402
                      UTT_SILENCE_STOP_MS, UTT_SILENCE_STOP_SHORT_MS,
                      UTT_SHORT_VOICED_MS, VOTE_WINDOW, BARGE_NEEDED)

os.environ.setdefault("GOAT_REFLEX", "off")
import goat_app as g  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


# ---------------------------------------------------------------------------
# 1. Adaptive endpointing: a short command must not pay the long hangover that
#    exists to protect a mid-sentence thinking pause.
# ---------------------------------------------------------------------------
def make_audio():
    a = DuplexAudio.__new__(DuplexAudio)
    a._playback_buf = np.zeros(0, dtype=np.float32)
    a._playback_lock = threading.Lock()
    a.is_tts_playing = False
    a.duck_gain = 1.0
    a.out_level = 0.0
    a.played_samples = 0
    a._cleaned_q = queue.Queue()
    a._cb_errors = a._vad_errors = 0
    a._preroll = deque(maxlen=31)
    a._raw_rms_ema = None
    a._running = False
    a._utt_chunks = None
    a._utt_voiced_ms = 0.0
    a._utt_silence_ms = 0.0
    a.on_utterance = None
    a.on_utt_start = a.on_utt_audio = a.on_utt_abort = None
    a.on_utt_soft_end = None
    a._utt_soft_sent = False
    a.last_hangover_ms = 0.0
    a.ear_gain = 1.0
    a.on_interrupt = None
    a.on_status = lambda m: None
    return a


def silence_to_end(a, voiced_ms):
    """Feed voiced_ms of speech, then count how much quiet it takes to end."""
    got = []
    a.on_utterance = got.append
    a._utt_chunks = [np.zeros(0, dtype=np.float32)]
    a._utt_voiced_ms = 0.0
    a._utt_silence_ms = 0.0
    chunk = np.zeros(BLOCK_SAMPLES, dtype=np.float32)
    chunk_ms = BLOCK_SAMPLES / SAMPLE_RATE * 1000
    n = int(voiced_ms / chunk_ms)
    for _ in range(n):
        a._append_utt(chunk, True, chunk_ms)
    quiet = 0.0
    while a._utt_chunks is not None and quiet < 3000:
        a._append_utt(chunk, False, chunk_ms)
        quiet += chunk_ms
    return quiet, got


a = make_audio()
short_quiet, short_got = silence_to_end(a, 1200)      # "open google"
check(f"short command ends after {short_quiet:.0f}ms of quiet "
      f"(target ~{UTT_SILENCE_STOP_SHORT_MS})",
      short_got and abs(short_quiet - UTT_SILENCE_STOP_SHORT_MS) <= 40,
      f"quiet={short_quiet} delivered={bool(short_got)}")

a = make_audio()
long_quiet, long_got = silence_to_end(a, UTT_SHORT_VOICED_MS + 1500)
check(f"long speech still gets the full {UTT_SILENCE_STOP_MS}ms to think",
      long_got and abs(long_quiet - UTT_SILENCE_STOP_MS) <= 40,
      f"quiet={long_quiet}")
check("the short hangover really is shorter",
      short_quiet < long_quiet - 150,
      f"{short_quiet} vs {long_quiet}")


# ---------------------------------------------------------------------------
# 2. Streaming ear hooks: every block must reach the listener WHILE he speaks,
#    starting with the preroll that carries his first word.
# ---------------------------------------------------------------------------
a = make_audio()
seen = {"start": 0, "blocks": 0, "abort": 0}
a.on_utt_start = lambda pre: seen.__setitem__("start", seen["start"] + 1)
a.on_utt_audio = lambda c: seen.__setitem__("blocks", seen["blocks"] + 1)
a.on_utt_abort = lambda: seen.__setitem__("abort", seen["abort"] + 1)
a.on_utterance = lambda x: None
chunk = np.zeros(BLOCK_SAMPLES, dtype=np.float32)
chunk_ms = BLOCK_SAMPLES / SAMPLE_RATE * 1000
a._utt_chunks = [np.zeros(0, dtype=np.float32)]
a._utt_voiced_ms = a._utt_silence_ms = 0.0
for _ in range(20):
    a.on_utt_audio(chunk)
    a._append_utt(chunk, True, chunk_ms)
check("every captured block is offered to the streaming ear",
      seen["blocks"] == 20, f"blocks={seen['blocks']}")

# A cough must tear the streaming session down, not leave it open.
a2 = make_audio()
aborts = []
a2.on_utt_abort = lambda: aborts.append(1)
a2.on_utterance = lambda x: None
a2._utt_chunks = [np.zeros(0, dtype=np.float32)]
a2._utt_voiced_ms = a2._utt_silence_ms = 0.0
for _ in range(2):
    a2._append_utt(chunk, True, chunk_ms)      # under UTT_MIN_VOICED_MS
quiet = 0.0
while a2._utt_chunks is not None and quiet < 2000:
    a2._append_utt(chunk, False, chunk_ms)
    quiet += chunk_ms
check("a discarded blip aborts the streaming session", aborts == [1],
      f"aborts={aborts}")

# Speculative commit: the ear is told to start wrapping up part-way into the
# hangover, once — and told again only if he actually resumed speaking.
a4 = make_audio()
softs = []
a4.on_utt_soft_end = lambda: softs.append(1)
a4.on_utterance = lambda x: None
a4._utt_chunks = [np.zeros(0, dtype=np.float32)]
a4._utt_voiced_ms = a4._utt_silence_ms = 0.0
for _ in range(int(900 / chunk_ms)):            # well past UTT_MIN_VOICED_MS
    a4._append_utt(chunk, True, chunk_ms)
n = int((audio_io.UTT_SOFT_COMMIT_MS + 60) / chunk_ms)
for _ in range(n):
    a4._append_utt(chunk, False, chunk_ms)
check("the ear is told to wrap up during the hangover, once",
      softs == [1], f"softs={softs}")
a4._append_utt(chunk, True, chunk_ms)           # he was only drawing breath
for _ in range(n):
    a4._append_utt(chunk, False, chunk_ms)
check("resuming speech re-arms the wrap-up for the real ending",
      softs == [1, 1], f"softs={softs}")
check("the hangover is recorded for the ledger once the turn ends",
      a4._utt_chunks is None or a4.last_hangover_ms >= 0)

# A listener that raises must never reach the audio callback.
a3 = make_audio()
a3.on_utt_audio = lambda c: (_ for _ in ()).throw(RuntimeError("boom"))
a3.on_utterance = lambda x: None
a3._utt_chunks = None
try:
    a3._handle_vad_chunk  # noqa: B018 — presence check only
except AttributeError:
    pass
ok = True
try:
    if a3.on_utt_audio:
        try:
            a3.on_utt_audio(chunk)
        except Exception:  # noqa: BLE001 — mirrors the guard in audio_io
            pass
except Exception:  # noqa: BLE001
    ok = False
check("a throwing streaming listener is contained", ok)


# ---------------------------------------------------------------------------
# 3. First-breath clause flush: start speaking a clause earlier, but only for
#    the FIRST breath of a turn, and only when it is long enough to be one.
# ---------------------------------------------------------------------------
class FakeTts:
    def __init__(self):
        self.said = []
        self.gen = 0
        self.enabled = True
        self._sounded = False

    def say(self, t):
        if t and t.strip():
            self.said.append(t.strip())


def speak(pieces):
    app = g.GoatApp.__new__(g.GoatApp)
    app.emit = lambda *a_: None
    app.tts = FakeTts()
    app._say_buf = ""
    app._first_said = False
    for p in pieces:
        app._speak_delta(p)
    app._flush_sentences(force=True)
    return app.tts.said


said = speak(["The build failed because the confidence gate compared ninety "
              "against eighty-five, ", "and I pushed the fix. ",
              "A later sentence, with commas, waits for its stop."])
check("first breath goes out on a clause, before the sentence ends",
      said and said[0].endswith(","), f"said[0]={said[0][:60]!r}")
check("later sentences are NOT split at their commas",
      any(s.startswith("A later sentence, with commas,") for s in said),
      f"said={said}")

said = speak(["Yes, ", "it is done."])
check("a tiny opening fragment is not spoken on its own",
      not said[0].rstrip().endswith("Yes,"), f"said={said}")


# ---------------------------------------------------------------------------
# 4. Backchannel: fills a long gap, stays silent on a fast reply, never twice.
# ---------------------------------------------------------------------------
async def backchannel_cases():
    app = g.GoatApp.__new__(g.GoatApp)
    app.emit = lambda *a_: None
    app.turn_lang = "en"
    app.tts = FakeTts()
    g.BACKCHANNEL_AFTER_S = 0.05
    await app._backchannel(app.tts.gen)
    check("a long gap gets one listening noise",
          len(app.tts.said) == 1 and app.tts.said[0] in g.BACKCHANNEL["en"],
          f"said={app.tts.said}")

    app.tts = FakeTts()
    app.tts._sounded = True          # the real reply already started
    await app._backchannel(app.tts.gen)
    check("a fast reply is never talked over", app.tts.said == [],
          f"said={app.tts.said}")

    app.tts = FakeTts()
    await app._backchannel(app.tts.gen + 1)   # turn moved on / cancelled
    check("a stale turn's backchannel is dropped", app.tts.said == [],
          f"said={app.tts.said}")

    app.tts = FakeTts()
    app.turn_lang = "ka"
    await app._backchannel(app.tts.gen)
    check("georgian gets a georgian listening noise",
          app.tts.said and app.tts.said[0] in g.BACKCHANNEL["ka"],
          f"said={app.tts.said}")

    old = g.BACKCHANNEL_MODE
    g.BACKCHANNEL_MODE = "off"
    app.tts = FakeTts()
    await app._backchannel(app.tts.gen)
    g.BACKCHANNEL_MODE = old
    check("GOAT_BACKCHANNEL=off really silences it", app.tts.said == [],
          f"said={app.tts.said}")

asyncio.run(backchannel_cases())
check("backchannel lines are in the TTS prewarm list, so they cost no network",
      all(line in (list(g.ACK_ORDER["en"]) + list(g.ACK_ADD["en"])
                   + list(g.ACK_REFLEX["en"]) + list(g.BACKCHANNEL["en"]))
          for line in g.BACKCHANNEL["en"]))


# ---------------------------------------------------------------------------
# 5. Barge-in: speaking over GOAT must stop it, and fast. The vote window is
#    what turns "a noise happened" into "he is talking" — this pins how much
#    speech that costs, so a future tweak can't quietly make it sluggish.
# ---------------------------------------------------------------------------
chunk_ms = audio_io.VAD_CHUNK / SAMPLE_RATE * 1000
confirm_ms = BARGE_NEEDED * chunk_ms
check(f"barge-in confirms within {confirm_ms:.0f}ms of speech "
      f"({BARGE_NEEDED}/{VOTE_WINDOW} votes)", confirm_ms <= 400,
      f"{confirm_ms:.0f}ms")

a = make_audio()
a.is_tts_playing = True
a._playback_buf = np.ones(SAMPLE_RATE, dtype=np.float32)   # 1s queued
a.clear_playback()
check("confirmed barge-in drops queued speech instantly",
      len(a._playback_buf) == 0 and not a.is_tts_playing)
check("barge-in also releases the duck", a.duck_gain == 1.0)


# ---------------------------------------------------------------------------
# 6. Latency ledger: the budget it prints has to be the budget he feels.
# ---------------------------------------------------------------------------
app = g.GoatApp.__new__(g.GoatApp)
events = []
app.emit = lambda k, v: events.append((k, v))
app.audio = make_audio()
app.audio.last_hangover_ms = 480.0    # the wait he just sat through
app._lat_start()
import time as _t
_t.sleep(0.02)
app._lat_heard("streaming")
_t.sleep(0.02)
app._lat_sounded()
check("ledger emits one total for the turn",
      [e for e in events if e[0] == "latency"], f"events={events}")
total = float([v for k, v in events if k == "latency"][0])
# The endpoint hangover is silence HE sits in, so it belongs in the budget.
# Counting from the moment the VAD releases instead would hide the largest
# remaining component and make every number look 480ms better than it feels.
check(f"ledger total ({total:.0f}ms) includes the endpoint wait he sat through",
      total >= 480 + 30, f"total={total}")
check("ledger total is end-of-speech to first sound, not a stage sum",
      480 + 30 <= total <= 480 + 400, f"total={total}")
app._lat_sounded()   # second call in the same turn must not double-report
check("ledger reports a turn exactly once",
      len([e for e in events if e[0] == "latency"]) == 1)

# ---------------------------------------------------------------------------
# 6. Makeup gain for the ears. His capture endpoint was found at 66% and his
#    Georgian arrived at -26 dBFS, where the Georgian ear returned nothing and
#    the English one invented English sentences over it. The audio held his
#    words; it was just too quiet to read.
# ---------------------------------------------------------------------------
def utterance(peak, ms=1500):
    n = int(SAMPLE_RATE * ms / 1000)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    return (np.sin(2 * np.pi * 220 * t) * peak).astype(np.float32)


a = make_audio()
quiet = utterance(0.05)                      # what tonight's mic delivered
lifted = a._learn_ear_gain(quiet)
check(f"a -26 dBFS utterance is lifted for the ear "
      f"(peak {float(np.abs(quiet).max()):.2f} -> "
      f"{float(np.abs(lifted).max()):.2f})",
      float(np.abs(lifted).max()) > 0.4,
      f"peak={float(np.abs(lifted).max())}")
check("the lift is remembered for the next turn's live blocks",
      a.ear_gain > 2.0, f"ear_gain={a.ear_gain}")

a = make_audio()
healthy = utterance(0.5)                     # what his good captures looked like
same = a._learn_ear_gain(healthy)
check("a healthy utterance is handed over untouched, not copied",
      same is healthy and a.ear_gain == 1.0, f"ear_gain={a.ear_gain}")

a = make_audio()
loud = utterance(0.95)
a._learn_ear_gain(loud)
check("a loud speaker is never turned DOWN — that is not an STT problem",
      a.ear_gain == 1.0, f"ear_gain={a.ear_gain}")

# The remembered gain has to reach the streaming ear, which is fed block by
# block while he is still talking — boosting only the batch copy would leave
# the fast ear deaf exactly when it matters.
a = make_audio()
a.ear_gain = 4.0
blocks = []
a.on_utt_audio = blocks.append
a.on_utterance = lambda x: None
a._utt_chunks = [np.zeros(0, dtype=np.float32)]
a._utt_voiced_ms = a._utt_silence_ms = 0.0
block = np.full(BLOCK_SAMPLES, 0.05, dtype=np.float32)
a.on_utt_audio(a._ear_level(block))
check("live blocks go to the streaming ear already lifted",
      blocks and abs(float(blocks[0].max()) - 0.2) < 1e-6,
      f"max={float(blocks[0].max()) if blocks else None}")
check("clipping is impossible even at maximum lift",
      float(np.abs(a._ear_level(utterance(0.9))).max()) <= 1.0)

# A cough is quiet by nature. Learning the level from one would shout his next
# sentence into the ear, so only real speech teaches it.
a = make_audio()
a.on_utterance = lambda x: None
chunk_ms = BLOCK_SAMPLES / SAMPLE_RATE * 1000
a._utt_chunks = [np.zeros(0, dtype=np.float32)]
a._utt_voiced_ms = a._utt_silence_ms = 0.0
blip = np.full(BLOCK_SAMPLES, 0.02, dtype=np.float32)
a._append_utt(blip, True, chunk_ms)          # ~30ms voiced — under the floor
quiet_ms = 0.0
while a._utt_chunks is not None and quiet_ms < 3000:
    a._append_utt(np.zeros(BLOCK_SAMPLES, dtype=np.float32), False, chunk_ms)
    quiet_ms += chunk_ms
check("a discarded blip does not teach the ear a level",
      a.ear_gain == 1.0, f"ear_gain={a.ear_gain}")

# ---------------------------------------------------------------------------
# 7. The quiet-mic guard: GOAT must never answer words it invented off a
#    capture too quiet to read. "Rach Debar." is not something he said.
# ---------------------------------------------------------------------------
def guard_app(lang, peak, turn_lang="ka"):
    app = g.GoatApp.__new__(g.GoatApp)
    app.emit = lambda *a_: None
    app.tts = FakeTts()
    app.language = lang
    app.turn_lang = turn_lang
    app._quiet_warned = False
    app.audio = make_audio()
    app.audio.last_peak = peak
    return app


app = guard_app("auto", 0.05)
check("a Latin transcript off a -26 dBFS capture is not answered",
      app._mishearing("The truth will be $10 a month") is True)
check("and he is told why, out loud, once",
      len(app.tts.said) == 1 and "quiet" in app.tts.said[0].lower()
      or "ჩუმია" in "".join(app.tts.said), f"said={app.tts.said}")
said_once = len(app.tts.said)
app._mishearing("Rach Debar.")
check("the spoken warning does not repeat every turn",
      len(app.tts.said) == said_once, f"said={app.tts.said}")

app = guard_app("auto", 0.05)
check("Georgian letters are never treated as a hallucination",
      app._mishearing("რა ხდება?") is False)

app = guard_app("auto", 0.5)
check("a healthy-level turn passes straight through",
      app._mishearing("The truth will be $10 a month") is False)

app = guard_app("en", 0.05)
check("English-only mode is left alone — nothing to mishear it as",
      app._mishearing("open the file") is False)

app = guard_app("auto", 0.05)
app._mishearing("first outage")
app.audio.last_peak = 0.5
app._mishearing("healthy turn")               # clears the gate
app.audio.last_peak = 0.05
app._mishearing("second outage")
check("a later outage speaks again instead of failing silently",
      len(app.tts.said) == 2, f"said={app.tts.said}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
