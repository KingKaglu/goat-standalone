"""One door to the ear (2026-11-10 audit, A1/C4).

    stt.transcribe(audio, 16000, mode) -> Heard(text, lang, status)

Every finished clip that has no streaming transcript goes through here, from
the laptop mic and from the phone alike. Which engine answers is config, not
code:

    GOAT_STT_ENGINE    first engine asked            (default "chain")
    GOAT_STT_FALLBACK  asked when the first is down  (default none)

"chain" is what GOAT did before this module existed, moved here unchanged:
local Whisper for English, and for Georgian the Live ear, then REST Gemini and
Gladia (stt_whisper._georgian). It is always the last resort, so a missing or
broken engine can only cost time, never the turn.

An engine is a function (audio, sample_rate, mode) -> Heard or None:
  mode         "en", "ka" or "auto" (his language setting)
  Heard.text   the words; "" = silence or junk (normal); None = engine broken
  Heard.lang   "en"/"ka" = the language of this turn; "" = keep the current one
  Heard.status a line for the status bar, "" for none
  None         "not mine" (wrong mode, not installed, no key): ask the next one

Blocking: call it from a worker thread, never from the event loop.
"""
from __future__ import annotations

import os
from typing import Callable, NamedTuple

import numpy as np

import stt_gladia
import stt_whisper


class Heard(NamedTuple):
    text: "str | None"
    lang: str = ""
    status: str = ""


ENGINE = os.environ.get("GOAT_STT_ENGINE", "chain").strip().lower() or "chain"
FALLBACK = os.environ.get("GOAT_STT_FALLBACK", "").strip().lower()

ENGINES: "dict[str, Callable]" = {}


def engine(name: str):
    """Register an engine under the name the config uses."""
    def add(fn):
        ENGINES[name] = fn
        return fn
    return add


def order() -> list:
    """The engines asked, in turn. "chain" always closes the list."""
    names = []
    for n in (ENGINE, FALLBACK, "chain"):
        if n and n not in names:
            names.append(n)
    return names


def transcribe(audio: np.ndarray, sample_rate: int = 16000, mode: str = "en") -> Heard:
    last = Heard(None)
    for name in order():
        fn = ENGINES.get(name)
        if fn is None:
            print(f"[stt] unknown engine {name!r} (have: {', '.join(sorted(ENGINES))})",
                  flush=True)
            continue
        try:
            got = fn(audio, sample_rate, mode)
        except Exception as e:  # noqa: BLE001 — one engine down must not cost the turn
            print(f"[stt] engine {name} failed: {e!r}", flush=True)
            got = None
        if got is None:
            continue
        if got.text is not None:
            return got
        last = got
    return last


@engine("fastconformer")
def _fastconformer(audio: np.ndarray, sample_rate: int, mode: str):
    """The local Georgian ear alone (Georgian mode only: it knows no English
    and cannot tell the languages apart). The chain asks it too, first, for
    every clip that is plainly Georgian; naming it here as the engine skips
    Whisper and the cloud for Georgian mode altogether."""
    if mode != "ka":
        return None
    import stt_fastconformer
    if not stt_fastconformer.available():
        return None
    text = stt_fastconformer.transcribe(audio, sample_rate)
    if text is None:
        return None
    if text:
        stt_whisper.note_georgian()
        text = stt_whisper.clean_transcript(text, True)
    return Heard(text, "ka")


@engine("chain")
def _chain(audio: np.ndarray, sample_rate: int, mode: str) -> Heard:
    gpu = bool(getattr(stt_whisper, "GPU", False))
    if gpu and mode == "en":
        # OpenAI's Whisper large-v3-turbo on his GPU (2026-10-24): the
        # English ear — local, no credits. Georgian is beyond it (see
        # stt_whisper), so Georgian and bilingual modes keep the cloud.
        text, heard = stt_whisper.transcribe_lang(audio, sample_rate, "en")
        return Heard(text, "ka" if heard == "ka" else "en")
    if mode == "auto" and gpu and stt_gladia.available():
        # Bilingual with the GPU ear: Whisper tells English from
        # Georgian in ~1 s locally; English is its own text, Georgian
        # goes to the cloud ear pinned to Georgian (mixed-language
        # Gladia wrote his Georgian as "Good morning", 2026-10-24).
        text, heard = stt_whisper.transcribe_lang(audio, sample_rate, None)
        if heard != "en" and text is not None:
            # Live ear first, Gladia its fallback (see the ka branch)
            ka_text = stt_whisper._georgian(audio, sample_rate)
            if ka_text is not None:
                text, heard = ka_text, "ka"
        return Heard(text, heard if heard in ("en", "ka") else "")
    if mode == "ka":
        text = stt_whisper._georgian(audio, sample_rate)
        if text is not None:
            # Georgian mode: Gemini Live's transcript first, Gladia inside
            # _georgian as its fallback. Straight to Gladia his Georgian came
            # out garbled in 5.5-7.2 s ("გარგიმა მლობამ" for "კარგი მადლობა",
            # 2026-10-25 23:01); Live wrote it right in 1.5-2.8 s. The next
            # turn's session is dialled now (saves ~1.4 s).
            try:
                import stt_live
                stt_live.prime()
            except Exception:  # noqa: BLE001 — the turn dials its own
                pass
            return Heard(text, "ka")
    if mode != "en" and stt_gladia.available():
        # Georgian or bilingual mode: cloud ear (local whisper can't do
        # ka at all — it romanizes it into English-looking nonsense,
        # which is exactly what "it hears English when I speak Georgian"
        # was). In "auto" nothing is pinned, so the same ear takes both
        # languages and tells us which one it heard.
        text, heard = stt_gladia.transcribe_lang(
            audio, sample_rate, "ka" if mode == "ka" else None)
        if text is None:
            # Cloud route broke — English local hearing still works.
            return Heard(stt_whisper.transcribe(audio), "en",
                         "georgian hearing offline — english ear on")
        return Heard(text, "ka" if mode == "ka" else (heard or ""))
    return Heard(stt_whisper.transcribe(audio), "en")
