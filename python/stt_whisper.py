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
import asyncio
import concurrent.futures
import io
import json
import os
import re
import subprocess
import threading
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

# One client for the life of the process (2026-10-25). httpx.post() builds a
# fresh Client per call, and on this laptop that alone took 0.25-0.8 s (SSL
# context + Windows proxy lookup) — the server answers his 3 s sentence in
# 0.3 s, so the ear's "1.1-1.5 s" was mostly that. Local server: no proxy.
_http = httpx.Client(trust_env=False, timeout=30.0)


def ensure_server() -> bool:
    """Spawn GOAT's own whisper-server (port 3781) if it isn't up yet.
    Returns True when a server is reachable, False when hearing is dead —
    the caller must SAY so, not just log it (a deaf GOAT looks alive)."""
    global _server_proc
    try:
        _http.get(f"http://127.0.0.1:{STT_PORT}/", timeout=1.0)
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
            _http.get(f"http://127.0.0.1:{STT_PORT}/", timeout=1.0)
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


EN_PROB_MIN = 0.10      # Whisper's English score at/above this = English
NOISY_EN_MAX = 0.35     # below this, not detected English: ask the Live ear
                        # (his noisy Georgian 0.13-0.33; his English, noisy
                        # or not, is detected English, 2026-10-25)
# His Georgian said in English mode goes to the Live ear — his order
# (2026-10-25 evening: it came out as English nonsense). On by default;
# GOAT_STT_EN_KA_CLOUD=off keeps English mode's audio local only.
EN_KA_CLOUD = os.environ.get("GOAT_STT_EN_KA_CLOUD", "on").strip().lower() \
    in ("on", "1", "true", "yes")


# Speech nearby in a language he doesn't use with GOAT (2026-10-25, Essen:
# the phone heard the Germans around him, and the English-pinned ear turned
# "Was machst du da?" into "What's was marks to diga?" — fake English that
# then reached the voice as his words). Whisper names German with certainty:
# 1.00 on German speech, while his English scores 0.00-0.02 German and his
# Georgian (which it calls "Russian") 0.02-0.03. Russian can't be on this
# list for exactly that reason.
FOREIGN = {"de": 0.6}
FOREIGN_EN_MAX = 0.2


def _foreign(got: dict) -> str | None:
    lp = got.get("language_probabilities") or {}
    if float(lp.get("en", 0.0) or 0.0) >= FOREIGN_EN_MAX:
        return None
    for code, p_min in FOREIGN.items():
        if float(lp.get(code, 0.0) or 0.0) >= p_min:
            return code
    return None


def transcribe_lang(audio: np.ndarray, sample_rate: int = 16000,
                    force: str | None = None):
    """GPU ear, any language: (text, "en"|"ka"|other) — text '' for silence,
    None when the server is broken. force pins the language ("en"/"ka");
    None lets Whisper detect it (bilingual mode). Speech in a FOREIGN
    language comes back as ("", code): someone near him, not him."""
    if force == "en":
        # Listen first, pin second: one request when it IS English (most
        # turns), a second pinned one only when Whisper guessed otherwise.
        got = _post(audio, sample_rate, {"response_format": "verbose_json",
                                         "language": "auto"})
        if got is None or isinstance(got, str):
            return got, "en"
        code = _foreign(got)
        if code:
            print(f"[stt] {code} speech nearby, not him: "
                  f"{(got.get('text') or '').strip()[:80]!r}", flush=True)
            return "", code
        if str(got.get("detected_language") or "").lower() == "english":
            return clean_transcript(got.get("text", ""),
                                    time.monotonic() < _chatting_until), "en"
        p_en = float((got.get("language_probabilities") or {}).get("en", 0.0) or 0.0)
        if p_en < EN_PROB_MIN:
            # Not English at all: his Georgian (2026-10-25 evening: in English
            # mode his spoken Georgian was pinned to English and came out as
            # nonsense; Whisper scores it 0.05-0.07 English). Cloud Georgian
            # recognition is opt-in in English mode to preserve local-only
            # speech by default.
            if EN_KA_CLOUD:
                ka = _georgian(audio, sample_rate)
                if ka is not None:
                    return ka, "ka"
        elif p_en < NOISY_EN_MAX:
            # Not called English, but with talk around him the English score
            # climbs: his Georgian mixed with GOAT's own voice or the room's
            # read 0.13-0.33 English (detected russian/turkish) and came out
            # as "Kamarjova Giyogi" (2026-10-25, his clips + the room
            # recording). The Live ear decides: Georgian letters = Georgian;
            # anything else is English after all.
            if EN_KA_CLOUD:
                ka = _georgian(audio, sample_rate, script_only=True)
                if ka is not None:
                    return ka, "ka"
        return _pinned(audio, sample_rate, "en")
    if force:
        return _pinned(audio, sample_rate, force)
    got = _post(audio, sample_rate, {"response_format": "verbose_json", "language": "auto"})
    if got is None or isinstance(got, str):
        return got, ""
    code = _foreign(got)
    if code:
        print(f"[stt] {code} speech nearby, not him: "
              f"{(got.get('text') or '').strip()[:80]!r}", flush=True)
        return "", code
    text = clean_transcript(got.get("text", ""), time.monotonic() < _chatting_until)
    # Bilingual: turbo can't name Georgian (p=0.000 on every Georgian clip,
    # top guess "icelandic"/"russian"), but its English score separates his
    # two languages — English 0.12-1.00 (one-word "Opus" 0.12), Georgian
    # 0.017-0.051 (his own recordings, 2026-10-24). Not English = Georgian,
    # and the caller hands Georgian to the cloud ear.
    p_en = float((got.get("language_probabilities") or {}).get("en", 0.0) or 0.0)
    lang = "en" if p_en >= EN_PROB_MIN else "ka"
    if re.search(r"[Ⴀ-ჿ]", text):
        return text, "ka"                  # Georgian letters settle it
    if lang == "en" and str(got.get("detected_language") or "").lower() != "english":
        # English, but decoded in the language it wrongly guessed
        # ("Opus" -> "Obrigado."): once more, pinned to English.
        return _pinned(audio, sample_rate, "en")
    return text, lang


_ka_at = 0.0         # when he last spoke Georgian
KA_WARM_S = 300.0    # he spoke Georgian this recently: dial Live as he starts


def _georgian(audio: np.ndarray, sample_rate: int, script_only: bool = False):
    """Georgian text from the cloud ear, or None when it can't be had.

    Gemini Live's own transcription first (stt_live): on his 2026-10-25
    clips it wrote "როგორ ხარ, რას შვები?" and "გთხოვ, მითხარი რომელი საათია
    ახლა?" in 2.2-4.5 s, where Gladia wrote "რომ ხარობრაშ შვიმი" and
    "გოტმით ხარი რომ ელი საათი ახლა" in 5-9 s. Gladia stays the fallback.
    script_only: the clip may be English — Georgian letters or None, and
    no Gladia (pinned to Georgian, it would write English as Georgian)."""
    try:
        import stt_live
        t0 = time.monotonic()
        text = stt_live.transcribe(audio, sample_rate)
        if script_only and not (text and re.search(r"[Ⴀ-ჿ]", text)):
            print(f"[stt] Live ear says not Georgian ({time.monotonic() - t0:.1f}s): "
                  f"{(text or '')[:60]!r}", flush=True)
            return None
        if text:
            global _ka_at
            _ka_at = time.monotonic()
            print(f"[stt] Georgian via Live in {time.monotonic() - t0:.1f}s: "
                  f"{text[:60]!r}", flush=True)
            return clean_transcript(text, True)
    except Exception as e:  # noqa: BLE001 — Gladia below
        print(f"[stt] Live Georgian ear failed: {e!r}", flush=True)
    if script_only:
        return None
    try:
        import stt_gladia
        if not stt_gladia.available():
            return None
        text, _ = stt_gladia.transcribe_lang(audio, sample_rate, "ka")
        return text
    except Exception as e:  # noqa: BLE001 — English pinning is the fallback
        print(f"[stt] Georgian ear failed: {e!r}", flush=True)
        return None


def _pinned(audio: np.ndarray, sample_rate: int, lang: str):
    got = _post(audio, sample_rate, {"response_format": "verbose_json", "language": lang})
    if got is None or isinstance(got, str):
        return got, lang
    return clean_transcript(got.get("text", ""), time.monotonic() < _chatting_until), lang


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
            r = _http.post(
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


# ---------------------------------------------------------------------------
# Listening while he talks (2026-10-25, his order: "the other one listens and
# thinks at the same time ... when I am done giving the task I have the
# answer immediately"). The local Whisper ear transcribes the utterance the moment he
# pauses (GOAT's soft end, 200 ms into a silence) instead of after the turn
# is called (500 ms) — so the text is usually ready the instant the turn
# ends, and the partial tells the endpointer whether the sentence sounds
# finished ("...and I want to" waits the long hangover). It works with the
# CPU and CUDA whisper-server; same interface as stt_realtime.Session.
# ---------------------------------------------------------------------------

_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper-live")
STREAM_RESULT_S = 8.0       # the final transcript never waits longer than this


class Stream:
    """One utterance on local Whisper. feed/soft_commit/end are called from the
    audio thread and only append or hand off; the work runs on _POOL."""

    local = True

    def __init__(self, loop, lang: str = "en"):
        self.loop = loop
        self.lang = lang
        self.last_partial = ""
        self.error: str | None = None
        self._chunks: list = []
        self._lock = threading.Lock()
        self._latest = None          # (n_chunks, Future) of the newest job
        self._closed = False
        self.t_commit_sent = 0.0
        self.heard = lang             # the language the newest partial was in
        if EN_KA_CLOUD and time.monotonic() - _ka_at < KA_WARM_S:
            # he's talking Georgian lately: have the Live ear's session
            # dialled while he speaks (saves ~1.4 s, measured 3.2 -> 1.75 s)
            try:
                import stt_live
                stt_live.prime()
            except Exception:  # noqa: BLE001 — the turn dials its own
                pass

    @property
    def pending(self) -> bool:
        j = self._latest
        return j is not None and not j[1].done()

    # ---- audio thread ----
    def feed(self, chunk):
        if not self._closed:
            self._chunks.append(chunk)

    def soft_commit(self):
        if not self._closed:
            self._submit()

    def end(self):
        if self._closed:
            return
        self._closed = True
        # The newest soft commit already holds every voiced block: GOAT only
        # calls the turn after a silence the soft commit fires inside of. A
        # turn without one (the 30 s cap) is transcribed whole.
        if self._latest is None:
            self._submit()

    def drop(self):
        """A cough, not a turn: nothing to transcribe."""
        self._closed = True

    # ---- worker ----
    def _submit(self):
        n = len(self._chunks)
        if n == 0:
            return
        self.t_commit_sent = time.monotonic()
        fut = _POOL.submit(self._run, n)
        with self._lock:
            self._latest = (n, fut)

    def _run(self, n: int):
        with self._lock:
            latest = self._latest
        if latest is not None and latest[0] != n:
            return None              # superseded before it started: skip
        audio = np.concatenate(self._chunks[:n])
        try:
            text, heard = transcribe_lang(audio, 16000, self.lang)
        except Exception as e:  # noqa: BLE001 — the batch ear still has the audio
            self.error = f"{type(e).__name__}: {e}"
            return None
        if text is None:
            self.error = "whisper server failed"
            return None
        with self._lock:
            # The executor can begin the worker before _submit stores its
            # Future in _latest. Treat None as that initial submit race, but
            # still ignore any genuinely superseded partial.
            if self._latest is None or self._latest[0] == n:
                self.last_partial = text
                if heard in ("en", "ka"):
                    self.heard = heard
        return text

    async def result(self):
        """The transcript, or None to mean 'use the batch ear'."""
        j = self._latest
        if j is None:
            return None
        try:
            text = await asyncio.wait_for(asyncio.wrap_future(j[1]), STREAM_RESULT_S)
        except (asyncio.TimeoutError, concurrent.futures.CancelledError):
            self.error = self.error or f"no answer in {STREAM_RESULT_S:.0f}s"
            return None
        if text is None:
            return None
        ms = (time.monotonic() - self.t_commit_sent) * 1000
        print(f"[stt-local] heard {ms:.0f}ms after his pause: {text[:60]!r}")
        return text

    def why(self) -> str:
        return f"{self.lang}: {self.error or 'nothing heard'}"
