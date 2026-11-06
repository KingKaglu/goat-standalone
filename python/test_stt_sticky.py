"""Sticky English (2026-11-06 audit V1/V2): after a confident English turn the
laptop ear skips the slow language check, except every 4th utterance, after
Georgian, or when switched off. A fake whisper server, no audio, no network.
Run: py -3.13 test_stt_sticky.py"""
import sys
import time

import numpy as np

import stt_whisper as w

fails = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  — {info}" if info and not ok else ""))
    if not ok:
        fails.append(name)


calls = []
reply = {"p_en": 0.97, "lang": "english"}


def fake_post(audio, sr, data):
    calls.append(data.get("language"))
    if data.get("language") == "auto":
        return {"text": " open spotify", "detected_language": reply["lang"],
                "language_probabilities": {"en": reply["p_en"], "de": 0.0}}
    return {"text": " open spotify"}


def reset():
    calls.clear()
    w._en_sure_until = 0.0
    w._sticky_n = 0
    w._ka_at = 0.0
    w.EN_STICKY = True
    reply.update(p_en=0.97, lang="english")


def main() -> int:
    w._post = fake_post
    a = np.zeros(16000, dtype=np.float32)

    reset()
    check("cold start: no stickiness", not w.sticky_en())
    text, lang = w.transcribe_lang(a, 16000, "en", cloud=False)
    check("first turn runs the language check", calls == ["auto"], calls)
    check("...and comes back English", lang == "en" and "spotify" in text.lower())
    check("a confident English turn makes it sticky", w._en_sure_until > time.monotonic())
    picks = [w.sticky_en() for _ in range(8)]
    check("3 of every 4 utterances skip the check",
          picks == [True, True, True, False, True, True, True, False], picks)

    calls.clear()
    text, lang = w.transcribe_lang(a, 16000, "en", cloud=False, fast=True)
    check("fast: one pinned request, no language check", calls == ["en"], calls)
    check("fast: same transcript", lang == "en" and "spotify" in text.lower())

    reset()
    reply["p_en"] = 0.8
    w.transcribe_lang(a, 16000, "en", cloud=False)
    check("an unsure English turn does not make it sticky", not w.sticky_en())

    reset()
    w.transcribe_lang(a, 16000, "en", cloud=False)
    w._ka_at = time.monotonic()
    check("Georgian lately: never sticky", not w.sticky_en())

    reset()
    w.transcribe_lang(a, 16000, "en", cloud=False)
    w.EN_STICKY = False
    check("GOAT_STT_EN_STICKY=off: never sticky", not w.sticky_en())

    reset()
    w.transcribe_lang(a, 16000, "en", cloud=False)
    w._en_sure_until = time.monotonic() - 1
    check("stickiness runs out", not w.sticky_en())

    reset()
    calls.clear()
    w.transcribe_lang(a, 16000, "en", cloud=True)
    check("phone/batch path (no fast flag) always checks", calls[0] == "auto", calls)

    # the Stream decides once per utterance and passes it on
    reset()
    w.transcribe_lang(a, 16000, "en", cloud=False)
    s = w.Stream(loop=None, lang="en")
    check("a new utterance while sticky streams pinned", s.fast is True)
    s2 = w.Stream(loop=None, lang="ka")
    check("a non-English stream is never pinned", s2.fast is False)

    print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
