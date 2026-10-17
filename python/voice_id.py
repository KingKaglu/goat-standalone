"""Voice ID: does this utterance sound like Giorgi? (2026-10-18, his order:
"recognize only my voice ... as perfect as it gets").

Two speaker-embedding models (WeSpeaker ResNet34-LM and 3D-Speaker CAM++,
ONNX) each turn the speech in an utterance into a voiceprint vector. Each
score is the cosine against his print, AS-normalised against a cohort of
other people's voices, and the two are averaged. On his own 20 saved clips
against 70 pieces of 23 unseen voices: either model alone overlapped
(raw cosine, his worst 0.12-0.19 vs others' best 0.21-0.24); the normalised
pair left a clean gap (his worst 3.03, others' best 2.25).

Runs on the onnxruntime the VAD already uses: no torch (300MB), no
sherpa-onnx (a second onnxruntime.dll in one process is DLL roulette on
Windows). The Kaldi fbank front end is numpy, checked against torchaudio's
to 2e-4 (test_voice_id.py).

Prints are per mic ("laptop", "phone"): the phone browser's noise
suppression colours a voice differently from the laptop's AEC'd mic. Each
mic has ANCHORS (enrolled, never drift) and RECENT (clearly-him turns, so a
cold or a new room can't lock him out). A mic with no print borrows the
laptop's with wider bands and quietly grows its own from turns that clear
the full bar. All of it lives in goat-standalone/voiceid/: biometric data,
gitignored, never public.

CLI (for the brain): python voice_id.py status | lock on|off | enroll [mic]
| forget [mic]
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import onnxruntime as ort

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "voiceid"))
MODEL_DIR = os.path.join(ROOT, "models")
CONFIG = os.path.join(ROOT, "config.json")
PRINTS = os.path.join(ROOT, "prints.npz")
COHORT = os.path.join(ROOT, "cohort.npz")
LOG = os.path.join(ROOT, "voice-id.log")
MODELS = {"resnet": "wespeaker_en_voxceleb_resnet34_LM.onnx",
          "campplus": "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"}
SR = 16000

# Decision bands on the normalised score (roughly "standard deviations above
# a random other voice"). Set on the measurements in the module docstring.
ACCEPT = 3.0         # him
REJECT = 2.0         # clearly somebody else; between the two is "unsure"
SHORT_S = 0.4        # less speech than this is too little to judge
SHORT_DROP = 0.5     # both bands sink this much at SHORT_S (linear to 1.5s)
BORROW_DROP = 0.5    # and this much more on a mic scored with the laptop print
ADAPT_OVER = 1.0     # a turn this far above ACCEPT teaches RECENT
TOP_K = 50           # cohort voices nearest each side, for AS-norm
MAX_RECENT = 40
MAX_ANCHORS = 60
GROW_ANCHORS = 5     # clearly-him turns that give a borrowing mic its own print
SPEECH_CAP_S = 8.0   # embed at most this much speech: cost grows, accuracy doesn't
ENROLL_SPEECH_S = 20.0
ENROLL_WINDOW_S = 2.5
ENROLL_TIMEOUT_S = 180.0
LOG_MAX = 1_000_000


# ---------------------------------------------------------------- front end
def _mel_banks(num_bins=80, n_fft=512, sr=SR, low=20.0, high=0.0):
    """Kaldi's triangular mel filters, built the way torchaudio's
    compliance.kaldi.get_mel_banks does (plus the zero Nyquist column fbank
    pads on)."""
    nyq = 0.5 * sr
    if high <= 0.0:
        high += nyq
    mel = lambda f: 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)  # noqa: E731
    lo, hi = mel(low), mel(high)
    delta = (hi - lo) / (num_bins + 1)
    b = np.arange(num_bins, dtype=np.float64)[:, None]
    left, center, right = lo + b * delta, lo + (b + 1) * delta, lo + (b + 2) * delta
    m = mel((sr / n_fft) * np.arange(n_fft // 2))[None, :]
    up = (m - left) / (center - left)
    down = (right - m) / (right - center)
    banks = np.maximum(0.0, np.minimum(up, down))
    return np.pad(banks, ((0, 0), (0, 1))).astype(np.float32)


_BANKS = _mel_banks()
_N = np.arange(400, dtype=np.float64)
_WINDOWS = {
    "povey": ((0.5 - 0.5 * np.cos(2 * np.pi * _N / 399)) ** 0.85).astype(np.float32),
    "hamming": (0.54 - 0.46 * np.cos(2 * np.pi * _N / 399)).astype(np.float32),
}
_EPS = np.finfo(np.float32).eps


def fbank(x: np.ndarray, window: str = "hamming", scale: float = 32768.0) -> np.ndarray:
    """80-bin Kaldi log-mel fbank: 25ms frames, 10ms hop, snip_edges, no
    dither, DC removed, 0.97 pre-emphasis, 512-point power spectrum."""
    x = np.asarray(x, dtype=np.float32) * np.float32(scale)
    if len(x) < 400:
        return np.zeros((0, 80), dtype=np.float32)
    n = 1 + (len(x) - 400) // 160
    fr = x[np.arange(400)[None, :] + 160 * np.arange(n)[:, None]]
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = fr - 0.97 * np.concatenate([fr[:, :1], fr[:, :-1]], axis=1)
    fr = fr * _WINDOWS[window]
    spec = np.abs(np.fft.rfft(fr, n=512)).astype(np.float32) ** 2
    return np.log(np.maximum(spec @ _BANKS.T, _EPS)).astype(np.float32)


def _session(path: str, threads: int = 1) -> ort.InferenceSession:
    o = ort.SessionOptions()
    o.inter_op_num_threads = 1
    o.intra_op_num_threads = threads
    o.enable_cpu_mem_arena = False   # 8GB laptop: don't keep a peak-sized arena
    return ort.InferenceSession(path, sess_options=o, providers=["CPUExecutionProvider"])


class _Silero:
    """Own Silero VAD (the live one in audio_io carries streaming state;
    sharing it would scramble both)."""

    def __init__(self):
        spec = importlib.util.find_spec("silero_vad")
        self.s = _session(os.path.join(os.path.dirname(spec.origin), "data",
                                       "silero_vad.onnx"))
        self.sr = np.array(SR, dtype=np.int64)

    def probs(self, x: np.ndarray) -> np.ndarray:
        state = np.zeros((2, 1, 128), dtype=np.float32)
        ctx = np.zeros((1, 64), dtype=np.float32)
        out = []
        for i in range(0, len(x) - 511, 512):
            inp = np.concatenate([ctx, x[i:i + 512].reshape(1, -1)], axis=1)
            p, state = self.s.run(None, {"input": inp, "state": state, "sr": self.sr})
            ctx = inp[:, -64:]
            out.append(float(p[0, 0]))
        return np.array(out, dtype=np.float32)


def speech_only(x: np.ndarray, vad: _Silero, thr: float = 0.5) -> np.ndarray:
    """The voiced part of an utterance. The preroll and the hangover are room
    noise, and room noise averaged into a voiceprint drags every score toward
    everyone else's. One 32ms chunk of margin either side keeps word edges."""
    x = np.asarray(x, dtype=np.float32)
    p = vad.probs(x)
    if not len(p):
        return x[:0]
    keep = p >= thr
    keep = keep | np.r_[keep[1:], False] | np.r_[False, keep[:-1]]
    idx = np.flatnonzero(keep)
    return np.concatenate([x[i * 512:(i + 1) * 512] for i in idx]) if len(idx) else x[:0]


class Embedder:
    def __init__(self, filename: str):
        self.s = _session(os.path.join(MODEL_DIR, filename), threads=2)
        meta = self.s.get_modelmeta().custom_metadata_map
        self.input = self.s.get_inputs()[0].name
        self.scale = 1.0 if meta.get("normalize_samples") == "1" else 32768.0
        self.window = "povey" if meta.get("framework") == "3d-speaker" else "hamming"

    def __call__(self, speech: np.ndarray) -> np.ndarray | None:
        f = fbank(speech, self.window, self.scale)
        if len(f) < 20:
            return None
        f = f - f.mean(axis=0, keepdims=True)   # CMN (idempotent if the graph does it too)
        e = self.s.run(None, {self.input: f[None]})[0][0].astype(np.float32)
        n = float(np.linalg.norm(e))
        return e / n if n > 0 else None


# ---------------------------------------------------------------- verdicts
@dataclass
class Verdict:
    decision: str          # "me" | "not_me" | "unsure" | "unknown" (no print to compare)
    score: float | None
    speech_s: float
    source: str
    ms: float = 0.0
    embs: dict | None = field(default=None, repr=False)
    borrowed: bool = False

    def line(self) -> str:
        s = "-" if self.score is None else f"{self.score:.2f}"
        b = " (laptop print)" if self.borrowed else ""
        return (f"{self.decision} score={s} speech={self.speech_s:.1f}s "
                f"mic={self.source}{b} {self.ms:.0f}ms")


def bands(speech_s: float, borrowed: bool = False) -> tuple[float, float]:
    """(accept, reject) for this much speech: short speech scores lower for
    everyone, so both bars sink a little for it."""
    k = min(1.0, max(0.0, (1.5 - speech_s) / (1.5 - SHORT_S)))
    drop = SHORT_DROP * k + (BORROW_DROP if borrowed else 0.0)
    return ACCEPT - drop, REJECT - drop


def allow(v: Verdict, active: bool, addressed: bool) -> bool:
    """Does this turn get through the lock? `active` = he's mid-conversation
    (GOAT busy or speaking, or he was verified moments ago); `addressed` =
    the transcript says GOAT's name. A clear "not him" never passes; an
    unsure one passes only inside a live exchange or when called by name."""
    if v.decision in ("me", "unknown"):
        return True
    if v.decision == "not_me":
        return False
    return active or addressed


# ---------------------------------------------------------------- the store
class VoiceID:
    def __init__(self):
        self._lock = threading.Lock()
        self._emb: dict[str, Embedder] = {}
        self._vad: _Silero | None = None
        self._cfg_mtime = None
        self._cfg: dict = {}
        self.cohort: dict[str, np.ndarray] = {}
        self.prints: dict[str, dict[str, dict[str, np.ndarray]]] = {}
        self._enroll: dict | None = None
        self._enroll_seen = time.time()   # a CLI request older than this boot is stale
        try:
            z = np.load(COHORT)
            self.cohort = {k: z[k].astype(np.float32) for k in z.files}
        except (OSError, ValueError):
            pass
        self._load_prints()

    # ---- config (shared by the UI switch, the CLI and the app)
    def config(self) -> dict:
        try:
            m = os.path.getmtime(CONFIG)
        except OSError:
            return {}
        if m != self._cfg_mtime:
            try:
                with open(CONFIG, encoding="utf-8") as f:
                    self._cfg = json.load(f)
            except (OSError, ValueError):
                self._cfg = {}
            self._cfg_mtime = m
        return self._cfg

    def _write_cfg(self, **kw):
        cfg = dict(self.config())
        cfg.update(kw)
        os.makedirs(ROOT, exist_ok=True)
        tmp = CONFIG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
        os.replace(tmp, CONFIG)

    @property
    def locked(self) -> bool:
        return bool(self.config().get("lock", False))

    def set_lock(self, on: bool):
        self._write_cfg(lock=bool(on))

    # ---- prints: {source: {kind: {model_tag: (n, dim)}}}
    def _load_prints(self):
        self.prints = {}
        try:
            z = np.load(PRINTS)
        except (OSError, ValueError):
            return
        for k in z.files:
            src, kind, tag = k.split("__")
            self.prints.setdefault(src, {}).setdefault(kind, {})[tag] = \
                z[k].astype(np.float32)

    def _save_prints(self):
        os.makedirs(ROOT, exist_ok=True)
        flat = {f"{s}__{k}__{t}": a for s, kinds in self.prints.items()
                for k, tags in kinds.items() for t, a in tags.items() if len(a)}
        tmp = PRINTS + ".tmp.npz"
        np.savez(tmp, **flat)
        os.replace(tmp, PRINTS)

    def _count(self, source: str, kind: str) -> int:
        tags = self.prints.get(source, {}).get(kind, {})
        return min((len(a) for a in tags.values()), default=0) if tags else 0

    def enrolled(self, source: str) -> bool:
        return self._count(source, "anchors") >= 3

    def _add(self, source: str, kind: str, embs: dict, cap: int):
        tags = self.prints.setdefault(source, {}).setdefault(kind, {})
        for t, e in embs.items():
            e = np.atleast_2d(e).astype(np.float32)
            old = tags.get(t, np.zeros((0, e.shape[1]), np.float32))
            tags[t] = np.concatenate([old, e])[-cap:]

    def _centroids(self, source: str) -> tuple[dict | None, bool]:
        """Per-model unit centroid of his print for this mic, and whether it
        had to borrow the laptop's."""
        if not self.enrolled(source):
            if source != "laptop" and self.enrolled("laptop"):
                return self._centroids("laptop")[0], True
            return None, False
        kinds = self.prints[source]
        out = {}
        for t in MODELS:
            parts = [kinds[k][t] for k in ("anchors", "recent")
                     if t in kinds.get(k, {}) and len(kinds[k][t])]
            c = np.concatenate(parts).mean(axis=0)
            out[t] = c / max(float(np.linalg.norm(c)), 1e-9)
        return out, False

    # ---- inference
    def available(self) -> bool:
        return all(t in self.cohort for t in MODELS) and all(
            os.path.exists(os.path.join(MODEL_DIR, f)) for f in MODELS.values())

    def _ready(self):
        if not self._emb:
            self._vad = _Silero()
            self._emb = {t: Embedder(f) for t, f in MODELS.items()}

    def warm(self):
        with self._lock:
            self._ready()

    def _embed_speech(self, sp: np.ndarray) -> dict | None:
        out = {}
        for t, m in self._emb.items():
            e = m(sp)
            if e is None:
                return None
            out[t] = e
        return out

    def embed(self, audio: np.ndarray) -> tuple[dict | None, float]:
        self._ready()
        sp = speech_only(audio, self._vad)[:int(SPEECH_CAP_S * SR)]
        secs = len(sp) / SR
        if secs < 0.25:
            return None, secs
        return self._embed_speech(sp), secs

    def _asnorm(self, tag: str, e: np.ndarray, c: np.ndarray) -> float:
        coh = self.cohort[tag]
        k = min(TOP_K, len(coh))
        se = np.sort(coh @ c)[-k:]
        st = np.sort(coh @ e)[-k:]
        s = float(e @ c)
        return 0.5 * ((s - se.mean()) / max(se.std(), 1e-6)
                      + (s - st.mean()) / max(st.std(), 1e-6))

    def score(self, embs: dict, source: str) -> tuple[float | None, bool]:
        cents, borrowed = self._centroids(source)
        if cents is None:
            return None, False
        return float(np.mean([self._asnorm(t, embs[t], cents[t]) for t in MODELS])), borrowed

    def check(self, audio: np.ndarray, source: str = "laptop",
              wait: float = -1) -> Verdict | None:
        """Score one utterance. None only when `wait` ran out on the lock
        (the barge-in check never queues behind a long turn's check)."""
        if not self._lock.acquire(timeout=wait):
            return None
        try:
            t0 = time.perf_counter()
            embs, secs = self.embed(audio)
            ms = (time.perf_counter() - t0) * 1000
            if embs is None:
                v = Verdict("unsure", None, secs, source, ms)
            else:
                s, borrowed = self.score(embs, source)
                if s is None:
                    v = Verdict("unknown", None, secs, source, ms, embs)
                else:
                    acc, rej = bands(secs, borrowed)
                    d = ("unsure" if secs < SHORT_S else
                         "me" if s >= acc else "not_me" if s < rej else "unsure")
                    v = Verdict(d, s, secs, source, ms, embs, borrowed)
        finally:
            self._lock.release()
        self._log(v.line())
        return v

    def adapt(self, v: Verdict):
        """A turn that was clearly him keeps his print current; on a mic still
        borrowing the laptop print, such turns become that mic's own print."""
        if v.embs is None or v.score is None or v.decision != "me":
            return
        with self._lock:
            if v.borrowed:
                if v.speech_s >= 1.0 and v.score >= ACCEPT:
                    self._add(v.source, "pending", v.embs, GROW_ANCHORS)
                    if self._count(v.source, "pending") >= GROW_ANCHORS:
                        self.prints[v.source]["anchors"] = self.prints[v.source].pop("pending")
                        self._log(f"{v.source} mic has its own print now")
                    self._save_prints()
            elif v.speech_s >= 1.5 and v.score >= bands(v.speech_s)[0] + ADAPT_OVER:
                self._add(v.source, "recent", v.embs, MAX_RECENT)
                self._save_prints()

    def clearly_not_him(self, audio: np.ndarray, source: str = "laptop") -> bool:
        """For barge-in while GOAT is talking: only a confident "someone
        else" may keep the voice going. GOAT's own residual echo is mixed in
        there, so unsure means let him interrupt."""
        if not (self.locked and self.available() and self._centroids(source)[0]):
            return False
        v = self.check(audio, source, wait=0.15)
        return v is not None and v.decision == "not_me"

    # ---- enrollment
    def start_enroll(self, source: str | None = None):
        """Learn his voice from the next ~20s he speaks into `source` (None =
        whichever mic he uses next)."""
        self._enroll = {"source": source, "t": time.monotonic(), "speech": []}
        self._log(f"enroll started ({source or 'next mic'})")

    def cancel_enroll(self):
        self._enroll = None

    def enrolling(self, source: str) -> bool:
        req = self.config().get("enroll_at", 0.0)
        if req > self._enroll_seen:   # asked for from the CLI or the UI
            self._enroll_seen = req
            want = self.config().get("enroll")
            self.start_enroll(None if want in (None, "any") else want)
        e = self._enroll
        if e is None:
            return False
        if time.monotonic() - e["t"] > ENROLL_TIMEOUT_S:
            self._enroll = None
            self._log("enroll timed out")
            return False
        return e["source"] in (None, source)

    def enroll_feed(self, audio: np.ndarray, source: str) -> tuple[float, bool]:
        """One utterance of enrollment speech. Returns (seconds collected,
        finished). Finishing turns the speech into 2.5s anchors for that mic
        (added to what it had) and switches the lock on."""
        e = self._enroll
        with self._lock:
            self._ready()
            e["source"] = source
            sp = speech_only(audio, self._vad)
            if len(sp) >= 0.5 * SR:
                e["speech"].append(sp)
            got = sum(len(s) for s in e["speech"]) / SR
            if got < ENROLL_SPEECH_S:
                return got, False
            allsp = np.concatenate(e["speech"])
            w = int(ENROLL_WINDOW_S * SR)
            embs = [self._embed_speech(allsp[i:i + w])
                    for i in range(0, len(allsp) - w + 1, w)]
            embs = [x for x in embs if x is not None]
            for x in embs:
                self._add(source, "anchors", x, MAX_ANCHORS)
            self.prints.get(source, {}).pop("pending", None)
            self._save_prints()
            self._enroll = None
        self.set_lock(True)
        self._log(f"enrolled {source}: {len(embs)} anchors from {got:.0f}s")
        return got, True

    def forget(self, source: str | None = None):
        with self._lock:
            if source:
                self.prints.pop(source, None)
            else:
                self.prints = {}
            self._save_prints()
        if not self.prints:
            self.set_lock(False)

    def status(self) -> str:
        mics = "; ".join(f"{s}: {self._count(s, 'anchors')} enrolled + "
                         f"{self._count(s, 'recent')} learned"
                         + (f" + {self._count(s, 'pending')} growing" if
                            self._count(s, 'pending') else "")
                         for s in self.prints) or "no voiceprint yet"
        ready = "" if self.available() else " (models or cohort missing)"
        return f"voice lock {'ON' if self.locked else 'off'}{ready}; {mics}"

    def _log(self, line: str):
        try:
            if os.path.getsize(LOG) > LOG_MAX:
                os.replace(LOG, LOG + ".old")
        except OSError:
            pass
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass


_inst: VoiceID | None = None


def get() -> VoiceID | None:
    """The shared instance, or None when the models or cohort are missing
    (GOAT then hears everyone, exactly as before)."""
    global _inst
    if _inst is None:
        v = VoiceID()
        if not v.available():
            return None
        _inst = v
    return _inst


if __name__ == "__main__":
    vid = VoiceID()
    a = sys.argv[1:]
    if a[:1] == ["lock"] and a[1:2] in (["on"], ["off"]):
        if a[1] == "on" and not vid.enrolled("laptop") and not vid.prints:
            sys.exit("no voiceprint yet: say \"learn my voice\" first")
        vid.set_lock(a[1] == "on")
    elif a[:1] == ["enroll"]:
        vid._write_cfg(enroll=a[1] if len(a) > 1 else "any", enroll_at=time.time())
        print("listening for his voice: the next ~20s he speaks are enrollment")
    elif a[:1] == ["forget"]:
        vid.forget(a[1] if len(a) > 1 else None)
    print(vid.status())
