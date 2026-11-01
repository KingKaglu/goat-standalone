"""Chat or task — decided before the voice says a word.

His order 2026-11-01: "a fast classifier before the voice reply. Output:
{type: chat | task, task_summary, needs_confirmation}. Under 300 ms, and it
must not hurt voice latency." And, asked how far the voice's own hands go:
"goat can do all the things even though the usage is up, but when the usage
is on it can use claude — small tasks it should do by itself — it should be
natural, like escalation: it needs to know when to use what."

So there are three answers, all from rules (microseconds, no model call):

  chat   small talk, opinions, quick facts, explanations   -> the voice
  small  a one-step thing the voice's own hands do well     -> the voice
         (open/find/read one file, a quick web fact, one phone action,
         a note, an email draft, work hours, the terminal Claude)
  big    tools or the machine, more than one step: files and folders in
         bulk, code, git, the terminal, installs, settings, research    -> Claude

Only "big" changes anything: goat_app hands it to Claude at once, without
asking the voice model first, and speaks one canned line — so the hand-off
costs no model round trip and the voice can't fake or half-do the job.
"chat" and "small" go to the voice as before, whose prompt carries the same
escalation rules for the cases these patterns can't see ("when unsure,
Claude"). With Claude out of usage nothing is forced: the voice tries.
"""
from __future__ import annotations

import re
import time

import brain_route

CHAT, TASK = "chat", "task"
SMALL, BIG = "small", "big"

# An order: an imperative at the start of a sentence, or "can you <verb>".
_ORDER = brain_route._TASK_RE

# Work that is never one step for the voice's hands.
_BIG_VERB_RE = re.compile(
    r"\b(?:delete|remove|erase|wipe|move|copy|rename|organi[sz]e|sort\s+out|"
    r"clean\s+(?:up|out)|tidy|declutter|compress|zip|unzip|extract|archive|"
    r"back\s*up|restore|sync|convert|merge|split|install|uninstall|reinstall|"
    r"update|upgrade|downgrade|download|upload|deploy|publish|"
    r"run|execute|build|compile|fix|debug|repair|refactor|implement|test|"
    r"commit|push|pull|clone|scan|analy[sz]e|audit|investigate|diagnose|"
    r"benchmark|optimi[sz]e|speed\s+up|free\s+up|research|compare|"
    r"set\s+up|setup|configure|enable|disable|schedule|automate|"
    r"edit|change|modify|replace|fix\s+up|rewrite|translate\s+(?:the|this|that|my)\s+file)\b",
    re.IGNORECASE)

# The machine's insides: code, shell, git, settings.
_MACHINE_RE = re.compile(
    r"\b(?:terminal|command\s+line|cmd|powershell|bash|shell|script|registry|"
    r"git|github|repo|repository|branch|commit|code|codebase|python|node|npm|pip|"
    r"driver|drivers|bios|firewall|defender|startup\s+apps?|services?|"
    r"environment\s+variables?|path\s+variable|windows\s+update|"
    r"settings?\s+(?:for|of|in)|system\s+settings?|"
    r"process(?:es)?|ram|memory\s+usage|cpu|disk\s+space|storage)\b",
    re.IGNORECASE)

# Files in bulk / digging through the laptop (one named file stays small).
_BULK_FILES_RE = re.compile(
    r"\b(?:(?:largest|biggest|smallest|oldest|newest|duplicate|all|every|old|"
    r"unused|empty|temp|temporary|junk)\s+(?:\w+\s+)?(?:files?|folders?|photos?|"
    r"videos?|downloads?|documents?|apps?|programs?)|"
    r"(?:files?|folders?)\s+(?:bigger|larger|older|over|than|in\s+(?:my\s+)?"
    r"(?:downloads?|documents?|desktop|pictures?|drive|c:|d:))|"
    r"(?:search|go|look|dig)\s+(?:through|across|all\s+over)\s+(?:my\s+)?"
    r"(?:laptop|computer|pc|drives?|files|folders?)|"
    r"(?:whole|entire)\s+(?:laptop|computer|pc|drive|disk))\b",
    re.IGNORECASE)

# Several steps in one breath.
_MULTI_STEP_RE = re.compile(
    r"\b(?:and\s+then|then\s+(?:open|send|save|move|delete|run|tell|email|put)|"
    r"after\s+that|step\s+by\s+step|one\s+by\s+one|for\s+each|every\s+one\s+of)\b",
    re.IGNORECASE)

# What needs his yes before it runs (the brain's shell gate enforces it;
# this flag rides along in the job so Claude asks before trying).
_CONFIRM_RE = re.compile(
    r"\b(?:delete|remove|erase|wipe|format|uninstall|force[-\s]?push|"
    r"empty\s+the\s+(?:recycle\s+bin|trash)|"
    r"send|email|e-mail|text\s+(?:him|her|them|\w+)|message\s+(?:him|her|them)|"
    r"pay|payment|buy|purchase|order\s+(?:a|the|some))\b",
    re.IGNORECASE)

# A question about the world is chat even when it names a machine word
# ("what is python?", "how does git work?").
_QUESTION_RE = re.compile(
    r"^\W*(?:(?:hey|ok(?:ay)?|so|and|goat)\W+)*"
    r"(?:what|what's|whats|who|who's|when|where|why|how|which|is|are|was|were|"
    r"do|does|did|can|could|should|would|will|explain|tell\s+me\s+(?:about|what|why|how))\b",
    re.IGNORECASE)
# ...unless it asks about HIS machine or files.
_HIS_THINGS_RE = re.compile(
    r"\b(?:my|our|this|the)\s+(?:laptop|computer|pc|files?|folders?|downloads|"
    r"documents|desktop|drive|disk|repo|code|project|app|goat)\b", re.IGNORECASE)

# Talk about the running job: the voice handles it (status, cancel, change).
_JOB_TALK_RE = re.compile(
    r"\b(?:what(?:'s| is)\s+(?:it|claude)\s+doing|how(?:'s| is)\s+it\s+going|"
    r"cancel|stop\s+(?:that|it|the\s+job)|never\s*mind|change\s+it\s+to|"
    r"instead|actually\s+make\s+it|status|progress)\b",
    re.IGNORECASE)

# Something of his is misbehaving: a diagnosis job ("why is my laptop slow").
_TROUBLE_RE = re.compile(
    r"\b(?:slow|lag\w*|freez\w*|crash\w*|hang\w*|stuck|overheat\w*|hot|"
    r"not\s+working|broken|won'?t|doesn'?t|isn'?t|can'?t|keeps?\s+\w+ing|"
    r"error\w*|full|out\s+of\s+space)\b", re.IGNORECASE)

_SMALL_HAND_RE = re.compile(
    r"\b(?:open|show|find|read|play|pause|mute|volume|brightness|lock|"
    r"screenshot|note|write\s+(?:a|me\s+a)\s+note|remind|alarm|timer|"
    r"call|look\s+up|google|search\s+(?:for|the\s+web|online|google)|"
    r"weather|work\s+hours|terminal\s+claude)\b",
    re.IGNORECASE)

_GAME_RE = re.compile(
    r"\b(?:games?|play\s+(?:chess|snake|tetris|minesweeper|solitaire|"
    r"sudoku|2048|wordle|checkers|pac-?man|flappy|dino|slither|agar)|"
    r"play\s+(?:against|vs\.?)|beat\s+(?:the|this)\s+level)\b", re.IGNORECASE)
_MEDIA_RE = re.compile(
    r"\b(?:music|song|songs|playlist|album|podcast|video|spotify|youtube|"
    r"radio|track)\b", re.IGNORECASE)

_TERMINAL_CLAUDE_RE = re.compile(
    r"\b(?:terminal\s+claude|claude\s+(?:in|on)\s+(?:my|the)\s+terminal|claude\s+code)\b",
    re.IGNORECASE)

_FILLER_RE = re.compile(
    r"^\W*(?:(?:okay|ok|hey|now|please|so|and|alright|goat|yes|yeah|then|just|also|"
    r"uh|um|hmm|well|actually|can\s+you|could\s+you|would\s+you|will\s+you)\W+)*",
    re.IGNORECASE)


def _summary(text: str) -> str:
    """His words as a self-contained instruction for Claude."""
    t = " ".join((text or "").split())
    t = _FILLER_RE.sub("", t).strip(" .!?")
    return (t[:1].upper() + t[1:]) if t else ""


def route(text: str, *, job_running: bool = False) -> dict:
    """{type, size, task_summary, needs_confirmation, why, ms} for one turn."""
    t0 = time.perf_counter()
    t = " ".join((text or "").split())
    out = {"type": CHAT, "size": SMALL, "task_summary": "",
           "needs_confirmation": False, "why": "talk"}

    def done(**kw):
        out.update(kw)
        out["ms"] = round((time.perf_counter() - t0) * 1000, 3)
        return out

    if not t:
        return done(why="empty")
    if job_running and _JOB_TALK_RE.search(t):
        return done(why="about the running job")
    if _TERMINAL_CLAUDE_RE.search(t):
        # His own Claude Code sessions: the voice's terminal_claude hand
        # reads and types into them in one step ("terminal" isn't a job).
        return done(type=TASK, size=SMALL, task_summary=_summary(t),
                    why="the terminal Claude")
    big_lead = bool(_BIG_VERB_RE.match(_FILLER_RE.sub("", t)))
    order = bool(_ORDER.search(t)) or big_lead
    question = bool(_QUESTION_RE.match(t)) and not re.match(
        r"(?i)^\W*(?:can|could|would|will)\s+you\s+(?!tell|explain|say)", t)
    his = bool(_HIS_THINGS_RE.search(t))
    if question and not his and not order:
        return done(why="a question")
    big_verb = bool(_BIG_VERB_RE.search(t))
    machine = bool(_MACHINE_RE.search(t))
    bulk = bool(_BULK_FILES_RE.search(t))
    multi = bool(_MULTI_STEP_RE.search(t))
    code = bool(brain_route._CODE_RE.search(t) or brain_route._KA_CODE_RE.search(t))
    if not (order or question):
        return done(why="talk")
    trouble = his and bool(_TROUBLE_RE.search(t))
    # Playing a game is Claude's (game tool + autopilot), never a media "play".
    if order and _GAME_RE.search(t) and not _MEDIA_RE.search(t):
        return done(type=TASK, size=BIG, task_summary=_summary(t),
                    needs_confirmation=False, why="a game")
    if bulk or multi or (order and (big_verb or machine or code)) or (
            question and his and (machine or bulk or code or trouble)):
        why = ("files in bulk" if bulk else "several steps" if multi
               else "a diagnosis" if trouble else "code" if code
               else "the machine" if machine else "work")
        # Only a job Claude runs carries the confirm rule; the voice's own
        # email/phone hands have their own "Send it?" step.
        return done(type=TASK, size=BIG, task_summary=_summary(t),
                    needs_confirmation=bool(_CONFIRM_RE.search(t)), why=why)
    if order:
        return done(type=TASK, size=SMALL, task_summary=_summary(t),
                    why="one step" if _SMALL_HAND_RE.search(t) else "order")
    return done(why="a question about his things")


HANDOFF_LINES = {
    "en": ("Sir, handing that to Claude.", "Sir, Claude's taking that one.",
           "Sir, passing that to Claude now."),
    "ka": ("ბატონო, Claude-ს გადავცემ.",),
}


def handoff_line(lang: str, n: int) -> str:
    pool = HANDOFF_LINES.get(lang) or HANDOFF_LINES["en"]
    return pool[n % len(pool)]


def job_text(r: dict, his: str) -> str:
    """The job as Claude gets it: his words, plus the confirm rule if needed."""
    task = r["task_summary"] or his
    if r["needs_confirmation"]:
        task += (". This is destructive or sends something: before the "
                 "irreversible step, ask Giorgi to confirm (tell_voice, kind "
                 "question) and wait for his yes.")
    return task
