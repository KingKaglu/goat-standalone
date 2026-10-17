"""Side talk: speech in the room that isn't meant for GOAT.

2026-10-01 ~22:28: Giorgi took a call from his dad near the laptop. The ear
transcribed the call — "your mother is…", "I'm not going to get a job",
"we gave you an opportunity to be responsible" — and every fragment went to
the brain, cut the previous reply, and got one back ("Standing by." "Still
standing by."). The brain had even said "that sounds like your call coming
through the mic" — out loud.

How GOAT handles it now:
- Normal mode is unchanged: everything still reaches the brain, so nothing
  he says to GOAT can be lost to a guess. The brain may answer a turn with
  QUIET_TOKEN alone; that reply is never spoken or shown, and it switches
  GOAT into side-talk mode. So does him announcing a call (enters_call).
- In side-talk mode a local gate decides, with no brain call and no cut of
  the running turn: only speech ADDRESSED to GOAT gets through — its name,
  an instant command, a clear order to the computer. Anything else is
  logged and ignored. Speaking to GOAT ends the mode; so does SIDE_QUIET_S
  without any side speech (the call is over).
- looks_aside() only decides whether to play the "Okay." filler: a
  statement with no question, order or GOAT cue gets no filler, so a quiet
  turn stays fully silent. It never drops a turn.
"""
from __future__ import annotations

import re

QUIET_TOKEN = "[[quiet]]"
SIDE_QUIET_S = 30.0   # a call has pauses; 30 s of no side speech = it's over

# He announces it: "I'm on the phone", "I have to take this call",
# "call your dad" (his own words at 22:28), Georgian equivalents.
_ENTER_RE = re.compile(
    r"\b(?:i'?m|i\s+am|i'll\s+be)\s+(?:on|taking)\s+(?:a|the|this)?\s*"
    r"(?:phone|call)\b(?!\s+(?:screen|app|number|charger|case))|"
    r"\btaking\s+a\s+call\b|"
    r"\b(?:have|got|need)\s+to\s+(?:take|answer|make)\s+(?:this|a|the)\s+(?:call|phone)\b|"
    r"\bcall(?:ing)?\s+(?:my|your|his|her)\s+(?:dad|mom|mum|mother|father|"
    r"brother|sister|wife|girlfriend|boss|friend)\b|"
    r"\b(?:hold\s+on|one\s+(?:sec|second|moment)|give\s+me\s+a\s+(?:sec|second|minute))"
    r"\W+goat\b|"
    r"ტელეფონზე\s+ვლაპარაკობ|დამირეკეს|ზარი\s+მაქვს|ვურეკავ|დავურეკავ",
    re.IGNORECASE)

# An order to the computer, at the start of what he says.
_COMMAND_RE = re.compile(
    # At the start of ANY sentence: "Good. Please restart yourself." is an
    # order even though it opens with "Good."
    r"(?:^|[.!?,;]\s+)\W*(?:(?:okay|ok|hey|now|please|so|and|alright|all\s+right|"
    r"good|great|yes|yeah|no|right|then|just)\W+)*"
    r"(?:open|close|click|tap|press|scroll|search|google|look\s+up|find|show|"
    r"play|pause|stop|mute|unmute|volume|turn\s+(?:up|down|on|off)|"
    r"take\s+a\s+screenshot|screenshot|type|write|delete|remove|run|restart|"
    r"go\s+back|refresh|reload|minimi[sz]e|maximi[sz]e|remind\s+me|set\s+a|"
    r"read|check|send|give|push|commit|install|update|download|save|copy|"
    r"paste|undo|zoom|switch|change|make|fix|build|tell\s+the\s+terminal|"
    r"let's|let\s+us|try|add|use|start|go\s+to|listen|watch|"
    r"can\s+you|could\s+you|would\s+you|will\s+you|do\s+you|did\s+you|"
    r"are\s+you|have\s+you|what\s+time|what's\s+the\s+time|how\s+much\s+battery|"
    r"გახსენი|დახურე|დააჭირე|მოძებნე|ჩართე|გამორთე|აჩვენე|ჩამოსქროლე|"
    r"შეგიძლია|გააკეთე|შეამოწმე)\b", re.IGNORECASE)

# Georgian puts the verb anywhere, often last ("არა, არა Google-ი გახსენი",
# "ახლიდან ჩართე იგივე ვიდეო") — an imperative stem anywhere counts.
_KA_ORDER_RE = re.compile(
    r"(?:გახსენ|დახურ|დააჭირ|დააკლიკ|მოძებნ|ჩართ|ჩამირთ|გამორთ|აჩვენ|"
    r"ჩამოსქროლ|ასქროლ|გააკეთ|გამიკეთ|შეამოწმ|გადაამოწმ|გაუშვ|მომეც|დაამატ|"
    r"წაშალ|დამიწერ|მითხარ|გადართ|გაადიდ|ჩააგდ|შეინახ|გადატვირთ|გამაგებინ|"
    r"დამიძახ|დავამატ)\w*")

# Things that only exist on his computer — talk about them is talk to GOAT.
_DEVICE_RE = re.compile(
    r"\b(?:screen|window|tab|tabs|terminal|claude|browser|chrome|brave|file|"
    r"files|folder|desktop|laptop|computer|pc|youtube|spotify|github|"
    r"wifi|bluetooth|gallery|downloads|app|apps|commit|commits|code|"
    r"ეკრან\w*|ფანჯარ\w*|ტერმინალ\w*|ფაილ\w*|კომპიუტერ\w*|ლეპტოპ\w*)\b",
    re.IGNORECASE)

_QUESTION_RE = re.compile(
    r"\?\s*$|^\W*(?:(?:okay|ok|hey|so|and|but)\W+)*(?:what|when|where|why|how|"
    r"who|which|is|are|was|were|can|could|should|would|will|do|does|did|"
    r"have|has|რა|როდის|სად|რატომ|როგორ|ვინ|რომელი)\b", re.IGNORECASE)


def enters_call(text: str) -> bool:
    return bool(_ENTER_RE.search(text or ""))


def addressed(text: str, wake_re=None, reflex_match=None) -> bool:
    """Does this sound like it's FOR GOAT? Used only in side-talk mode,
    where a wrong "no" costs a repeat and a wrong "yes" costs one reply."""
    t = (text or "").strip()
    if not t:
        return False
    if wake_re is not None and wake_re.search(t):
        return True
    if reflex_match is not None:
        try:
            if reflex_match(t) is not None:
                return True
        except Exception:  # noqa: BLE001
            pass
    return bool(_COMMAND_RE.search(t) or _DEVICE_RE.search(t)
                or _KA_ORDER_RE.search(t))


def is_question(text: str) -> bool:
    return bool(_QUESTION_RE.search((text or "").strip()))


def gate(text: str, busy: bool, wake_re=None, reflex_match=None) -> str:
    """Side-talk mode only. "pass": treat as his (and leave the mode);
    "probe": ask the brain, with a hint that it may be side talk (questions
    — calls have them too, but so does he); "ignore": drop it locally. A
    probe never interrupts real work: when GOAT is busy it is dropped."""
    if addressed(text, wake_re, reflex_match):
        return "pass"
    if is_question(text) and not busy:
        return "probe"
    return "ignore"


PROBE_NOTE = ("[side-talk check — not necessarily for you: Giorgi seems to be "
              "on a call or talking to someone else in the room. If the line "
              "below is NOT addressed to you, reply with exactly " + QUIET_TOKEN +
              " and nothing else. If it is for you, answer normally.]\n")


def looks_aside(text: str, wake_re=None) -> bool:
    """A plain statement with no question, order or GOAT cue — the shape of
    someone else's conversation. Decides only the filler noise."""
    t = (text or "").strip()
    if not t:
        return True
    return not (addressed(t, wake_re) or _QUESTION_RE.search(t))
