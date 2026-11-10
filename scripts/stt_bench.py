"""Georgian STT benchmark for GOAT: WER, CER and latency per engine.

    py -3.13 scripts/stt_bench.py <folder>                    # the current engine
    py -3.13 scripts/stt_bench.py <folder> --engines current,gemini-rest,onnx-ka
    py -3.13 scripts/stt_bench.py <folder> --engines all --prep raw,norm,denoise
    py -3.13 scripts/stt_bench.py <folder> --clip-ms 0,100,200   # VAD cutting word edges
    py -3.13 scripts/stt_bench.py <folder> --llm-fix             # + LLM correction pass
    py -3.13 scripts/stt_bench.py --list                         # engines and why any is unavailable
    py -3.13 scripts/stt_bench.py --selftest                     # the WER/CER maths, no audio

<folder> holds WAV files and their reference text, either
  - one `<name>.txt` beside each `<name>.wav` (UTF-8, what was really said), or
  - a `refs.tsv`: `file<TAB>reference text<TAB>tags` (tags optional, comma
    separated, e.g. `phone,mixed` or `laptop,numbers`; results are also broken
    down per tag).
Keep the folder out of git (e.g. workspace/stt-bench/, which is gitignored):
the recordings are his voice.

Nothing here changes the app. Engines reuse GOAT's own modules read-only
(python/stt_whisper.py, stt_live.py, stt_gladia.py), so "current" is the exact
path a laptop turn takes in English mode. Local candidates need packages GOAT
does not ship (see ENGINES); an engine whose package or key is missing is
skipped with the reason, never faked. Cloud engines send the audio to that
provider and may cost money: they only run when named in --engines.

Output: a Markdown table on stdout and, in <folder>/_bench/, results.json
(every hypothesis, per-utterance errors and timings) and results.md.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import statistics
import sys
import time
import unicodedata
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(os.path.dirname(HERE), "python")
SR = 16000


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

_PUNCT = re.compile(r"[^\w\s']", re.UNICODE)


def normalize(text: str) -> str:
    """What counts as 'the same words': NFC, lowercase, no punctuation, single
    spaces. Georgian has no case; English does. Digits are left as they are,
    so "5" vs "ხუთი" counts as an error on purpose (it shows in the table as
    a numbers problem, not hidden by a guess at how he would write it)."""
    t = unicodedata.normalize("NFC", text or "").lower()
    t = t.replace("_", " ")
    t = _PUNCT.sub(" ", t)
    return " ".join(t.split())


def edit_distance(ref: list, hyp: list) -> int:
    """Levenshtein distance between two sequences (substitution, insertion and
    deletion all cost 1)."""
    if not ref:
        return len(hyp)
    if not hyp:
        return len(ref)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1]


def errors(ref: str, hyp: str) -> dict:
    """Word and character edit counts for one utterance (normalized)."""
    r, h = normalize(ref), normalize(hyp)
    rw, hw = r.split(), h.split()
    rc, hc = list(r.replace(" ", "")), list(h.replace(" ", ""))
    return {"w_err": edit_distance(rw, hw), "w_ref": len(rw),
            "c_err": edit_distance(rc, hc), "c_ref": len(rc)}


def rate(err: int, ref: int) -> float | None:
    return None if ref == 0 else 100.0 * err / ref


def pct(xs: list, p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------

def load_wav(path: str):
    """Any WAV -> float32 mono 16 kHz in [-1, 1], plus the file's own rate and
    channel count (reported, because a phone clip that arrives at 48 kHz stereo
    is itself a finding)."""
    import numpy as np
    try:
        import soundfile as sf
        data, rate_in = sf.read(path, dtype="float32", always_2d=True)
        ch = data.shape[1]
        mono = data.mean(axis=1)
    except ImportError:
        with wave.open(path, "rb") as w:
            rate_in, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if width != 2:
            raise ValueError(f"{path}: only 16-bit PCM without the soundfile package")
        mono = (np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                ).reshape(-1, ch).mean(axis=1)
    if rate_in != SR:
        import math
        sys.path.insert(0, PY)
        import dsp
        g = math.gcd(SR, rate_in)
        mono = dsp.resample_poly(mono, SR // g, rate_in // g).astype(np.float32)
    return mono.astype(np.float32), rate_in, ch


def wav_bytes(audio) -> bytes:
    import numpy as np
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def prep_raw(a):
    return a


def prep_norm(a):
    """Peak to -6 dBFS, gain capped at 12x: what phone_server does to a phone
    clip and close to what audio_io does to the laptop mic."""
    import numpy as np
    pk = float(np.max(np.abs(a))) if len(a) else 0.0
    if pk < 0.002:
        return a
    return np.clip(a * min(12.0, 0.5 / pk), -1.0, 1.0).astype(np.float32)


def prep_highpass(a):
    """80 Hz one-pole high-pass: desk rumble and handling noise out."""
    import numpy as np
    rc = 1.0 / (2 * np.pi * 80.0)
    alpha = rc / (rc + 1.0 / SR)
    out = np.empty_like(a)
    prev_x = prev_y = 0.0
    for i, x in enumerate(a):
        prev_y = alpha * (prev_y + x - prev_x)
        prev_x = x
        out[i] = prev_y
    return out


def prep_denoise(a):
    """Noise suppression. Uses the `noisereduce` package when it is installed;
    otherwise a plain spectral gate (noise floor = quietest 10 % of frames,
    12 dB of reduction), so the row is honest about which one ran."""
    import numpy as np
    try:
        import noisereduce
        return noisereduce.reduce_noise(y=a, sr=SR).astype(np.float32)
    except ImportError:
        pass
    n, hop = 512, 128
    if len(a) < n * 2:
        return a
    win = np.hanning(n).astype(np.float32)
    frames = 1 + (len(a) - n) // hop
    spec = np.stack([np.fft.rfft(a[i * hop:i * hop + n] * win) for i in range(frames)])
    mag = np.abs(spec)
    floor = np.percentile(mag, 10, axis=0)
    gain = np.clip((mag - 2.0 * floor) / np.maximum(mag, 1e-9), 0.25, 1.0)
    out = np.zeros(len(a), dtype=np.float32)
    norm = np.zeros(len(a), dtype=np.float32)
    for i in range(frames):
        out[i * hop:i * hop + n] += np.fft.irfft(spec[i] * gain[i], n).astype(np.float32) * win
        norm[i * hop:i * hop + n] += win * win
    return (out / np.maximum(norm, 1e-6)).astype(np.float32)


def prep_pad(a):
    """300 ms of silence on both sides: does the engine need room around words?"""
    import numpy as np
    z = np.zeros(int(0.3 * SR), dtype=np.float32)
    return np.concatenate([z, a, z])


PREPS = {"raw": prep_raw, "norm": prep_norm, "highpass": prep_highpass,
         "denoise": prep_denoise, "pad": prep_pad}
PREP_NAME = {"denoise": lambda: "denoise(noisereduce)" if _has("noisereduce")
             else "denoise(spectral-gate)"}


def clip_edges(a, ms: int):
    """Cut `ms` off the start and the end: what a VAD does when it opens late
    or closes early. Compare WER at 0 / 100 / 200 ms to see how much a clipped
    word edge costs each engine."""
    n = int(ms / 1000 * SR)
    return a if n <= 0 or len(a) <= 2 * n + SR // 4 else a[n:-n]


# --------------------------------------------------------------------------
# Engines: name -> (why it can't run | None, factory -> transcribe(audio) -> str)
# --------------------------------------------------------------------------

def _has(mod: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(mod) is not None


def _goat():
    if PY not in sys.path:
        sys.path.insert(0, PY)


def _whisper():
    """GOAT's whisper-server module. If GOAT isn't running, ensure_server()
    starts a server of our own: stop that one again when the benchmark ends
    (shutdown() leaves a server GOAT itself started alone)."""
    _goat()
    import atexit

    import stt_whisper
    if not getattr(_whisper, "hooked", False):
        _whisper.hooked = True
        atexit.register(stt_whisper.shutdown)
    if not stt_whisper.ensure_server():
        raise RuntimeError("whisper-server did not start")
    return stt_whisper


def _gemini_key():
    _goat()
    import talk_brain
    return talk_brain.api_key()


def _scribe_key():
    _goat()
    import stt_gladia
    return stt_gladia._scribe_key()


VOCAB = None   # --vocab file: one name or term per line


def _vocab() -> list:
    if not VOCAB:
        return []
    with open(VOCAB, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def eng_current():
    """GOAT today, English mode, laptop batch path: Whisper large-v3-turbo
    (GPU, auto-detect) -> Gemini Live transcript -> REST Gemini / Gladia."""
    stt_whisper = _whisper()
    return lambda a: (stt_whisper.transcribe_lang(a, SR, "en")[0] or "")


def eng_whisper_ka():
    """The same local Whisper, language pinned to Georgian (forced language
    instead of auto-detect)."""
    stt_whisper = _whisper()
    return lambda a: (stt_whisper._pinned(a, SR, "ka")[0] or "")


def eng_whisper_auto():
    """The same local Whisper, language auto-detected, no cloud ear."""
    stt_whisper = _whisper()

    def run(a):
        got = stt_whisper._post(a, SR, {"response_format": "json", "language": "auto"})
        return (got.get("text") or "").strip() if isinstance(got, dict) else ""
    return run


def eng_gemini_live():
    """Gemini Live inputAudioTranscription (GOAT's Georgian ear today)."""
    _goat()
    import stt_live
    return lambda a: (stt_live.transcribe(a, SR) or "")


def _gemini_rest(model: str, vocab: bool):
    import base64

    import httpx
    key = _gemini_key()
    client = httpx.Client(timeout=30.0)
    prompt = ("Transcribe this speech exactly, word for word, in the language spoken. "
              "Georgian must be written in Georgian script (Mkhedruli); English words "
              "stay in English. Output only the transcript. If there is no speech, "
              "output nothing.")
    if vocab and _vocab():
        prompt += " Names and terms that may occur: " + ", ".join(_vocab()) + "."

    def run(a):
        body = {"contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": "audio/wav",
                            "data": base64.b64encode(wav_bytes(a)).decode()}},
            {"text": prompt}]}],
            "generationConfig": {"temperature": 0}}
        r = client.post("https://generativelanguage.googleapis.com/v1beta/models/"
                        f"{model}:generateContent", params={"key": key}, json=body)
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"].get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    return run


def eng_gemini_rest():
    """Gemini generateContent with the audio inline (model: --gemini-model)."""
    return _gemini_rest(ARGS.gemini_model, vocab=False)


def eng_gemini_rest_vocab():
    """The same, with the --vocab list in the prompt (vocabulary bias)."""
    return _gemini_rest(ARGS.gemini_model, vocab=True)


def _scribe(language: str | None, keyterms: bool):
    import httpx
    key = _scribe_key()
    client = httpx.Client(timeout=60.0)

    def run(a):
        data = {"model_id": "scribe_v2", "tag_audio_events": "false", "temperature": "0"}
        if language:
            data["language_code"] = language
        if keyterms and _vocab():
            data["keyterms"] = _vocab()[:90]
        r = client.post("https://api.elevenlabs.io/v1/speech-to-text",
                        headers={"xi-api-key": key}, data=data,
                        files={"file": ("u.wav", wav_bytes(a), "audio/wav")})
        r.raise_for_status()
        return (r.json().get("text") or "").strip()
    return run


def eng_scribe():
    """ElevenLabs Scribe v2 (batch), language auto-detected. PAID."""
    return _scribe(None, False)


def eng_scribe_ka():
    """ElevenLabs Scribe v2 (batch), language forced to Georgian. PAID."""
    return _scribe("ka", False)


def eng_scribe_vocab():
    """ElevenLabs Scribe v2, auto language, --vocab as keyterms. PAID (+keyterm fee)."""
    return _scribe(None, True)


def eng_gladia():
    """Gladia Solaria-1 pinned to Georgian (GOAT's last-resort ear). PAID."""
    _goat()
    import stt_gladia
    key = stt_gladia._gladia_key()
    return lambda a: (stt_gladia._gladia(key, wav_bytes(a), "ka") or "")


def eng_onnx_ka():
    """NVIDIA Georgian FastConformer, ONNX export, through onnx-asr on
    onnxruntime (no PyTorch): `pip install onnx-asr[hub]`."""
    import onnx_asr
    model = onnx_asr.load_model(ARGS.onnx_model)

    def run(a):
        out = model.recognize(a, sample_rate=SR)
        return out if isinstance(out, str) else str(getattr(out, "text", out))
    return run


def eng_nemo_ka():
    """nvidia/stt_ka_fastconformer_hybrid_large_pc through NeMo
    (`pip install nemo_toolkit[asr]`, PyTorch): transducer decoder."""
    import tempfile

    import nemo.collections.asr as nemo_asr
    model = nemo_asr.models.ASRModel.from_pretrained(ARGS.nemo_model)
    model.eval()

    def run(a):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes(a))
            path = f.name
        try:
            out = model.transcribe([path], verbose=False)
        finally:
            os.remove(path)
        first = out[0][0] if isinstance(out, tuple) else out[0]
        return getattr(first, "text", first)
    return run


def eng_hf_whisper():
    """A fine-tuned Whisper from Hugging Face through transformers
    (--hf-model, default BuzzASR/georgian; ~2B parameters: needs the GPU's
    memory to itself, or a lot of patience on CPU)."""
    import torch
    from transformers import AutoProcessor, WhisperForConditionalGeneration
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if dev == "cuda" else torch.float32
    proc = AutoProcessor.from_pretrained(ARGS.hf_model)
    model = WhisperForConditionalGeneration.from_pretrained(
        ARGS.hf_model, torch_dtype=dtype).to(dev)
    model.eval()

    def run(a):
        feats = proc(a, sampling_rate=SR, return_tensors="pt").input_features.to(dev, dtype)
        with torch.no_grad():
            ids = model.generate(feats, num_beams=1, no_repeat_ngram_size=3,
                                 repetition_penalty=1.2, max_new_tokens=225)
        return proc.batch_decode(ids, skip_special_tokens=True)[0].strip()
    return run


def _why_goat_file(name: str):
    return None if os.path.exists(os.path.join(PY, name)) else f"python/{name} missing"


ENGINES = {
    # name: (availability check -> reason or None, factory, cloud?, paid?)
    "current": (lambda: _why_goat_file("stt_whisper.py"), eng_current, True, False),
    "whisper-auto": (lambda: _why_goat_file("stt_whisper.py"), eng_whisper_auto, False, False),
    "whisper-ka": (lambda: _why_goat_file("stt_whisper.py"), eng_whisper_ka, False, False),
    "gemini-live": (lambda: None if _gemini_key() else "no Gemini key", eng_gemini_live, True, False),
    "gemini-rest": (lambda: None if _gemini_key() else "no Gemini key", eng_gemini_rest, True, False),
    "gemini-rest-vocab": (lambda: (None if _gemini_key() else "no Gemini key") if VOCAB
                          else "needs --vocab", eng_gemini_rest_vocab, True, False),
    "onnx-ka": (lambda: None if _has("onnx_asr") else "package onnx-asr not installed",
                eng_onnx_ka, False, False),
    "nemo-ka": (lambda: None if _has("nemo") else "package nemo_toolkit not installed",
                eng_nemo_ka, False, False),
    "hf-whisper": (lambda: None if _has("transformers") and _has("torch")
                   else "packages transformers/torch not installed", eng_hf_whisper, False, False),
    "scribe": (lambda: None if _scribe_key() else "no ElevenLabs key", eng_scribe, True, True),
    "scribe-ka": (lambda: None if _scribe_key() else "no ElevenLabs key", eng_scribe_ka, True, True),
    "scribe-vocab": (lambda: (None if _scribe_key() else "no ElevenLabs key") if VOCAB
                     else "needs --vocab", eng_scribe_vocab, True, True),
    "gladia": (lambda: _gladia_why(), eng_gladia, True, True),
}


def _gladia_why():
    _goat()
    import stt_gladia
    return None if stt_gladia._gladia_key() else "no Gladia key"


# --------------------------------------------------------------------------
# LLM correction pass
# --------------------------------------------------------------------------

def llm_fixer():
    """A cheap LLM pass over the transcript (Gemini, --fix-model): fixes
    obvious recognition slips, changes nothing else. Returns fix(text) -> text."""
    import httpx
    key = _gemini_key()
    if not key:
        raise RuntimeError("no Gemini key")
    client = httpx.Client(timeout=20.0)
    sys_text = ("You correct speech-recognition output for a Georgian speaker who mixes "
                "in English words. Fix only clear recognition errors (wrong or split "
                "words, wrong script). Never translate, add, remove or rephrase. Keep "
                "Georgian in Georgian script and English terms in English. Output only "
                "the corrected transcript.")
    if _vocab():
        sys_text += " Names and terms he uses: " + ", ".join(_vocab()) + "."

    def fix(text: str) -> str:
        if not text.strip():
            return text
        r = client.post("https://generativelanguage.googleapis.com/v1beta/models/"
                        f"{ARGS.fix_model}:generateContent", params={"key": key},
                        json={"systemInstruction": {"parts": [{"text": sys_text}]},
                              "contents": [{"role": "user", "parts": [{"text": text}]}],
                              "generationConfig": {"temperature": 0}})
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"].get("parts") or []
        return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip() or text
    return fix


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

def load_set(folder: str) -> list:
    items = []
    tsv = os.path.join(folder, "refs.tsv")
    if os.path.exists(tsv):
        with open(tsv, encoding="utf-8-sig") as f:
            for ln in f:
                if not ln.strip() or ln.startswith("#"):
                    continue
                cols = ln.rstrip("\n").split("\t")
                if len(cols) < 2:
                    raise SystemExit(f"refs.tsv: need file<TAB>text, got {ln!r}")
                tags = [t.strip() for t in cols[2].split(",")] if len(cols) > 2 else []
                items.append({"file": cols[0].strip(), "ref": cols[1].strip(),
                              "tags": [t for t in tags if t]})
    else:
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith(".wav"):
                txt = os.path.join(folder, os.path.splitext(name)[0] + ".txt")
                if not os.path.exists(txt):
                    print(f"  (skipped {name}: no reference .txt)", file=sys.stderr)
                    continue
                with open(txt, encoding="utf-8-sig") as f:
                    items.append({"file": name, "ref": f.read().strip(), "tags": []})
    for it in items:
        path = os.path.join(folder, it["file"])
        if not os.path.exists(path):
            raise SystemExit(f"missing audio: {path}")
        it["audio"], it["rate_in"], it["channels"] = load_wav(path)
        it["seconds"] = len(it["audio"]) / SR
        if not it["tags"]:
            # tags from the file name: phone_mixed_01.wav -> phone, mixed
            it["tags"] = [t for t in re.split(r"[_\-. ]+", os.path.splitext(it["file"])[0].lower())
                          if t in ("phone", "laptop", "short", "long", "mixed", "names",
                                   "numbers", "noisy", "quiet")]
    if not items:
        raise SystemExit(f"no WAV + reference pairs in {folder}")
    return items


def run_config(label: str, fn, items: list, prep, clip_ms: int, fix, repeats: int) -> dict:
    rows = []
    for it in items:
        audio = prep(clip_edges(it["audio"], clip_ms))
        best, hyp, err = None, "", None
        for _ in range(max(1, repeats)):
            t0 = time.perf_counter()
            try:
                hyp = fn(audio) or ""
                err = None
            except Exception as e:  # noqa: BLE001 - one bad clip must not end the run
                hyp, err = "", f"{type(e).__name__}: {str(e)[:120]}"
            dt = (time.perf_counter() - t0) * 1000
            best = dt if best is None else min(best, dt)
        row = {"file": it["file"], "ref": it["ref"], "hyp": hyp, "ms": round(best),
               "seconds": round(it["seconds"], 2), "tags": it["tags"], "error": err}
        row.update(errors(it["ref"], hyp))
        if fix is not None and err is None:
            t0 = time.perf_counter()
            try:
                fixed = fix(hyp)
            except Exception as e:  # noqa: BLE001
                fixed, row["fix_error"] = hyp, f"{type(e).__name__}: {str(e)[:120]}"
            row["fix_ms"] = round((time.perf_counter() - t0) * 1000)
            row["hyp_fixed"] = fixed
            fe = errors(it["ref"], fixed)
            row["w_err_fixed"], row["c_err_fixed"] = fe["w_err"], fe["c_err"]
        rows.append(row)
        print(f"  {label:<34} {it['file']:<28} {row['ms']:>6} ms  "
              f"{'ERR ' + err if err else ''}", file=sys.stderr, flush=True)
    return {"label": label, "rows": rows}


def summarize(res: dict, tag: str | None = None) -> dict:
    rows = [r for r in res["rows"] if tag is None or tag in r["tags"]]
    ok = [r for r in rows if not r["error"]]
    s = {"label": res["label"], "n": len(rows), "failed": len(rows) - len(ok)}
    # a failed call scores as an empty transcript: every reference word is an error
    s["wer"] = rate(sum(r["w_err"] for r in rows), sum(r["w_ref"] for r in rows))
    s["cer"] = rate(sum(r["c_err"] for r in rows), sum(r["c_ref"] for r in rows))
    s["exact"] = sum(1 for r in rows if r["w_err"] == 0)
    ms = [r["ms"] for r in ok]
    s["p50"], s["p90"] = pct(ms, 0.5), pct(ms, 0.9)
    s["rtf"] = (statistics.fmean(r["ms"] / 1000 / r["seconds"] for r in ok if r["seconds"])
                if ok else None)
    fixed = [r for r in rows if "w_err_fixed" in r]
    if fixed:
        s["wer_fixed"] = rate(sum(r["w_err_fixed"] for r in fixed), sum(r["w_ref"] for r in fixed))
        s["cer_fixed"] = rate(sum(r["c_err_fixed"] for r in fixed), sum(r["c_ref"] for r in fixed))
        s["fix_p50"] = pct([r["fix_ms"] for r in fixed], 0.5)
    return s


def _f(v, nd=1, unit=""):
    return "-" if v is None else f"{v:.{nd}f}{unit}"


def table(sums: list, title: str) -> str:
    has_fix = any("wer_fixed" in s for s in sums)
    head = ["config", "n", "fail", "WER %", "CER %", "exact", "p50 ms", "p90 ms", "RTF"]
    if has_fix:
        head += ["WER % +LLM", "CER % +LLM", "LLM p50 ms"]
    out = [f"### {title}", "", "| " + " | ".join(head) + " |",
           "|" + "|".join(["---"] + ["---:"] * (len(head) - 1)) + "|"]
    for s in sorted(sums, key=lambda x: (x["wer"] is None, x["wer"] or 0)):
        row = [s["label"], str(s["n"]), str(s["failed"]), _f(s["wer"]), _f(s["cer"]),
               f"{s['exact']}/{s['n']}", _f(s["p50"], 0), _f(s["p90"], 0), _f(s["rtf"], 2)]
        if has_fix:
            row += [_f(s.get("wer_fixed")), _f(s.get("cer_fixed")), _f(s.get("fix_p50"), 0)]
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out)


def selftest() -> int:
    cases = [
        ("გამარჯობა გიორგი", "გამარჯობა გიორგი", 0, 0),
        ("გამარჯობა, გიორგი!", "გამარჯობა გიორგი", 0, 0),          # punctuation ignored
        ("Open Chrome", "open chrome", 0, 0),                      # case ignored
        ("გახსენი Chrome ახლა", "გახსენი ქრომი ახლა", 1, 6),        # 1 of 3 words
        ("ერთი ორი სამი", "ერთი სამი", 1, 3),                      # deletion
        ("ერთი ორი", "ერთი ორი სამი", 1, 4),                       # insertion
        ("რა ხდება", "", 2, 7),                                    # nothing heard
    ]
    bad = 0
    for ref, hyp, w, c in cases:
        e = errors(ref, hyp)
        ok = e["w_err"] == w and e["c_err"] == c
        bad += not ok
        print(("ok  " if ok else "FAIL"), repr(ref), "|", repr(hyp), "->", e["w_err"], e["c_err"],
              "" if ok else f"(expected {w} {c})")
    e = errors("ერთი ორი სამი ოთხი", "ერთი ორი სამი ხუთი")
    ok = abs(rate(e["w_err"], e["w_ref"]) - 25.0) < 1e-9
    bad += not ok
    print("ok  " if ok else "FAIL", "WER 1/4 = 25 %")
    import numpy as np
    a = (0.1 * np.sin(np.arange(SR) * 2 * np.pi * 220 / SR)).astype(np.float32)
    for name, fn in PREPS.items():
        out = fn(a)
        ok = len(out) >= len(a) and np.isfinite(out).all()
        bad += not ok
        print("ok  " if ok else "FAIL", f"prep {name}: {len(out)} samples")
    ok = len(clip_edges(a, 100)) == len(a) - 2 * 1600
    bad += not ok
    print("ok  " if ok else "FAIL", "clip_edges 100 ms")
    print("\nselftest", "FAILED" if bad else "passed")
    return 1 if bad else 0


ARGS = None


def main(argv: list) -> int:
    global ARGS, VOCAB
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # Georgian on a cp1252 console
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Georgian STT benchmark: WER, CER, latency.")
    ap.add_argument("folder", nargs="?", help="WAV files + references")
    ap.add_argument("--engines", default="current",
                    help="comma list, or 'all' (every available engine, cloud and paid "
                         "included), 'local' (no audio leaves the laptop), 'free'")
    ap.add_argument("--prep", default="raw", help="comma list of: " + ", ".join(PREPS))
    ap.add_argument("--clip-ms", default="0", help="comma list of ms cut off both ends")
    ap.add_argument("--llm-fix", action="store_true", help="add the LLM correction pass")
    ap.add_argument("--vocab", help="file with one name/term per line (vocabulary bias)")
    ap.add_argument("--repeats", type=int, default=1, help="runs per clip (fastest is kept)")
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--gemini-model", default="gemini-3.5-flash-lite")
    ap.add_argument("--fix-model", default="gemini-3.5-flash-lite")
    ap.add_argument("--onnx-model", default="OpenVoiceOS/stt_ka_fastconformer_hybrid_large_pc_onnx")
    ap.add_argument("--nemo-model", default="nvidia/stt_ka_fastconformer_hybrid_large_pc")
    ap.add_argument("--hf-model", default="BuzzASR/georgian")
    ap.add_argument("--out", help="output folder (default <folder>/_bench)")
    ap.add_argument("--list", action="store_true", help="list engines and exit")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--goat", help="GOAT checkout whose python/ modules, keys and models "
                                   "to use (default: the one this script is in; a git "
                                   "worktree has no keys or models of its own)")
    ARGS = ap.parse_args(argv[1:])
    VOCAB = ARGS.vocab
    if ARGS.goat:
        global PY
        PY = os.path.join(os.path.abspath(ARGS.goat), "python")
        if not os.path.isdir(PY):
            ap.error(f"--goat: no python/ folder in {ARGS.goat}")
    if ARGS.selftest:
        return selftest()
    if ARGS.list:
        for name, (why, fac, cloud, paid) in ENGINES.items():
            try:
                reason = why()
            except Exception as e:  # noqa: BLE001
                reason = f"{type(e).__name__}: {e}"
            print(f"{name:<18} {'cloud' if cloud else 'local':<5} {'PAID' if paid else 'free':<4} "
                  f"{'ready' if reason is None else 'unavailable: ' + reason}\n"
                  f"{'':<18} {' '.join((fac.__doc__ or '').split())}")
        return 0
    if not ARGS.folder:
        ap.error("folder is required")
    pick = ARGS.engines.strip().lower()
    names = (list(ENGINES) if pick == "all"
             else [n for n, e in ENGINES.items() if not e[2]] if pick == "local"
             else [n for n, e in ENGINES.items() if not e[3]] if pick == "free"
             else [n.strip() for n in ARGS.engines.split(",") if n.strip()])
    unknown = [n for n in names if n not in ENGINES]
    if unknown:
        ap.error(f"unknown engine(s): {', '.join(unknown)} (see --list)")
    preps = [p.strip() for p in ARGS.prep.split(",") if p.strip()]
    if any(p not in PREPS for p in preps):
        ap.error("unknown --prep (choose from " + ", ".join(PREPS) + ")")
    clips = [int(c) for c in ARGS.clip_ms.split(",") if c.strip()]

    items = load_set(ARGS.folder)
    total_s = sum(i["seconds"] for i in items)
    odd = sorted({(i["rate_in"], i["channels"]) for i in items if (i["rate_in"], i["channels"]) != (SR, 1)})
    print(f"{len(items)} utterances, {total_s:.0f} s of audio"
          + (f"; resampled/downmixed from {odd}" if odd else ""), file=sys.stderr)

    fix = llm_fixer() if ARGS.llm_fix else None
    results, skipped = [], []
    for name in names:
        why, factory, _cloud, _paid = ENGINES[name]
        try:
            reason = why()
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {e}"
        if reason is None:
            try:
                t0 = time.perf_counter()
                fn = factory()
                load_ms = (time.perf_counter() - t0) * 1000
            except Exception as e:  # noqa: BLE001
                reason = f"failed to load: {type(e).__name__}: {str(e)[:160]}"
        if reason is not None:
            skipped.append((name, reason))
            print(f"skipped {name}: {reason}", file=sys.stderr)
            continue
        if not ARGS.no_warmup:
            try:
                fn(items[0]["audio"])          # model load, sockets, caches: not timed
            except Exception:  # noqa: BLE001
                pass
        for p in preps:
            for c in clips:
                pname = PREP_NAME.get(p, lambda p=p: p)()
                label = name + ("" if p == "raw" else f" +{pname}") + (f" clip{c}" if c else "")
                res = run_config(label, fn, items, PREPS[p], c, fix, ARGS.repeats)
                res.update(engine=name, prep=pname, clip_ms=c, load_ms=round(load_ms))
                results.append(res)

    if not results:
        print("nothing ran", file=sys.stderr)
        return 1
    tags = sorted({t for i in items for t in i["tags"]})
    md = [f"# STT benchmark: {os.path.basename(os.path.abspath(ARGS.folder))}", "",
          f"{len(items)} utterances, {total_s:.0f} s of audio, {time.strftime('%Y-%m-%d %H:%M')}. "
          "WER/CER after normalization (lowercase, punctuation removed); a failed call counts "
          "as an empty transcript. Latency is wall time per clip on this machine, the model "
          "already loaded.", "",
          table([summarize(r) for r in results], "All utterances")]
    for t in tags:
        md += ["", table([summarize(r, t) for r in results], f"Tag: {t}")]
    if skipped:
        md += ["", "Skipped: " + "; ".join(f"{n} ({why})" for n, why in skipped)]
    text = "\n".join(md)
    print(text)
    out = ARGS.out or os.path.join(ARGS.folder, "_bench")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "results.md"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(os.path.join(out, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"when": time.time(), "folder": os.path.abspath(ARGS.folder),
                   "utterances": len(items), "audio_s": total_s,
                   "skipped": skipped, "results": results}, f, ensure_ascii=False, indent=1)
    print(f"\nsaved {out}\\results.md and results.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
