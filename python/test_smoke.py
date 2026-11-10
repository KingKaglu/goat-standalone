"""Smoke tests for the hearing path, written before the STT refactor
(2026-11-10 audit, Phase 3 step 1). They pin what the code does TODAY so a
change of engine or interface that breaks a contract fails here first.

  1. the ear's contract: text / '' (silence) / None (broken), and the language
  2. the Georgian chain's order: Live -> REST Gemini -> Gladia -> None
  3. which ear _handle_utterance picks per language mode, and what reaches _heard
  4. a phone clip: bytes -> float audio -> _handle_utterance(source="phone")
  5. the end-of-turn decision (finished / still talking / transcript pending)

No audio device, no network, no server: every engine is a fake.
Run: py -3.13 test_smoke.py
"""
from __future__ import annotations

import asyncio
import sys
import time
import types

import numpy as np

import endpoint
import stt_gladia
import stt_live
import stt_whisper as w

fails = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  — {info}" if info and not ok else ""))
    if not ok:
        fails.append(name)


KA = "გამარჯობა როგორ ხარ"
SEC = np.zeros(16000, dtype=np.float32)


# ---- 1. the ear's contract -------------------------------------------------
def ear_contract():
    real_post = w._post
    check("audio under 0.3 s is silence, no request",
          real_post(np.zeros(1000, dtype=np.float32), 16000, {}) == "")

    reply = {}
    w._post = lambda audio, sr, data: reply.get("got")
    w.GPU = True
    w._en_sure_until = 0.0
    w._ka_at = 0.0

    reply["got"] = None
    check("server broken: transcribe gives None", w.transcribe(SEC) is None)
    check("server broken: transcribe_lang gives (None, lang)",
          w.transcribe_lang(SEC, 16000, "en") == (None, "en")
          and w.transcribe_lang(SEC, 16000, None) == (None, ""))

    reply["got"] = {"text": " open the browser", "detected_language": "english",
                    "language_probabilities": {"en": 0.98}}
    text, lang = w.transcribe_lang(SEC, 16000, "en")
    check("English: text and 'en'", lang == "en" and "browser" in text.lower(), (text, lang))
    check("transcribe returns the same text as a plain string",
          isinstance(w.transcribe(SEC), str) and "browser" in w.transcribe(SEC).lower())

    reply["got"] = {"text": " Kamarjova rogor har", "detected_language": "russian",
                    "language_probabilities": {"en": 0.03}}
    text, lang = w.transcribe_lang(SEC, 16000, None)
    check("bilingual: a low English score means Georgian", lang == "ka", (text, lang))

    reply["got"] = {"text": " " + KA, "detected_language": "georgian",
                    "language_probabilities": {"en": 0.4}}
    text, lang = w.transcribe_lang(SEC, 16000, None)
    check("bilingual: Georgian letters settle it", lang == "ka" and KA in text, (text, lang))

    reply["got"] = {"text": " Was machst du da", "detected_language": "german",
                    "language_probabilities": {"en": 0.01, "de": 0.99}}
    check("speech in a foreign language is dropped, not transcribed",
          w.transcribe_lang(SEC, 16000, None) == ("", "de"))
    w._post = real_post


# ---- 2. the Georgian chain --------------------------------------------------
def georgian_chain():
    order = []
    state = {"live": None, "rest": None, "gladia": None, "gladia_on": True}

    def live(audio, sr):
        order.append("live")
        return state["live"]

    def rest(audio, wait):
        order.append("rest")
        return state["rest"]

    def gladia(audio, sr, force):
        order.append("gladia")
        return state["gladia"], "ka"

    keep = (stt_live.transcribe, stt_live.transcribe_rest,
            stt_gladia.available, stt_gladia.transcribe_lang)
    stt_live.transcribe, stt_live.transcribe_rest = live, rest
    stt_gladia.available = lambda: state["gladia_on"]
    stt_gladia.transcribe_lang = gladia
    try:
        state.update(live=KA)
        order.clear()
        check("Live answers: its text, nothing else asked",
              w._georgian(SEC, 16000) == KA and order == ["live"], order)
        check("...and Georgian is remembered (no sticky English)",
              time.monotonic() - w._ka_at < 5 and not w.sticky_en())

        state.update(live=None, rest=KA, gladia="გლადია")
        order.clear()
        got = w._georgian(SEC, 16000)
        check("Live silent: REST Gemini's Georgian wins over Gladia",
              got == KA and order[0] == "live" and "rest" in order, (got, order))

        state.update(live=None, rest="hello there", gladia=KA)
        check("REST without Georgian letters: Gladia's answer",
              w._georgian(SEC, 16000) == KA)

        state.update(live=None, rest=None, gladia=None, gladia_on=False)
        check("every ear down: None, not an invented text",
              w._georgian(SEC, 16000) is None)

        state.update(live="hello there", rest=KA, gladia=KA, gladia_on=True)
        order.clear()
        check("script_only: Live's non-Georgian means None, no fallback asked",
              w._georgian(SEC, 16000, script_only=True) is None and order == ["live"], order)
    finally:
        (stt_live.transcribe, stt_live.transcribe_rest,
         stt_gladia.available, stt_gladia.transcribe_lang) = keep


# ---- 3. which ear _handle_utterance picks ----------------------------------
class FakeStream:
    def __init__(self, text, local=False, heard=""):
        self._text, self.local, self.heard, self.lang = text, local, heard, "en"
        self.pending, self.last_partial = False, ""

    async def result(self):
        return self._text

    def why(self):
        return "test miss"


def make_app(goat_app, language):
    app = object.__new__(goat_app.GoatApp)
    app.vid = None
    app.language = language
    app.turn_lang = "en"
    app._stt_notice = ""
    app._t_speech_end = 0.0
    app.heard, app.langs, app.status = [], [], []
    app.emit = lambda kind, *a: app.status.append((kind, a))
    app._set_turn_lang = lambda lang: app.langs.append(lang)

    async def _heard(text, gap, vcheck, source, capped=False):
        app.heard.append((text, source))
    app._heard = _heard
    return app


async def routing():
    import goat_app
    import stt_realtime

    calls = []
    keep = (w.transcribe_lang, w._georgian, w.transcribe, w.GPU,
            stt_gladia.available, stt_gladia.transcribe_lang, stt_live.prime,
            stt_realtime.unavailable_reason)

    def transcribe_lang(audio, sr=16000, force=None, **kw):
        calls.append(("whisper", force))
        return ("open the browser", "en") if force == "en" else ("kamarjoba", "ka")

    def georgian(audio, sr, **kw):
        calls.append(("georgian",))
        return KA

    w.transcribe_lang, w._georgian = transcribe_lang, georgian
    w.transcribe = lambda audio, sr=16000: (calls.append(("whisper-plain",)) or "plain")
    stt_gladia.available = lambda: True
    stt_gladia.transcribe_lang = lambda a, sr, force: (calls.append(("gladia", force)) or (KA, "ka"))
    stt_live.prime = lambda: None
    stt_realtime.unavailable_reason = lambda lang: ""
    try:
        w.GPU = True
        app = make_app(goat_app, "en")
        await app._handle_utterance(SEC)
        check("English mode + GPU: local Whisper pinned to English",
              calls == [("whisper", "en")] and app.heard == [("open the browser", "laptop")],
              (calls, app.heard))

        calls.clear()
        app = make_app(goat_app, "ka")
        await app._handle_utterance(SEC)
        check("Georgian mode: the Georgian chain, turn language ka",
              calls == [("georgian",)] and app.heard == [(KA, "laptop")] and app.langs == ["ka"],
              (calls, app.heard, app.langs))

        calls.clear()
        app = make_app(goat_app, "auto")
        await app._handle_utterance(SEC)
        check("bilingual: Whisper detects, Georgian goes to the Georgian chain",
              calls == [("whisper", None), ("georgian",)] and app.heard == [(KA, "laptop")],
              (calls, app.heard))

        calls.clear()
        app = make_app(goat_app, "en")
        await app._handle_utterance(SEC, FakeStream("from the stream", local=True))
        check("a streaming transcript is used as is, no second request",
              calls == [] and app.heard == [("from the stream", "laptop")], (calls, app.heard))

        app = make_app(goat_app, "en")
        await app._handle_utterance(SEC, FakeStream("", local=True))
        check("the local stream's '' is a verdict (silence), not a miss",
              calls == [] and app.heard == [("", "laptop")], (calls, app.heard))

        app = make_app(goat_app, "en")
        await app._handle_utterance(SEC, FakeStream(None, local=True))
        check("a stream that missed falls back to the full clip",
              calls == [("whisper", "en")] and app.heard == [("open the browser", "laptop")],
              (calls, app.heard))
        check("...and the miss is reported", any(k == "status" for k, _ in app.status))

        calls.clear()
        app = make_app(goat_app, "en")
        await app._handle_utterance(SEC, source="phone")
        check("the source reaches _heard", app.heard == [("open the browser", "phone")], app.heard)

        calls.clear()
        w.GPU = False
        app = make_app(goat_app, "en")
        await app._handle_utterance(SEC)
        check("English mode without the GPU ear: plain local Whisper",
              calls == [("whisper-plain",)] and app.heard == [("plain", "laptop")],
              (calls, app.heard))

        # ---- 5. the end-of-turn decision, as the audio thread asks it ----
        w.GPU = True
        app = make_app(goat_app, "en")
        rt = FakeStream("x", local=True)
        app._rt = rt
        rt.pending = True
        check("transcript still pending: not finished (long wait)",
              app._sentence_state() is False and app._ep_state == "local-pending")
        rt.pending, rt.last_partial = False, "Open Google."
        check("a finished sentence: short wait", app._sentence_state() is True)
        rt.last_partial = "I want to."
        check("Whisper's full stop on a fragment is ignored", app._sentence_state() is False)
        app._rt = None
        check("nothing heard yet, English: unknown", app._sentence_state() is None)
        app.language = "ka"
        check("nothing heard yet, Georgian: not finished", app._sentence_state() is False)
    finally:
        (w.transcribe_lang, w._georgian, w.transcribe, w.GPU,
         stt_gladia.available, stt_gladia.transcribe_lang, stt_live.prime,
         stt_realtime.unavailable_reason) = keep


# ---- 4. a phone clip ---------------------------------------------------------
async def phone_clip():
    import phone_server
    import tts_edge
    tts_edge.prime = lambda: None          # no socket dial in a test

    got, sent = [], []

    class App:
        reply_to = "laptop"
        _cut_in = None

        def _lat_start(self):
            pass

        def _lat_restore(self):
            pass

        def _route(self, where):
            self.reply_to = where

        async def _handle_utterance(self, audio, rt, gap, source="laptop"):
            got.append((audio, rt, source))

    srv = object.__new__(phone_server.PhoneServer)
    srv.app = App()
    srv._streams, srv._barge, srv._voice_end, srv._you_n = {}, None, 0.0, 0
    srv._send = lambda msg: sent.append(msg)
    srv._phone_speaks = lambda: True
    srv._laptop_talking = lambda: False

    t = np.arange(16000) / 16000.0
    quiet = (0.05 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes()
    srv._utterance(quiet, {})
    await asyncio.sleep(0.05)
    check("a phone clip reaches the ear once", len(got) == 1, len(got))
    if got:
        audio, rt, source = got[0]
        check("...as 16 kHz float audio of the same length",
              audio.dtype == np.float32 and len(audio) == 16000, (audio.dtype, len(audio)))
        check("...lifted to a healthy level (quiet phone mics)",
              0.55 < float(np.max(np.abs(audio))) <= 1.0, float(np.max(np.abs(audio))))
        check("...marked as coming from the phone, replies routed there",
              source == "phone" and srv.app.reply_to == "phone", (source, srv.app.reply_to))
    check("a clip that made no message sends the page back to listening",
          any('"idle"' in m for m in sent), sent)

    got.clear()
    srv._utterance(quiet[:2000], {})       # 1000 samples: under 0.25 s
    await asyncio.sleep(0.05)
    check("a clip under 0.25 s is dropped before the ear", got == [])


def endpoint_words():
    f = endpoint.sounds_finished
    check("endpoint: finished English and Georgian",
          f("open google") is True and f("რა ამინდია დღეს") is True)
    check("endpoint: trailing conjunction keeps the turn open",
          f("open google and") is False and f("მინდა რომ") is False)
    check("endpoint: a bare wake word waits for the sentence", f("Goat.") is False)
    check("endpoint: nothing heard is unknown", f("") is None and f(None) is None)


def main() -> int:
    ear_contract()
    georgian_chain()
    endpoint_words()
    asyncio.run(routing())
    asyncio.run(phone_clip())
    print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
