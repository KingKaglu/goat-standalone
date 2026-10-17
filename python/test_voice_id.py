"""Voice lock (voice_id.py) — front end, decisions, enrollment, real voices,
the barge-in veto and the engine's door.

Run: py -3.13 test_voice_id.py   (real-voice checks skip when the saved
clips / eval voices / models are missing)
"""
import asyncio
import glob
import os
import sys
import tempfile
import time
from collections import deque

import numpy as np

import voice_id as V

HERE = os.path.dirname(os.path.abspath(__file__))
MINE = sorted(glob.glob(os.path.join(HERE, "stt-debug", "*.wav")))
LIBRI = sorted(glob.glob(os.path.join(V.ROOT, "eval", "libri", "*.wav")))
fails = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        fails.append(name)


def sandbox():
    """Point config/prints/log at a temp dir so tests never touch his print."""
    d = tempfile.mkdtemp(prefix="vid-")
    V.CONFIG = os.path.join(d, "config.json")
    V.PRINTS = os.path.join(d, "prints.npz")
    V.LOG = os.path.join(d, "voice-id.log")
    return d


def load(p):
    import soundfile as sf
    x, sr = sf.read(p, dtype="float32", always_2d=True)
    assert sr == 16000
    return x.mean(axis=1)


def t_fbank():
    try:
        import torch
        import torchaudio.compliance.kaldi as K
    except ImportError:
        print("skip fbank-vs-torchaudio (no torch)")
        return
    rng = np.random.default_rng(0)
    x = (rng.standard_normal(16000) * 0.1).astype(np.float32)
    for w in ("hamming", "povey"):
        ref = K.fbank(torch.from_numpy(x[None]) * 32768, num_mel_bins=80,
                      frame_length=25, frame_shift=10, dither=0.0,
                      sample_frequency=16000, window_type=w, use_energy=False).numpy()
        mine = V.fbank(x, w)
        check(f"fbank matches torchaudio ({w})",
              ref.shape == mine.shape and np.abs(ref - mine).max() < 2e-3,
              f"max diff {np.abs(ref - mine).max():.1e}")
    check("fbank: too short -> no frames", V.fbank(np.zeros(399)).shape == (0, 80))


def t_decisions():
    a_long, r_long = V.bands(3.0)
    a_short, r_short = V.bands(V.SHORT_S)
    check("bands: short speech gets lower bars", a_short < a_long and r_short < r_long)
    a_b, r_b = V.bands(3.0, borrowed=True)
    check("bands: borrowed print gets lower bars", a_b < a_long and r_b < r_long)
    mk = lambda d: V.Verdict(d, 1.0, 2.0, "laptop")  # noqa: E731
    check("allow: me passes", V.allow(mk("me"), False, False))
    check("allow: no print yet passes", V.allow(mk("unknown"), False, False))
    check("allow: not him never passes, even named",
          not V.allow(mk("not_me"), True, True))
    check("allow: unsure blocked when idle", not V.allow(mk("unsure"), False, False))
    check("allow: unsure passes mid-conversation", V.allow(mk("unsure"), True, False))
    check("allow: unsure passes when named", V.allow(mk("unsure"), False, True))


def t_real():
    if len(MINE) < 10 or len(LIBRI) < 5 or V.get() is None:
        print("skip real voices (clips, eval voices or models missing)")
        return
    sandbox()
    vid = V.VoiceID()
    vid._ready()
    E = [vid.embed(load(f))[0] for f in MINE]
    held, train = E[-5:], E[:-5]
    for e in train:
        vid._add("laptop", "anchors", e, V.MAX_ANCHORS)
    vid._save_prints()
    vid.set_lock(True)
    vs = [vid.check(load(f)) for f in MINE[-5:]]
    check("his held-out clips: none rejected",
          all(v.decision != "not_me" for v in vs),
          ", ".join(f"{v.decision}:{v.score:.1f}" for v in vs if v.score is not None))
    check("his held-out clips: most accepted",
          sum(v.decision == "me" for v in vs) >= 4)
    others = []
    for f in LIBRI[:8]:
        x = load(f)
        others.append(vid.check(x[16000:16000 + 40000]))   # a 2.5s cut
    check("other voices: none accepted",
          all(v.decision != "me" for v in others),
          ", ".join(f"{v.decision}:{v.score:.1f}" for v in others if v.score is not None))
    check("other voices: mostly rejected outright",
          sum(v.decision == "not_me" for v in others) >= 6)
    check("check is fast", max(v.ms for v in vs + others) < 800,
          f"max {max(v.ms for v in vs + others):.0f}ms")
    # The phone borrows the laptop print, then grows its own from strong turns.
    v = vid.check(load(MINE[-1]), "phone")
    check("phone without a print borrows the laptop's", v.borrowed and v.score is not None)
    best = max(MINE, key=lambda f: len(load(f)))
    for _ in range(V.GROW_ANCHORS):
        v = vid.check(load(best), "phone")
        v.score, v.decision, v.speech_s = V.ACCEPT + 1, "me", 2.0   # force a strong turn
        vid.adapt(v)
    check("phone grows its own print from strong turns", vid.enrolled("phone"))
    # Barge-in veto: someone else = veto, him = no veto.
    check("barge-in: other voice vetoed",
          vid.clearly_not_him(load(LIBRI[0])[16000:36000]))
    check("barge-in: his voice not vetoed", not vid.clearly_not_him(load(MINE[-2])))
    vid.set_lock(False)
    check("barge-in: lock off never vetoes",
          not vid.clearly_not_him(load(LIBRI[0])[16000:36000]))


def t_enroll():
    if len(MINE) < 10 or V.get() is None:
        print("skip enrollment (clips or models missing)")
        return
    sandbox()
    vid = V.VoiceID()
    check("fresh store: not enrolled, lock off", not vid.enrolled("laptop") and not vid.locked)
    vid.start_enroll(None)
    check("enrolling on any mic", vid.enrolling("laptop") and vid.enrolling("phone"))
    clips = [load(f) for f in MINE] * 2
    done = False
    for x in clips:
        got, done = vid.enroll_feed(x, "laptop")
        if done:
            break
    check("enrollment finishes after ~20s of speech", done, f"{got:.0f}s")
    check("enrollment locks and stores anchors",
          vid.locked and vid.enrolled("laptop") and not vid.enrolling("laptop"))
    again = V.VoiceID()
    check("print survives a restart", again.enrolled("laptop") and again.locked)
    vid._write_cfg(enroll="phone", enroll_at=time.time() + 1)
    check("CLI request starts enrollment for that mic",
          vid.enrolling("phone") and not vid.enrolling("laptop"))
    vid.cancel_enroll()
    check("cancel stops enrollment", not vid.enrolling("phone"))
    vid.forget()
    check("forget all clears print and lock", not vid.enrolled("laptop") and not vid.locked)


def t_veto_hook():
    import audio_io as A
    d = A.DuplexAudio.__new__(A.DuplexAudio)
    d._vote_hist, d._vote_hist_state = deque(maxlen=A.VOTE_WINDOW), None
    d.music_on, d._duck_active, d.duck_gain = False, False, 1.0
    d._utt_chunks, d.on_utt_audio, d.on_utt_start = None, None, None
    d.on_status, d.ear_gain = (lambda m: None), 1.0
    d._preroll, d._recent = deque(maxlen=31), deque(maxlen=120)
    d._veto_quiet_until = 0.0
    hits = []
    d.on_interrupt = lambda pre: hits.append(1)
    d.clear_playback = lambda: hits.append("clear")
    chunk = np.zeros(512, np.float32)
    d._recent.append(chunk)

    def run(veto, n=A.VOTE_WINDOW):
        hits.clear()
        d._utt_chunks = None
        d._veto_quiet_until = 0.0
        d._duck_active, d.duck_gain = False, 1.0
        d._vote_hist.clear()
        d.voice_veto = veto
        for _ in range(n):
            d._on_vad_chunk(chunk, True, 32.0, was_playing=True)
            if d._utt_chunks is not None or hits:
                break
        return hits[:], d._utt_chunks is not None

    h, cap = run(lambda a: True, n=A.VOTE_WINDOW * 2)
    check("veto: someone else doesn't stop GOAT", not h and not cap)
    check("veto: and doesn't keep dimming GOAT's voice", d.duck_gain == 1.0)
    h, cap = run(lambda a: False)
    check("no veto: barge-in stops GOAT", 1 in h and cap)
    h, cap = run(None)
    check("no lock: barge-in as before", 1 in h and cap)

    def boom(a):
        raise RuntimeError("model died")
    h, cap = run(boom)
    check("broken veto never blocks barge-in", 1 in h and cap)


def t_engine():
    import goat_app as g

    class FakeVid:
        def __init__(self):
            self.lock, self.started, self.adapted = True, None, []
            self.prints = {"laptop": {}}

        @property
        def locked(self):
            return self.lock

        def set_lock(self, on):
            self.lock = on

        def enrolled(self, s):
            return True

        def start_enroll(self, s):
            self.started = s

        def forget(self, s=None):
            self.lock = False

        def adapt(self, v):
            self.adapted.append(v)

    class Audio:
        is_tts_playing = False

    app = g.GoatApp.__new__(g.GoatApp)
    app.events, app.said = [], []
    app.emit = lambda *a: app.events.append(a)
    app._say_now = lambda line: app.said.append(line)
    app.audio, app.vid, app._voice_ok_t = Audio(), FakeVid(), 0.0

    async def gate(decision, text, ok_t=0.0):
        app._voice_ok_t = ok_t
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(V.Verdict(decision, 1.0, 2.0, "laptop"))
        return await app._voice_gate(fut, text)

    async def main():
        check("door: him passes", await gate("me", "open spotify"))
        await asyncio.sleep(0.05)
        check("door: him refreshes the conversation clock",
              time.monotonic() - app._voice_ok_t < 1 and app.vid.adapted)
        check("door: someone else is dropped", not await gate("not_me", "goat, open spotify"))
        check("door: drop shows on the status line",
              any("not your voice" in str(e) for e in app.events))
        check("door: unsure + idle dropped", not await gate("unsure", "yes"))
        check("door: unsure + mid-conversation passes",
              await gate("unsure", "yes", ok_t=time.monotonic()))
        check("door: unsure + named passes", await gate("unsure", "goat, yes"))
        boom = asyncio.get_running_loop().create_future()
        boom.set_exception(RuntimeError("x"))
        check("door: a broken check lets him through", await app._voice_gate(boom, "hi"))

    asyncio.run(main())
    check("cmd: 'learn my voice' starts enrollment",
          app._voice_command("GOAT, learn my voice", "phone") and app.vid.started == "phone")
    check("cmd: 'voice lock off'", app._voice_command("voice lock off", "laptop")
          and not app.vid.lock)
    check("cmd: 'turn on voice lock'", app._voice_command("turn on the voice lock", "laptop")
          and app.vid.lock)
    check("cmd: ordinary talk untouched",
          not app._voice_command("what's the weather tomorrow", "laptop"))
    check("cmd: long sentence about voices untouched", not app._voice_command(
        "can you tell me how the thing that learns my voice works in detail please", "laptop"))
    app.vid = None
    check("cmd: no voice lock installed -> ignored", not app._voice_command("voice lock on", "laptop"))


if __name__ == "__main__":
    t_fbank()
    t_decisions()
    t_veto_hook()
    t_engine()
    t_enroll()
    t_real()
    print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED: ' + ', '.join(fails)}")
    sys.exit(1 if fails else 0)
