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
    if lang != "en":
        _warm_local()
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
    r"help|thanks?|thank you|okay|ok|"
    # his Georgian yes / no / stop / wait / ok / thanks
    r"კი|ხო|ჰო|დიახ|არა|არ|გაჩერდი|მოიცა|კარგი|მადლობა|გამარჯობა)\W*$", re.IGNORECASE)
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
    # A one-word Georgian reply is a reply: "ბოლოში" (from the Live ear,
    # 2026-10-26) was dropped by the short-junk rule, which is about
    # Whisper's English inventions over noise.
    if JUNK.match(text) or (len(words) < 2 and len(text) < 8
                            and not SHORT_OK.match(text)
                            and not re.search(r"[Ⴀ-ჿ]{3}", text)):
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

# Sticky English (2026-11-06 audit V1/V2). The language check above is a
# second encoder pass: on his own clips, on this GPU, the "auto" request took
# 1310 ms where the same clip pinned to English took 495 ms. Every laptop turn
# paid it, and the 800 ms extra kept the ear's job pending past the 500 ms
# end-of-turn mark, so EVERY turn waited the long 1.2 s (56 of 69 endpoints
# that night ended on "local-pending"). Once a turn comes back confidently
# English, the laptop's streaming ear pins English for STICKY_S; every
# STICKY_RECHECK-th utterance still runs the full check, and Georgian lately
# (KA_WARM_S) turns it off. The phone and batch ears always check.
# GOAT_STT_EN_STICKY=off = check every turn, as before.
EN_STICKY = os.environ.get("GOAT_STT_EN_STICKY", "on").strip().lower() \
    in ("on", "1", "true", "yes")
STICKY_S = 120.0
STICKY_P_EN = 0.9
STICKY_RECHECK = 4
_en_sure_until = 0.0
_sticky_n = 0


def sticky_en() -> bool:
    """May THIS utterance skip the language check? (one call per utterance)"""
    global _sticky_n
    now = time.monotonic()
    if not EN_STICKY or now >= _en_sure_until or now - _ka_at < KA_WARM_S:
        return False
    _sticky_n += 1
    return _sticky_n % STICKY_RECHECK != 0


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
                    force: str | None = None, cloud: bool = True,
                    fast: bool = False):
    """GPU ear, any language: (text, "en"|"ka"|other) — text '' for silence,
    None when the server is broken. force pins the language ("en"/"ka");
    None lets Whisper detect it (bilingual mode). Speech in a FOREIGN
    language comes back as ("", code): someone near him, not him.
    cloud=False (the stream's partials): a clip that may be his Georgian is
    NOT sent to the Georgian ear; it comes back pinned English with "ka?"
    (or "ka?script", the noisy band) for the caller to settle once.
    fast=True (sticky_en said so): English pinned, no language check."""
    if force == "en" and fast:
        got = _pinned(audio, sample_rate, "en")
        if got[0] and _ASKS_KA.search(got[0]):
            note_georgian()                 # "talk to me in Georgian": he's switching
        return got
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
                  f"{(got.get('text') or '').strip()[:80]!a}", flush=True)
            return "", code
        p_en = float((got.get("language_probabilities") or {}).get("en", 0.0) or 0.0)
        if str(got.get("detected_language") or "").lower() == "english":
            if _ASKS_KA.search(got.get("text") or ""):
                note_georgian()
            elif p_en >= STICKY_P_EN and (got.get("text") or "").strip():
                global _en_sure_until
                _en_sure_until = time.monotonic() + STICKY_S
            return clean_transcript(got.get("text", ""),
                                    time.monotonic() < _chatting_until), "en"
        # Below EN_PROB_MIN: not English at all — his Georgian (2026-10-25
        # evening: pinned to English it came out as nonsense; Whisper scores
        # it 0.05-0.07 English). Up to NOISY_EN_MAX: not called English, but
        # with talk around him the English score climbs (his Georgian over
        # GOAT's voice read 0.13-0.33 and came out "Kamarjova Giyogi"), so the
        # Live ear decides: Georgian letters = Georgian, else English after all.
        if p_en < NOISY_EN_MAX and EN_KA_CLOUD:
            noisy = p_en >= EN_PROB_MIN
            if not cloud:
                _warm_local()               # a partial that may be his Georgian
                if noisy:                   # may be English after all: its fallback
                    text, _ = _pinned(audio, sample_rate, "en")
                    return text, "ka?script"
                # Plainly not English (p_en < 0.10): a second, English-pinned
                # decode only made Georgian nonsense, ~0.3 s on every one of
                # his Georgian partials (2026-11-08 bench, 3 clips: auto 0.78 s
                # + pinned 0.26-0.32 s). The Georgian ear writes the real text.
                return clean_transcript(got.get("text", ""),
                                        time.monotonic() < _chatting_until), "ka?"
            ka = _georgian(audio, sample_rate, script_only=noisy)
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
              f"{(got.get('text') or '').strip()[:80]!a}", flush=True)
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


def note_georgian() -> None:
    """Georgian is in the air (he spoke it, asked for it, or the voice answered
    in it): stop pinning English. 2026-11-08 bug: _ka_at was only set when
    the Live ear wrote the Georgian, so after a REST/Gladia Georgian turn the
    sticky English pin stayed on and his next three Georgian sentences came
    out as English nonsense ("Wowun Jobe Shinkatuli Semenis Enginy")."""
    global _ka_at, _en_sure_until
    _ka_at = time.monotonic()
    _en_sure_until = 0.0
    _warm_local()


# an English sentence that asks for Georgian ("talk to me in Georgian")
_ASKS_KA = re.compile(r"\b(georgian|kartuli|qartuli)\b|ქართ", re.I)


def _ka_result(text):
    """Georgian text from any ear: remember it, pass it on."""
    if text and re.search(r"[Ⴀ-ჿ]", text):
        note_georgian()
    return text


def _live_job(audio: np.ndarray):
    """The Live Georgian ear on one clip -> (text or None, seconds)."""
    import stt_live
    t0 = time.monotonic()
    return stt_live.transcribe(audio, 16000), time.monotonic() - t0


LIVE_WAIT_S = 20.0   # a raced Live job never holds the turn longer (its own cap is ~18 s)

# The local Georgian ear (stt_fastconformer, 2026-11-10 audit A1/A4). Measured
# on 30 FLEURS clips (340 s of read Georgian) on his laptop, same clips for all:
#                 WER     CER    median time   empty answers
#   Live ear      11.7 %  6.6 %  4958 ms       1
#   local ear     17.6 %  5.5 %   339 ms       0      (clips of 7-18 s)
# On the 22 clips without numerals or Latin words: 12.7 % against 13.9 % WER,
# 7.9 % against 3.4 % CER. It writes numbers as words and knows no English.
# So it goes first where the clip is plainly Georgian, the cloud ears stay as
# its fallback, and Georgian works with no network at all.
# On when its model files are there; GOAT_STT_KA_LOCAL=off = cloud ears only.
KA_LOCAL = os.environ.get("GOAT_STT_KA_LOCAL", "on").strip().lower() \
    in ("on", "1", "true", "yes")


def _warm_local() -> None:
    """Georgian is likely: load the local ear's model in the background
    (about two seconds, once) so his sentence does not wait for it."""
    if not KA_LOCAL:
        return
    try:
        import stt_fastconformer
        stt_fastconformer.warm_async()
    except Exception:  # noqa: BLE001 — it loads on first use instead
        pass


def _local_georgian(audio: np.ndarray, sample_rate: int):
    """Georgian text from the local ear, or None: switched off, no model,
    failed, or it heard no Georgian (the cloud ears get the clip then)."""
    if not KA_LOCAL:
        return None
    try:
        import stt_fastconformer
        if not stt_fastconformer.available():
            return None
        t0 = time.monotonic()
        text = stt_fastconformer.transcribe(audio, sample_rate)
    except Exception as e:  # noqa: BLE001 — the cloud ears below
        print(f"[stt] local Georgian ear failed: {e!r}", flush=True)
        return None
    if not (text and re.search(r"[Ⴀ-ჿ]", text)):
        return None
    print(f"[stt] Georgian via the local ear in {time.monotonic() - t0:.2f}s: "
          f"{text[:60]!a}", flush=True)
    return text


def _georgian(audio: np.ndarray, sample_rate: int, script_only: bool = False,
              live=None):
    """Georgian text from the cloud ear, or None when it can't be had.

    Gemini Live's own transcription first (stt_live): on his 2026-10-25
    clips it wrote "როგორ ხარ, რას შვები?" and "გთხოვ, მითხარი რომელი საათია
    ახლა?" in 2.2-4.5 s, where Gladia wrote "რომ ხარობრაშ შვიმი" and
    "გოტმით ხარი რომ ელი საათი ახლა" in 5-9 s. Gladia stays the fallback.
    script_only: the clip may be English — Georgian letters or None, and
    no Gladia (pinned to Georgian, it would write English as Georgian).
    live: a Future of _live_job already running on this clip (Stream started
    it as he paused): its answer is used instead of asking Live again.

    The local Georgian ear answers first when its model is installed
    (_local_georgian); everything below is then its fallback. Not for
    script_only clips: it writes English speech in Georgian letters too, so
    it cannot say which language a doubtful clip is."""
    if not script_only:
        text = _local_georgian(audio, sample_rate)
        if text:
            note_georgian()
            return clean_transcript(text, True)
    try:
        if live is not None:
            text, secs = live.result(timeout=LIVE_WAIT_S)
        else:
            text, secs = _live_job(audio) if sample_rate == 16000 else (None, 0.0)
        if script_only and not (text and re.search(r"[Ⴀ-ჿ]", text)):
            print(f"[stt] Live ear says not Georgian ({secs:.1f}s): "
                  f"{(text or '')[:60]!a}", flush=True)
            return None
        if text:
            note_georgian()
            print(f"[stt] Georgian via Live in {secs:.1f}s: "
                  f"{text[:60]!a}", flush=True)
            return clean_transcript(text, True)
    except Exception as e:  # noqa: BLE001 — Gladia below
        print(f"[stt] Live Georgian ear failed: {e!r}", flush=True)
    if script_only:
        return None
    # Live is resting or failed: REST Gemini and Gladia together. REST writes
    # his Georgian far better but its time swings (4-29 s), so it gets
    # REST_KA_WAIT_S; past that, Gladia's answer goes.
    rest = None
    if sample_rate == 16000:
        try:
            import stt_live
            rest = _KA_POOL.submit(stt_live.transcribe_rest, audio, REST_KA_WAIT_S + 1)
        except Exception:  # noqa: BLE001
            rest = None

    def gladia():
        import stt_gladia
        if not stt_gladia.available():
            return None
        return stt_gladia.transcribe_lang(audio, sample_rate, "ka")[0]
    glad = _KA_POOL.submit(gladia)
    if rest is not None:
        try:
            text = rest.result(timeout=REST_KA_WAIT_S)
            if text and re.search(r"[Ⴀ-ჿ]", text):
                # ASCII escapes keep this diagnostic from raising under the
                # Windows console's cp1252 encoding and dropping good text.
                print(f"[stt] Georgian via REST Gemini: {text[:60]!a}", flush=True)
                return _ka_result(clean_transcript(text, True))
        except Exception:  # noqa: BLE001 — too slow or failed: Gladia
            pass
    try:
        return _ka_result(glad.result(timeout=30))
    except Exception as e:  # noqa: BLE001 — English pinning is the fallback
        print(f"[stt] Georgian ear failed: {e!r}", flush=True)
        return None


REST_KA_WAIT_S = 4.0
_KA_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="ka-ear")


def _pinned(audio: np.ndarray, sample_rate: int, lang: str):
    # Plain "json" (2026-11-06): only the text is read here, and verbose_json
    # cost ~400 ms more per clip on his GPU (890 vs ~500 ms, his own clips).
    got = _post(audio, sample_rate, {"response_format": "json", "language": lang})
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
        self._maybe_ka: dict = {}     # n_chunks -> "ka?" / "ka?script" / None
        self.fast = lang == "en" and sticky_en()   # pinned English this utterance
        # Georgian lately: the Live ear hears each pause's audio AS he pauses,
        # side by side with Whisper, instead of only after Whisper's language
        # check and the end of the turn (2026-11-08 log: every Georgian turn
        # was Whisper ~1.1 s, then the 1.2 s endpoint, then Live 1-3.8 s, in a
        # row; 4-10 s from his pause to his words).
        self._ka_warm = (lang == "en" and EN_KA_CLOUD and not self.fast
                         and time.monotonic() - _ka_at < KA_WARM_S)
        self._live = None             # (n_chunks, Future of _live_job)
        self._said_en = False         # a partial came back plainly English
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
        if self._ka_warm and not self._said_en:
            lv = self._live          # one Live job at a time per utterance
            if lv is None or lv[1].done():
                self._start_live(n)

    def _start_live(self, n: int):
        try:
            self._live = (n, _KA_POOL.submit(_live_job, np.concatenate(self._chunks[:n])))
        except Exception:  # noqa: BLE001 — result() asks the Georgian ear itself
            self._live = None
        return self._live

    def _run(self, n: int):
        with self._lock:
            latest = self._latest
        if latest is not None and latest[0] != n:
            return None              # superseded before it started: skip
        audio = np.concatenate(self._chunks[:n])
        try:
            # Local only: a partial that may be Georgian is flagged, not sent
            # to the Georgian ear. Each partial used to take that detour, one
            # after another on this single worker, and the final transcript
            # came after "no answer in 8s" + a batch redo: 9.3 s of ear
            # (2026-10-26 04:20). result() asks the Georgian ear ONCE.
            text, heard = transcribe_lang(audio, 16000, self.lang, cloud=False,
                                          fast=self.fast)
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
            self._maybe_ka[n] = heard if str(heard).startswith("ka?") else None
            if heard == "en":
                self._said_en = True
        return text

    async def result(self):
        """The transcript, or None to mean 'use the batch ear'."""
        j = self._latest
        if j is None:
            return None
        live = None
        if self._ka_warm:
            lv = self._live
            if lv is None or lv[0] != j[0]:
                lv = None if self._said_en and not self._maybe_ka.get(j[0]) \
                    else self._start_live(j[0])
            live = lv[1] if lv else None
        whisper = asyncio.wrap_future(j[1])
        if live is not None:
            # Race: Georgian letters from Live settle it without waiting for
            # Whisper (which can sit behind an earlier partial on its one worker).
            done, _ = await asyncio.wait({whisper, asyncio.wrap_future(live)},
                                         timeout=STREAM_RESULT_S,
                                         return_when=asyncio.FIRST_COMPLETED)
            if live.done() and not live.cancelled() and live.exception() is None:
                ka_text, secs = live.result()
                if ka_text and re.search(r"[Ⴀ-ჿ]", ka_text):
                    note_georgian()
                    print(f"[stt] Georgian via Live (raced) in {secs:.1f}s: "
                          f"{ka_text[:60]!a}", flush=True)
                    return self._heard(clean_transcript(ka_text, True), "ka")
        try:
            text = await asyncio.wait_for(whisper, STREAM_RESULT_S)
        except (asyncio.TimeoutError, concurrent.futures.CancelledError):
            self.error = self.error or f"no answer in {STREAM_RESULT_S:.0f}s"
            text = None
        if text is None:
            if live is None:
                return None
            # Whisper failed but Live is still listening: its Georgian, if any
            ka = await asyncio.to_thread(_georgian, np.concatenate(self._chunks[:j[0]]),
                                         16000, True, live)
            return self._heard(ka, "ka") if ka else None
        mode = self._maybe_ka.get(j[0])
        if mode:
            # the final words may be his Georgian: the Georgian ear, once
            audio = np.concatenate(self._chunks[:j[0]])
            ka = await asyncio.to_thread(_georgian, audio, 16000,
                                         mode == "ka?script", live)
            if ka is not None:
                text = ka
                self.heard = "ka"
        return self._heard(text, self.heard)

    def _heard(self, text: str, heard: str) -> str:
        self.heard = heard
        ms = (time.monotonic() - self.t_commit_sent) * 1000
        print(f"[stt-local] heard {ms:.0f}ms after his pause"
              f"{' (sticky en)' if self.fast else ''}: {text[:60]!a}")
        try:
            import metrics
            metrics.emit("ear", ms=round(ms), mode="pinned" if self.fast else "auto")
        except Exception:  # noqa: BLE001
            pass
        return text

    def why(self) -> str:
        return f"{self.lang}: {self.error or 'nothing heard'}"
