"""Build, seed and measure voice ID on real data (2026-10-18).

  python probe_voiceid.py cohort   embed the other-voices set -> voiceid/cohort.npz
  python probe_voiceid.py eval     honest numbers: half the real voices as the
                                   cohort, the other half + synthetic ones as
                                   impostors, his clips leave-one-out
  python probe_voiceid.py seed     his saved laptop clips -> his laptop print

Genuine: his own saved laptop captures (python/stt-debug/*.wav).
Others: real LibriSpeech voices (voiceid/eval/libri) + synthetic ones (Edge
voices in voiceid/eval/synth, Kokoro, the Ultron clips), cut into
command-length pieces like his. Needs scipy to resample: probe only, never
imported by the app.
"""
import glob
import os
import sys

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

import voice_id as V

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL = os.path.join(V.ROOT, "eval")
MINE = sorted(glob.glob(os.path.join(HERE, "stt-debug", "*.wav")))
LIBRI = sorted(glob.glob(os.path.join(EVAL, "libri", "*.wav")))
SYNTH = sorted(glob.glob(os.path.join(EVAL, "synth", "*.*"))
               + glob.glob(os.path.join(HERE, "..", "workspace", "voice-trial", "*.wav"))
               + glob.glob(os.path.join(HERE, "..", "inbox", "ultron-*.wav")))


def load(p):
    x, sr = sf.read(p, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != 16000:
        g = np.gcd(sr, 16000)
        x = resample_poly(x, 16000 // g, sr // g).astype(np.float32)
    return x


def pieces(x, vad, lens=(0.6, 1.0, 1.5, 2.5)):
    """Command-length cuts from the speech of a long clip."""
    sp = V.speech_only(x, vad)
    out, pos = [], 0
    for L in lens * 3:
        n = int(L * 16000)
        if pos + n > len(sp):
            break
        out.append(sp[pos:pos + n])
        pos += n + 8000
    return out


def embed_all(vid, chunks, trim=True):
    out = []
    for c in chunks:
        sp = V.speech_only(c, vid._vad) if trim else c
        e = vid._embed_speech(sp) if len(sp) >= 0.25 * 16000 else None
        out.append((e, len(sp) / 16000))
    return out


def stack(embs):
    return {t: np.stack([e[t] for e in embs]) for t in V.MODELS}


def cohort():
    vid = V.VoiceID()
    vid._ready()
    pcs = [p for f in LIBRI + SYNTH for p in pieces(load(f), vid._vad)]
    E = [e for e, _ in embed_all(vid, pcs, trim=False) if e is not None]
    np.savez(V.COHORT, **stack(E))
    print(f"cohort: {len(E)} pieces from {len(LIBRI) + len(SYNTH)} voices -> {V.COHORT}")


def seed():
    vid = V.VoiceID()
    vid._ready()
    E = [e for e, s in embed_all(vid, [load(f) for f in MINE]) if e is not None]
    for e in E:
        vid._add("laptop", "anchors", e, V.MAX_ANCHORS)
    vid._save_prints()
    print(f"seeded laptop print with {len(E)} of his clips; {vid.status()}")


def evaluate():
    vid = V.VoiceID()
    vid._ready()
    coh_files, imp_files = LIBRI[::2], LIBRI[1::2] + SYNTH
    coh = [e for e, _ in embed_all(
        vid, [p for f in coh_files for p in pieces(load(f), vid._vad)], trim=False)
        if e is not None]
    vid.cohort = stack(coh)
    imps = embed_all(vid, [p for f in imp_files for p in pieces(load(f), vid._vad)],
                     trim=False)
    imps = [(e, s) for e, s in imps if e is not None]
    mine = embed_all(vid, [load(f) for f in MINE])
    keep = [i for i, (e, _) in enumerate(mine) if e is not None]

    def verdict(e, secs):
        s, b = vid.score(e, "laptop")
        acc, rej = V.bands(secs, b)
        d = ("unsure" if secs < V.SHORT_S else
             "me" if s >= acc else "not_me" if s < rej else "unsure")
        return s, d

    print(f"cohort {len(coh)} pieces / {len(coh_files)} voices; impostors "
          f"{len(imps)} pieces / {len(imp_files)} voices; his clips {len(keep)}")
    gen = []
    for i in keep:
        vid.prints = {}
        for j in keep:
            if j != i:
                vid._add("laptop", "anchors", mine[j][0], 99)
        s, d = verdict(*mine[i])
        gen.append((s, d, mine[i][1], os.path.basename(MINE[i])))
    vid.prints = {}
    for j in keep:
        vid._add("laptop", "anchors", mine[j][0], 99)
    imp = [verdict(e, s) + (s,) for e, s in imps]
    # Fresh-enrollment realism: a print from 6 of his clips only.
    rng = np.random.default_rng(1)
    g6 = []
    for _ in range(300):
        idx = rng.choice(keep, 7, replace=False)
        vid.prints = {}
        for j in idx[:6]:
            vid._add("laptop", "anchors", mine[j][0], 99)
        g6.append(verdict(*mine[idx[6]]))
    count = lambda rows, d: sum(1 for r in rows if r[1] == d)  # noqa: E731
    gs = np.array([g[0] for g in gen]); is_ = np.array([r[0] for r in imp])
    print(f"his (LOO, 19-clip print): min {gs.min():.2f}  p10 {np.percentile(gs, 10):.2f}  "
          f"me {count(gen, 'me')}  unsure {count(gen, 'unsure')}  not_me {count(gen, 'not_me')}")
    print(f"his (6-clip print, 300 draws): me {count(g6, 'me')}  unsure "
          f"{count(g6, 'unsure')}  not_me {count(g6, 'not_me')}")
    print(f"others: max {is_.max():.2f}  p99 {np.percentile(is_, 99):.2f}  "
          f"me {count(imp, 'me')}  unsure {count(imp, 'unsure')}  not_me {count(imp, 'not_me')}")
    for s, d, secs, f in sorted(gen)[:5]:
        print(f"  his low: {s:.2f} {d} {secs:.1f}s {f}")
    for s, d, secs in sorted(imp, key=lambda r: r[0])[-5:]:
        print(f"  others high: {s:.2f} {d} {secs:.1f}s")


if __name__ == "__main__":
    {"cohort": cohort, "seed": seed, "eval": evaluate}[sys.argv[1]]()
