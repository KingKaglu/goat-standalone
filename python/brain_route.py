"""Which brain takes a turn — decided per turn, silently.

His order 2026-10-01: "the speaking GOAT and simple tasks done by Sonnet 5.5,
the hard code or serious questions by the big one — it needs to happen
naturally, without me noticing; the UI shows it."

Three tiers, one session (the CLI switches models on the live session with
set_model, no reconnect, the conversation stays whole):
  talk  — conversation, quick questions          -> Sonnet 5.5, low effort
  task  — simple orders to the computer           -> Sonnet 5.5, low
  deep  — code and planning only                  -> his working brain
          (Opus 5.5 unless he picked another), at his thinking dial

2026-10-16 "when I give a task, Opus does it" is reversed by his order of
2026-10-17: "only escalate to Opus when this clearly involves code or
planning, default to Sonnet at low effort when unsure, and never switch
mid-turn". Tasks run on Sonnet again; only DEEP (code, planning, an
explicit "hard", or the follow-up of a coding job) reaches Opus.

Measured 2026-10-01 on a fork of his live session (42k context, GOAT's
persona and tools), first spoken text:
  opus 5.5 low:   3.55 / 0.80 / 1.78 / 0.86 s
  sonnet 5.5 low: 3.86 / 1.06 / 1.58 / 1.04 s   (deep turn on opus between)
Same speed to the ear; the first switch writes Sonnet's prompt cache once
(~45k tokens), later switches read it warm. So the split costs him no
latency and moves the bulk of his turns off the expensive model.

A wrong "deep" costs a slower, pricier answer; a wrong "talk" costs a
weaker one. When in doubt the text stays on Sonnet at low: only code words,
planning words, an explicit "hard", and orders that continue a deep job go
deep. The tier is decided once, before the message is sent, and the model and
effort never change while a turn is running.
"""
from __future__ import annotations

import re

import side_talk

TALK, TASK, DEEP = "talk", "task", "deep"

FOLLOW_S = 180.0       # an order this soon after a deep turn continues it

# Code and the machine's insides. "fix"/"build" only with an object, so
# "fix me a drink" stays light but "fix the build" doesn't.
_CODE_RE = re.compile(
    r"\b(?:code|coding|codebase|source\s+code|bug|bugs|buggy|debug\w*|"
    r"refactor\w*|implement\w*|compil\w*|unit\s+tests?|tests?\s+(?:fail\w*|pass\w*|suite)|"
    r"(?:run|fix|write|add)\s+(?:the\s+|a\s+|some\s+)?tests?|"
    r"repo|repos|repository|git|github|commit\w*|push(?:ed)?\s+(?:it|the|to|that)|"
    r"pull\s+request|merge\w*|branch\w*|deploy\w*|rebase|"
    r"function|script|scripts|regex\w*|api|apis|endpoint\w*|database|sql|"
    r"backend|frontend|css|html|json|yaml|python|javascript|typescript|"
    r"rust|java|node|npm|pip|powershell|bash|stack\s*trace|traceback|"
    r"exception|errors?|crash\w*|segfault|readme|fasmetri|"
    r"(?:your|goat'?s|the\s+app'?s)\s+(?:own\s+)?(?:code|source|files)|"
    r"(?:un)?install\w*|config\w*|"
    r"(?:fix|build|rebuild|update|upgrade|patch|optimi[sz]e|migrate)\s+"
    r"(?:the|this|that|it|my|your|a|an|those|these|goat)\b|"
    r"\w+\.(?:py|js|ts|tsx|json|md|ps1|bat|toml|yml|yaml|css|html))\b",
    re.IGNORECASE)

# Planning — the only non-code thing that earns the big brain (his order
# 2026-10-17: "only escalate to Opus when this clearly involves code or
# planning"). Advice, health, money and the rest stay on Sonnet.
_SERIOUS_RE = re.compile(
    r"\b(?:plan(?:s|ned|ning)?|(?:make|draw\s+up|write)\s+(?:me\s+)?(?:a|the)\s+plan|"
    r"strateg\w*|roadmap|architecture|design\s+(?:a|an|the|my)|"
    r"step\s+by\s+step|think\s+(?:it\s+)?through|"
    r"გეგმ\w*|დაფიქრდი)\b",
    re.IGNORECASE)

# Georgian code words (no \b — Georgian suffixes glue onto the stem).
_KA_CODE_RE = re.compile(
    r"კოდ[ის]|კოდი|ბაგ|ერორ|შეცდომ|გაასწორ|ფუნქცი|სკრიპტ|რეპო|დეპლო|"
    r"კომიტ|დაპუშ|ტესტ")

# A simple order to the computer: an imperative at the start of a sentence,
# or "can you / could you <verb>".
_TASK_RE = re.compile(
    r"(?:^|[.!?;]\s+)\W*(?:(?:okay|ok|hey|now|please|so|and|alright|goat|"
    r"yes|yeah|no|then|just|also|uh|um|uhm|hmm|mm|well|actually)\W+)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?"
    r"(?:open|close|click|tap|press|scroll|search|google|look\s+up|find|show|"
    r"play|pause|stop|mute|unmute|turn|take|screenshot|type|write|send|"
    r"delete|remove|rename|move|copy|paste|save|download|upload|check|read|"
    r"set|remind|start|launch|run|restart|kill|go|navigate|switch|change|"
    r"make|create|add|put|minimi[sz]e|maximi[sz]e|zoom|refresh|reload|"
    r"bring\s+up|pull\s+up|text|message|email|translate|summari[sz]e|"
    r"remember|forget|note|list|sort|clean|empty|lock|sleep|shut\s+down|"
    r"skip|next|previous|volume|increase|decrease|lower|raise)\b",
    re.IGNORECASE)

# Georgian "open it for me" / "can you" forms side_talk's stems miss.
_KA_TASK_RE = re.compile(r"გამიხსნ|გახსნ|შეგიძლია|ჩამირთ|დამიკარ|მაჩვენ")

# "go ahead", "try again", "do it" — continues whatever was going on.
_CONTINUE_RE = re.compile(
    r"^\W*(?:(?:okay|ok|yes|yeah|yep|sure|alright|good|great|now|so|and)\W+)*"
    r"(?:go\s+ahead|do\s+it|do\s+that|continue|keep\s+going|carry\s+on|"
    r"try\s+(?:it\s+)?again|again|proceed|finish\s+it|ship\s+it|same\s+(?:thing|again)|"
    r"გააგრძელე|ისევ|კიდევ\s+სცადე|გააკეთე)\b",
    re.IGNORECASE)


def classify(text: str, hard: bool = False, last_tier: str | None = None,
             since_last: float | None = None) -> str:
    """talk | task | deep for one turn. Pure: no clock, no state."""
    t = (text or "").strip()
    if hard:
        return DEEP
    if (_CODE_RE.search(t) or _KA_CODE_RE.search(t) or _SERIOUS_RE.search(t)):
        return DEEP
    task = bool(_TASK_RE.search(t) or side_talk._KA_ORDER_RE.search(t)
                or _KA_TASK_RE.search(t))
    following = (last_tier == DEEP and since_last is not None
                 and since_last < FOLLOW_S)
    if following and (task or _CONTINUE_RE.search(t)):
        # "now push it", "try again", "make it blue" right after a coding
        # job belong to that job and its brain.
        return DEEP
    if task:
        return TASK
    return TALK
