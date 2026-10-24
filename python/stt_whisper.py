"""Client for GOAT's own resident whisper.cpp server — model stays loaded,
transcription ~1s. Runs on its own port (3781) with its own upgrades, fully
independent of the old Node app's instance on 3779.

Accuracy stack (Giorgi's "learn how I actually talk" order, 2026-07-09):
1. Beam search (-bs 5) instead of near-greedy decoding.
2. A vocabulary prompt biasing whisper toward the words he actually says —
   seed lexicon below + every correction already learned in stt-fixes.json,
   so each learned fix also sharpens raw recognition on the next utterance.
3. stt-fixes.json corrections applied on every transcript (re-read each
   call — GOAT's brain appends new patterns mid-session, they apply
   immediately, no restart). The junk-hallucination filter is ported from
   server.js verbatim.

Model choice, measured 2026-07-09 on this CPU: base.en + bs5 + vocab prompt
= 1.2s/phrase and transcribed every test phrase perfectly; small.en = 3.9s
at any beam size (model compute dominates) for no observed accuracy gain
once the vocab prompt is in play. Conversation needs the 1.2s. small.en
stays on disk — set GOAT_STT_MODEL=small to trade latency for accuracy.
"""
import io
import json
import os
import re
import subprocess
import time

import httpx
import numpy as np
import dsp

from goat_paths import GOAT_ROOT

WHISPER_SERVER_BIN = os.path.join(GOAT_ROOT, "stt", "bin", "Release", "whisper-server.exe")
WHISPER_MODEL_SMALL = os.path.join(GOAT_ROOT, "stt", "ggml-small.en.bin")
WHISPER_MODEL_BASE = os.path.join(GOAT_ROOT, "stt", "ggml-base.en.bin")
# 8-bit quantized base.en (2026-10-01, RAM): measured on his own recordings
# with GOAT's real server args — same word errors (17/92 vs cloud Scribe),
# ~18% faster, 66 MB less resident (359 -> 293 MB). Used when present.
WHISPER_MODEL_BASE_Q8 = os.path.join(GOAT_ROOT, "stt", "ggml-base.en-q8_0.bin")
if os.path.exists(WHISPER_MODEL_BASE_Q8):
    WHISPER_MODEL_BASE = WHISPER_MODEL_BASE_Q8
# Multilingual models — needed for Georgian. Measured 2026-07-10: base
# multilingual transcribes Georgian audio into LATIN transliteration
# (useless); small multilingual is required for Georgian script. base kept
# as a last-resort fallback only.
WHISPER_MODEL_MULTI = os.path.join(GOAT_ROOT, "stt", "ggml-base.bin")
WHISPER_MODEL_MULTI_SMALL = os.path.join(GOAT_ROOT, "stt", "ggml-small.bin")
LANGUAGE = "en"  # set via set_language(); "ka" switches model + decode lang

# GPU EAR (2026-10-24, his order: "I want to use OpenAI's Whisper"). The
# cloud ear (ElevenLabs Scribe) ran out of credits and every turn fell to
# base.en on the CPU — English only, and it misheard him ("Okay, Sannet" for
# "send it"). OpenAI's Whisper large-v3-turbo on his RTX 3050 through the
# CUDA build of whisper.cpp: free and offline. Measured on his own 20
# recordings (2026-10-24): median 1.25 s vs 1.75 s for base.en on the CPU,
# and long sentences near-perfect where base.en garbled them. Georgian is
# beyond it — forced "ka" gave "ეეეეეე" loops — so it is the ENGLISH ear;
# Georgian stays with the cloud ear. The vocabulary prompt made it worse
# ("How are you GOAT?" -> "Hóaí, Björn"), so the GPU ear sends none.
# Used when both files are present; GOAT_STT_GPU=off brings back the CPU ear.
WHISPER_GPU_BIN = os.path.join(GOAT_ROOT, "stt", "bin-cuda", "whisper-server.exe")
WHISPER_MODEL_TURBO = os.path.join(GOAT_ROOT, "stt", "ggml-large-v3-turbo-q8_0.bin")
GPU = (os.environ.get("GOAT_STT_GPU", "on").strip().lower() not in ("off", "0", "no")
       and os.path.exists(WHISPER_GPU_BIN) and os.path.exists(WHISPER_MODEL_TURBO))
STT_PORT = 3781
STT_URL = f"http://127.0.0.1:{STT_PORT}/inference"
FIXES_FILE = os.path.join(GOAT_ROOT, "stt-fixes.json")

# Words that keep getting mangled — names, project terms, how he talks.
# The brain adds to this indirectly: every stt-fixes.json value is folded
# into the prompt at server start.
SEED_VOCAB = [
    "GOAT", "Giorgi", "KingKaglu", "Claude", "Fable 5", "Sonnet", "Anthropic",
    "Python", "whisper", "Piper", "Ava", "echo cancellation", "barge-in",
    "restart GOAT", "workspace", "fullscreen", "design", "app", "model",
]


def _vocab_prompt() -> str:
    words = list(SEED_VOCAB)
    try:
        with open(FIXES_FILE, encoding="utf-8") as f:
            fixes = json.load(f)
        for k, v in fixes.items():
            if not k.startswith("_") and v not in words:
                words.append(v)
    except (OSError, json.JSONDecodeError):
        pass
    return "Conversation with GOAT, a JARVIS-style assistant. Vocabulary: " + ", ".join(words) + "."

JUNK = re.compile(
    r"^(you|bye(\s*bye)?|thank you\.?|thanks?( for watching)?|and build\.?|a|the"
    r"|okay\.?|so\.?|yeah\.?|\.+|,+"
    # large-v3-turbo's subtitle-credit inventions over silence (2026-10-24)
    r"|sous-titrage.*|subtitles? by.*|.*amara\.org.*|transcribed by.*"
    r"|(\W*(m+|u+h+|u+m+|h+m+|a+h+|o+h+|e+h+)m*[.,!?]*\s*)+)$",
    re.IGNORECASE,
)

_server_proc = None


def ensure_server() -> bool:
    """Spawn GOAT's own whisper-server (port 3781) if it isn't up yet.
    Returns True when a server is reachable, False when hearing is dead —
    the caller must SAY so, not just log it (a deaf GOAT looks alive)."""
    global _server_proc
    try:
        httpx.get(f"http://127.0.0.1:{STT_PORT}/", timeout=1.0)
        return True  # something is listening — good enough
    except (httpx.ConnectError, httpx.TimeoutException):
        pass  # dead or unresponsive — spawn our own
    except httpx.HTTPError:
        return True  # listening but grumpy about GET — still a live server

    if GPU:
        print(f"[stt] starting whisper-server (GPU) on {STT_PORT} with "
              f"{os.path.basename(WHISPER_MODEL_TURBO)}, language per request")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _server_proc = subprocess.Popen(
            # -nf: no temperature-fallback re-decodes — on hard audio they
            # turned 1.2 s into 4 s (and Georgian into 20 s loops).
            [WHISPER_GPU_BIN, "-m", WHISPER_MODEL_TURBO, "-t", "4", "-nt",
             "-bs", "5", "-nf", "-l", "en",
             "--host", "127.0.0.1", "--port", str(STT_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        return _wait_up(60)
    if LANGUAGE != "en" and (os.path.exists(WHISPER_MODEL_MULTI_SMALL)
                             or os.path.exists(WHISPER_MODEL_MULTI)):
        model = (WHISPER_MODEL_MULTI_SMALL
                 if os.path.exists(WHISPER_MODEL_MULTI_SMALL)
                 else WHISPER_MODEL_MULTI)
        lang_args = ["-l", LANGUAGE]
        prompt_args = []  # the English vocab prompt would fight Georgian
    else:
        model = (WHISPER_MODEL_SMALL
                 if os.environ.get("GOAT_STT_MODEL") == "small" and os.path.exists(WHISPER_MODEL_SMALL)
                 else WHISPER_MODEL_BASE)
        lang_args = []
        prompt_args = ["--prompt", _vocab_prompt(), "--carry-initial-prompt"]
    print(f"[stt] starting whisper-server on {STT_PORT} with {os.path.basename(model)} lang={LANGUAGE}")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _server_proc = subprocess.Popen(
        [WHISPER_SERVER_BIN, "-m", model, "-t", "6", "-nt",
         "-bs", "5", *lang_args, *prompt_args,
         "--host", "127.0.0.1", "--port", str(STT_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    # small.en takes a bit longer to load than base
    return _wait_up(30)


def _wait_up(seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _server_proc is not None and _server_proc.poll() is not None:
            break                  # it died on start (bad model, no CUDA)
        try:
            httpx.get(f"http://127.0.0.1:{STT_PORT}/", timeout=1.0)
            return True
        except httpx.ConnectError:
            time.sleep(0.5)
        except httpx.HTTPError:
            return True
    print(f"[stt] whisper-server did not come up in {seconds:.0f}s — "
          "transcription will fail")
    return False


def set_language(lang: str) -> bool:
    """Switch decode language and restart OUR server on the right model.
    Blocking (model load ~5s) — call from a worker thread. If the server on
    the port isn't ours it can't be restarted here; GOAT's own boot always
    owns it in practice."""
    global LANGUAGE
    if lang == LANGUAGE:
        return True
    LANGUAGE = lang
    if GPU:
        return ensure_server()     # one model for every language: no restart
    shutdown()
    time.sleep(0.4)  # let the port go
    return ensure_server()


def shutdown():
    """Kill our own whisper-server if we spawned one (no-op if we reused a
    live server that something else owns)."""
    global _server_proc
    if _server_proc is not None and _server_proc.poll() is None:
        _server_proc.terminate()
    _server_proc = None


def _apply_fixes(text: str) -> str:
    try:
        with open(FIXES_FILE, encoding="utf-8") as f:
            fixes = json.load(f)
    except (OSError, json.JSONDecodeError):
        return text
    for wrong, right in fixes.items():
        if wrong.startswith("_"):
            continue
        text = re.sub(r"\b" + re.escape(wrong) + r"\b", right, text, flags=re.IGNORECASE)
    return text


# One-word replies that are real speech, not noise: the old rule (a single
# word under 8 letters is junk) threw away "yes", "no", "stop", "wait" and
# "next" — the exact words a voice assistant lives on (2026-10-18 audit).
SHORT_OK = re.compile(
    r"^\W*(yes|yep|yeah|no|nope|stop|wait|cancel|pause|play|next|back|go|"
    r"continue|resume|repeat|louder|quieter|mute|unmute|hello|hi|hey|goat|"
    r"sir|sure|right|really|why|how|what|who|when|where|again|more|done|"
    r"help|thanks?|thank you|okay|ok)\W*$", re.IGNORECASE)
# Whisper's favourite inventions over noise. Still dropped, unless he is
# mid-conversation (see clean_transcript(chatting=...)).
_HALLUCINATED = re.compile(r"^\W*(thanks?( for watching)?|thank you|you|okay|ok|yeah|"
                           r"bye(\s*bye)?|and build)\W*$", re.IGNORECASE)


def clean_transcript(raw: str, chatting: bool = False) -> str:
    """Raw whisper text -> what he said, or "" for silence/junk.

    chatting=True means I spoke to him in the last half minute: a short
    "thank you" / "okay" is then a real reply, not whisper hallucinating over
    a quiet room, so it gets through."""
    text = re.sub(r"\[BLANK_AUDIO\]|\[.*?\]|\(.*?\)|\*[^*]{0,40}\*", "", raw)
    text = re.sub(r"^\s*-\s+", "", text)      # turbo's subtitle-style dash
    text = re.sub(r"\s+", " ", text).strip()
    words = [w for w in text.split(" ") if w]
    if text and SHORT_OK.match(text) and (chatting or not _HALLUCINATED.match(text)):
        return _apply_fixes(text)
    if JUNK.match(text) or (len(words) < 2 and len(text) < 8
                            and not SHORT_OK.match(text)):
        if text:
            print("[stt] dropped junk:", json.dumps(text))
        return ""
    return _apply_fixes(text)


_chatting_until = 0.0


def note_chatting(seconds: float = 30.0):
    """Called when I speak: for a while a short thank-you is a real reply."""
    global _chatting_until
    _chatting_until = time.monotonic() + seconds


def transcribe(audio: np.ndarray, sample_rate: int = 16000):
    """float32 [-1,1] mono → text. '' means silence/junk (normal, stay
    quiet); None means the STT server is BROKEN (the caller should tell
    Giorgi his hearing is down instead of silently eating his words).
    Blocking — call from a worker thread, not the event loop."""
    if GPU:
        return transcribe_lang(audio, sample_rate, "en")[0]
    got = _post(audio, sample_rate, {"response_format": "json"})
    if got is None or isinstance(got, str):
        return got
    return clean_transcript(got.get("text", ""), time.monotonic() < _chatting_until)


_LANG_NAMES = {"english": "en", "georgian": "ka", "en": "en", "ka": "ka"}


def transcribe_lang(audio: np.ndarray, sample_rate: int = 16000,
                    force: str | None = None):
    """GPU ear, any language: (text, "en"|"ka"|other) — text '' for silence,
    None when the server is broken. force pins the language ("en"/"ka");
    None lets Whisper detect it (bilingual mode)."""
    data = {"response_format": "verbose_json", "language": force or "auto"}
    got = _post(audio, sample_rate, data)
    if got is None or isinstance(got, str):
        return got, force or ""
    text = clean_transcript(got.get("text", ""), time.monotonic() < _chatting_until)
    lang = str(got.get("detected_language") or got.get("language") or force or "")
    lang = _LANG_NAMES.get(lang.strip().lower(), lang.strip().lower())
    if re.search(r"[Ⴀ-ჿ]", text):
        lang = "ka"                        # Georgian letters settle it
    return text, lang


def _post(audio: np.ndarray, sample_rate: int, data: dict):
    """One request: the JSON reply, '' for audio too short to bother, None
    when the server is broken."""
    if len(audio) < sample_rate * 0.3:
        return ""
    buf = io.BytesIO()
    pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    dsp.write_wav16(buf, sample_rate, pcm)
    for attempt in (1, 2):
        try:
            r = httpx.post(
                STT_URL,
                files={"file": ("audio.wav", buf.getvalue(), "audio/wav")},
                data=data,
                timeout=30.0,
            )
            r.raise_for_status()
            return r.json()
        except httpx.ConnectError as e:
            if attempt == 1:
                print("[stt] server gone — restarting it")
                ensure_server()
                continue
            print("[stt] request failed:", e)
            return None
        except httpx.HTTPError as e:
            print("[stt] request failed:", e)
            return None
    return None
