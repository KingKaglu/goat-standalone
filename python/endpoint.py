"""Does the sentence sound finished? (2026-10-17, his order: "500ms when it
sounds finished, about 1.2 seconds when it doesn't, and a 1.5s merge window".)

A flat silence timer cannot tell a thought from a pause: "open Google" is done
after 300ms, "so what I want is..." is not done after a full second. The text
the streaming ear has heard so far can. This module only judges that text; the
timing lives in audio_io, the merging in goat_app.

sounds_finished(text) -> True   looks complete: end the turn at the short wait
                         False  trails off (conjunction, article, aux verb,
                                filler, comma, dash, bare wake word): give him
                                the long wait
                         None   nothing heard yet: caller keeps the short wait

Deliberately conservative toward True. A wrongly-open sentence only costs
700ms; a wrongly-finished one cuts him off, and the merge window repairs that.
"""
import re

# Words that cannot end a thought. Object pronouns ("do it", "open that"),
# particles ("volume up"), wh-words ("tell me why"), "so"/"do"/"can" ("I think
# so", "what should I do") and nouns are NOT here on purpose.
_OPEN_EN = frozenset("""
a an the my your his her our their those these
and or but because since although though whereas unless until while if
whether than as then also plus not
to of in on at for with from by into onto
is are was were am have has had would could should shall may might must
i i'm i'll i've i'd you're we're they're he's she's let's
um uh uhm er erm hmm like
""".split())

_OPEN_KA = frozenset("""
და ან მაგრამ რომ თუ რადგან რადგანაც ხოლო როცა როდესაც რათა თუმცა
ანუ ეგ რასაც რაც რომელიც როგორც
""".split())

_OPEN_END_PUNCT = re.compile(r"(?:[,;:\-–—]|\.\.\.|…)\s*$")
_TERMINAL = re.compile(r"[.!?]\s*[\"')\]]*\s*$")
_WORD = re.compile(r"[\w']+", re.UNICODE)
_WAKE_ONLY = re.compile(r"^(?:hey\s+|ok(?:ay)?\s+)?(?:goat|გოუთ|გოატ|გოტ)\W*$",
                        re.IGNORECASE | re.UNICODE)


def sounds_finished(text) -> "bool | None":
    t = (text or "").strip()
    if not t:
        return None
    if _OPEN_END_PUNCT.search(t):
        return False
    if _WAKE_ONLY.match(t):
        return False            # "Goat." alone: the sentence is still coming
    if _TERMINAL.search(t):
        return True
    words = _WORD.findall(t.lower())
    if not words:
        return None
    last = words[-1]
    if last in _OPEN_EN or last in _OPEN_KA:
        return False
    return True
