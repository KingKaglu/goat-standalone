"""A local Georgian ear: NVIDIA's FastConformer for Georgian, on onnxruntime.

Model: nvidia/stt_ka_fastconformer_hybrid_large_pc (CC-BY-4.0, 115 M
parameters, Georgian only, writes punctuation), its CTC head exported to ONNX
(OpenVoiceOS/stt_ka_fastconformer_hybrid_large_pc_onnx). Runs on the CPU with
the onnxruntime GOAT already ships for Silero VAD: no new package, no network,
no key. The audio front end below is NeMo's mel spectrogram written in numpy.

Files (not in git; see GOAT_STT_KA_DIR):
    <GOAT>/stt/fastconformer-ka/model.onnx   (or model.int8.onnx)
    <GOAT>/stt/fastconformer-ka/vocab.txt

    transcribe(audio, 16000) -> text, "" for silence, None when it can't run
                                or is not sure it heard speech (see MIN_CONF)

It knows no English: English words come out as Georgian letters. The caller
decides when a clip is Georgian (stt.py).
"""
from __future__ import annotations

import os
import threading

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = (os.environ.get("GOAT_STT_KA_DIR", "").strip()
       or os.path.join(os.path.dirname(HERE), "stt", "fastconformer-ka"))
# Measured 2026-11-10 on his laptop (30 FLEURS clips, 340 s of Georgian):
#   model.int8.onnx  131 MB file, +266 MB RAM, WER 17.6 %, 112 ms for a 4 s clip
#   model.onnx       458 MB file, +583 MB RAM, WER 18.0 %,  58 ms for a 4 s clip
# Same words, so the small one is the default on an 8 GB machine.
MODEL = os.environ.get("GOAT_STT_KA_MODEL", "").strip() or next(
    (m for m in ("model.int8.onnx", "model.onnx") if os.path.isfile(os.path.join(DIR, m))),
    "model.int8.onnx")
THREADS = int(os.environ.get("GOAT_STT_KA_THREADS", "4") or 4)

# On sound that is not speech the model still writes a word or two ("საშუალო
# დაამთავრა." for a hum, 2026-11-10). Its own certainty tells them apart:
# the mean probability of the tokens it wrote was 0.39-0.55 on noise, hum,
# tones and clicks, 0.78 and up on clean speech of 2 s or more, 0.64 and up
# on whole sentences under 5 dB of noise. Below MIN_CONF it answers None and
# the caller's next ear decides.
MIN_CONF = float(os.environ.get("GOAT_STT_KA_MIN_CONF", "0.6") or 0.6)

SR = 16000
N_FFT, WIN, HOP, N_MELS = 512, 400, 160, 80
PREEMPH = 0.97
LOG_GUARD = 2.0 ** -24

_lock = threading.Lock()
_sess = None
_vocab: list = []
_blank = 0
_broken = ""


def _model_path() -> str:
    return os.path.join(DIR, MODEL)


def available() -> bool:
    return (not _broken and os.path.isfile(_model_path())
            and os.path.isfile(os.path.join(DIR, "vocab.txt")))


def why() -> str:
    if _broken:
        return _broken
    if not os.path.isfile(_model_path()):
        return f"model file missing: {_model_path()}"
    if not os.path.isfile(os.path.join(DIR, "vocab.txt")):
        return "vocab.txt missing"
    return ""


def _hz_to_mel(f):
    f = np.asarray(f, dtype=np.float64)
    mel = f / (200.0 / 3)
    log_t = f >= 1000.0
    mel[log_t] = 15.0 + np.log(f[log_t] / 1000.0) / (np.log(6.4) / 27.0)
    return mel


def _mel_to_hz(m):
    m = np.asarray(m, dtype=np.float64)
    f = m * (200.0 / 3)
    log_t = m >= 15.0
    f[log_t] = 1000.0 * np.exp((np.log(6.4) / 27.0) * (m[log_t] - 15.0))
    return f


def _mel_filters() -> np.ndarray:
    """Slaney-scale, area-normalised triangles (what librosa.filters.mel gives
    NeMo): [N_MELS, N_FFT // 2 + 1]."""
    freqs = np.linspace(0.0, SR / 2, N_FFT // 2 + 1)
    edges = _mel_to_hz(np.linspace(_hz_to_mel([0.0])[0], _hz_to_mel([SR / 2])[0], N_MELS + 2))
    fb = np.zeros((N_MELS, len(freqs)))
    for i in range(N_MELS):
        lo, mid, hi = edges[i], edges[i + 1], edges[i + 2]
        up = (freqs - lo) / (mid - lo)
        down = (hi - freqs) / (hi - mid)
        fb[i] = np.maximum(0.0, np.minimum(up, down)) * (2.0 / (hi - lo))
    return fb.astype(np.float32)


_FB = None
_WINDOW = None


def features(audio: np.ndarray) -> np.ndarray:
    """float32 mono 16 kHz -> normalised log-mel [N_MELS, frames]."""
    global _FB, _WINDOW
    if _FB is None:
        _FB = _mel_filters()
        w = np.hanning(WIN).astype(np.float32)          # symmetric, as NeMo
        left = (N_FFT - WIN) // 2
        _WINDOW = np.pad(w, (left, N_FFT - WIN - left))
    x = np.asarray(audio, dtype=np.float32)
    x = np.concatenate([x[:1], x[1:] - PREEMPH * x[:-1]])
    x = np.pad(x, N_FFT // 2)
    n = 1 + (len(x) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    spec = np.fft.rfft(x[idx] * _WINDOW, axis=1)
    power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
    mel = np.log(power @ _FB.T + LOG_GUARD).T            # [mels, frames]
    mean = mel.mean(axis=1, keepdims=True)
    std = mel.std(axis=1, ddof=1, keepdims=True) if mel.shape[1] > 1 else 1.0
    return ((mel - mean) / (std + 1e-5)).astype(np.float32)


def _load():
    global _sess, _vocab, _blank, _broken
    if _sess is not None or _broken:
        return _sess
    with _lock:
        if _sess is not None or _broken:
            return _sess
        try:
            import onnxruntime as ort
            toks = {}
            with open(os.path.join(DIR, "vocab.txt"), encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    tok, _, num = line.rpartition(" ")
                    toks[int(num)] = tok
            _vocab = [toks.get(i, "") for i in range(max(toks) + 1)]
            _blank = _vocab.index("<blk>") if "<blk>" in _vocab else len(_vocab) - 1
            opt = ort.SessionOptions()
            opt.intra_op_num_threads = THREADS
            opt.log_severity_level = 3
            _sess = ort.InferenceSession(_model_path(), opt,
                                         providers=["CPUExecutionProvider"])
        except Exception as e:  # noqa: BLE001 — the caller falls back to the cloud ear
            _broken = f"{type(e).__name__}: {e}"
            print(f"[stt-ka] local Georgian ear not loaded: {_broken}", flush=True)
            _sess = None
    return _sess


def warm() -> bool:
    """Load the model now (about two seconds) instead of on his first sentence."""
    return available() and _load() is not None


def warm_async() -> None:
    """The same, off the caller's thread. Cheap to call again."""
    if _sess is None and available():
        threading.Thread(target=warm, name="stt-ka-warm", daemon=True).start()


def transcribe(audio: np.ndarray, sample_rate: int = SR):
    if sample_rate != SR or not available():
        return None
    if len(audio) < SR * 0.3 or float(np.max(np.abs(audio))) < 0.002:
        return ""
    sess = _load()
    if sess is None:
        return None
    try:
        feats = features(audio)[None]
        ins = sess.get_inputs()
        feed = {ins[0].name: feats,
                ins[1].name: np.array([feats.shape[2]], dtype=np.int64)}
        with _lock:                       # one clip at a time: it uses every core it is given
            logp = sess.run(None, feed)[0][0]
    except Exception as e:  # noqa: BLE001
        print(f"[stt-ka] local Georgian ear failed: {e!r}", flush=True)
        return None
    ids = logp.argmax(axis=-1)
    spoke = ids != _blank
    if not spoke.any():
        return ""
    conf = float(np.exp(logp.max(axis=-1)[spoke]).mean())
    if conf < MIN_CONF:
        print(f"[stt-ka] unsure ({conf:.2f} < {MIN_CONF}): leaving this clip to the next ear",
              flush=True)
        return None
    out, prev = [], -1
    for i in ids.tolist():
        if i != prev and i != _blank:
            out.append(_vocab[i])
        prev = i
    return "".join(out).replace("▁", " ").strip()
