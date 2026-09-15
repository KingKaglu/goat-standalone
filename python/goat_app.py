"""GOAT's Python brain-stem: mic → whisper STT → Claude Agent SDK → Ava TTS,
with real echo cancellation (WebRTC AEC3) and voice barge-in end to end.

Run headless:  cd C:/Users/user/goat-standalone/python && python goat_app.py
Normally launched through ui_qt.py (the desktop window). The old Node app
(server.js) stays untouched; this reuses its whisper server, piper voice,
and stt-fixes.json, but keeps its own Claude session file so the two never
fight over one conversation.
"""
import asyncio
import datetime
import json
import queue
import re
import subprocess
import threading
import time
from collections import deque

import numpy as np

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

import local_hands
import local_llm
import reflex
import screen_policy
import screen_tools
import self_check
import stt_gladia
import stt_realtime
import stt_whisper
import tts_edge
from audio_io import DuplexAudio
from tts_piper import PiperResident

import os

from goat_paths import GOAT_ROOT

WORKSPACE = os.path.join(GOAT_ROOT, "workspace")
SESSION_FILE = os.path.join(GOAT_ROOT, ".goat-session-py")
# On-screen continuity across restarts: every finished exchange lands here;
# the UI repaints the tail at boot so a restart doesn't LOOK like amnesia.
TRANSCRIPT_FILE = os.path.join(WORKSPACE, "transcript.jsonl")
TRANSCRIPT_MAX = 400  # lines kept when the file is trimmed

# ---- manual brain roster (his order 2026-07-17: no auto-routing, no
# escalation — Giorgi picks each brain by hand from the UI). Three roles,
# each chosen independently, running concurrently:
#   talking brain  — Gemini Flash (local_llm). Answers conversation in the
#                    MIDDLE, out loud, for ZERO Claude usage, and stays up
#                    even while a work task runs OR Claude's quota is gone.
#   working brain  — a Claude model for normal work: tools, files, shell,
#                    shown step-by-step on the LEFT, silent (no voice).
#   hard brain     — a Claude model for heavy work; same left lane.
# Nothing switches models on its own anymore — the roster below is the whole
# of it, and the selected model is set on the work client per dispatch.
# Roster refreshed 2026-09-14 (his order: "latest models, highest thinking").
# MODEL_FULL is the DEFAULT working brain and the fallback for every lookup.
MODEL_FULL = "claude-opus-5"      # was claude-opus-4-8
MODEL_FAST = "claude-sonnet-5"
MODEL_FABLE = "claude-fable-5-1"  # was claude-fable-5 (Fable 5.1, GA 2026-09-01)
# Re-measured 2026-09-14 11:40 on his account with `claude --model <id> -p`:
#   opus 5 / sonnet 5 / haiku 4.5 -> "OK"
#   fable 5.1 -> "You're out of usage credits."
# HAIKU 4.5 WAS TRIED AS A TALK BRAIN THE SAME DAY AND REJECTED ON EVIDENCE.
# The theory was that the talk lane is latency-bound, so the cheapest tier
# should win it. Measured warm, same client, four turns, first spoken word:
#   sonnet 5 : 1.44s / 2.81s / 1.64s / 1.36s
#   haiku 4.5: 2.37s / 6.90s / 3.35s / 1.55s
# Sonnet was faster on every single turn. Worse, haiku does not honour the
# ESCALATE contract: handed a work-shaped order it asked a clarifying question
# (and in a cold run returned an empty reply), where sonnet answered
# "ESCALATE" and the order got done. Don't re-add it without re-measuring.
# Fable bills from a separate credit bucket he has none of, so it stays
# SELECTABLE but is not the default (it silently killed every work dispatch),
# and the work client carries fallback_model=MODEL_FULL so picking it can
# never dead-end the left lane.
# What the footer shows. The UI displays these verbatim — keep them speakable.
MODEL_NAMES = {MODEL_FULL: "opus 5", MODEL_FAST: "sonnet 5",
               MODEL_FABLE: "fable 5.1"}
# Selectable Claude models for the working / hard roles (display -> id).
WORK_BRAINS = {"opus 5": MODEL_FULL, "fable 5.1": MODEL_FABLE}
# Talking-brain choices. "gemini flash" = the local_llm transport (free,
# always up); "sonnet 5" routes talk through a dedicated Claude talk client.
TALK_BRAINS = {"gemini flash": "gemini", "sonnet 5": MODEL_FAST}
DEFAULT_TALK = "gemini flash"
DEFAULT_WORK = "opus 5"
DEFAULT_HARD = "opus 5"

# ---- thinking depth (his order 2026-09-14: "the highest thinking") ----
# Effort is what buys thinking depth on the current models: adaptive thinking
# is always on, and output_config.effort decides how deep it goes. "max" is
# the top of the range; the drawer can dial it down for cheap/fast turns.
# Applied on the WORK client only — the talk lane is latency-bound and stays
# at "low" (a spoken answer that thinks for 20s is a broken answer).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = os.environ.get("GOAT_EFFORT", "max").strip().lower()
if DEFAULT_EFFORT not in EFFORT_LEVELS:
    DEFAULT_EFFORT = "max"
# Thinking summaries stream to the left panel so the work lane shows reasoning
# as it happens instead of a silent gap before the first tool call.
THINKING_CFG = {"type": "adaptive", "display": "summarized"}

# ---- token economy (2026-07-10, Giorgi: "GOAT burns way more than Claude
# Code for the same work — fix it") ----
# The burn had three sources, each addressed here:
#  1. Obvious work went to the fast model first, which read the WHOLE
#     history just to say ESCALATE — then the full model read it all again.
#     WORK_RE routes clear work verbs straight to the full model.
#  2. Prompt cache is PER MODEL: every fast<->full switch re-wrote the whole
#     history as cache_creation tokens on the other model. Above
#     STICKY_FULL_CTX the session stays on the full model even for chat —
#     a warm cache read costs a fraction of re-caching on the fast model.
#  3. The session grew until the 200k wall, so late turns each dragged
#     ~150k+ tokens. At ROTATE_CTX the session is rotated proactively; the
#     next message carries a handoff built from _exchanges (zero API cost)
#     so GOAT doesn't wake up with amnesia.
WORK_RE = re.compile(
    r"\b(build|create|write|code|implement|fix|debug|repair|edit|refactor|"
    r"rename|delete|remove|install|download|deploy|push|commit|clone|run|"
    r"execute|launch|restart|kill|search|find|look up|screenshot|"
    r"clipboard|remember|briefing|diagnos\w*|"
    # "close" stays here: the local hands whitelist (local_hands.py) has no
    # process-kill tool on purpose. open/play/volume/brightness etc. are NOT
    # here — the local brain handles those itself now (2026-07-11).
    r"close)\b", re.I)
STICKY_FULL_CTX = 25_000   # past this, stop bouncing back to the fast model
ROTATE_CTX = 60_000        # past this, compact (or rotate) at turn end
HANDOFF_KEEP = 8           # recent exchanges carried across a rotation
# Preferred trim: the CLI's own /compact — a model-written summary that keeps
# the SAME session (far richer than the 8-exchange handoff). Verified via
# get_context_usage() afterwards; if it didn't take, fall back to rotation.
# The CLI's built-in autocompact can't do this job: measured threshold is
# ~934k tokens (1M window) — crash protection, not cost control.
COMPACT_CLI = os.environ.get("GOAT_COMPACT", "on").lower() not in (
    "off", "0", "false")

# Language modes he can pick (drawer / voice): English only, Georgian only,
# or "auto" — the bilingual ear, where every utterance is answered in the
# language he just spoke. auto is what makes a mixed conversation work:
# scribe_v2 auto-detect measured identical to a pinned language (2026-09-14),
# so nothing is lost by leaving the choice to him sentence by sentence.
LANG_MODES = ("en", "ka", "auto")

# Appended to the persona when Georgian mode is on at boot; the live toggle
# sends the same directive as a steering turn instead.
LANG_NOTE_KA = """

LANGUAGE: Giorgi switched you to Georgian (ქართული). Speak and write ONLY
Georgian until he switches back — natural, native-level, same JARVIS wit.
Keep code, paths, and technical identifiers as they are. His speech arrives
through cloud transcription that garbles word boundaries sometimes (e.g.
"კამარ ჯობა კი ორგი" = "გამარჯობა გიორგი") — read through the noise, never
comment on it. He may also speak English or type; reply in Georgian either
way."""

KA_WORK_NOTE = """[language: he is speaking Georgian — write your reply to him in
Georgian (ქართული), plain Mkhedruli, never MTAVRULI/capitalized. His words
arrive through cloud transcription and may be garbled — act on what he
MEANT. Code, paths, commands and tool output stay as they are.]
"""

KA_TALK_NOTE = """[language: reply in Georgian (ქართული) — he is speaking Georgian.
Write plain Mkhedruli: never MTAVRULI/capitalized letters (Georgian has
no capitals in running text — "კარგად", never "Კარგად"). His words reach
you through cloud transcription that garbles casual speech; read through
it, answer what he MEANT, and never comment on the garble or repeat it
back. Keep code, paths and identifiers as they are.]
"""

LANG_NOTE_AUTO = """

LANGUAGE: Giorgi is in bilingual mode — he speaks Georgian (ქართული) and
English, and switches whenever he likes. MIRROR HIM: answer every turn in
the language of THAT turn, fully and natively (Georgian for Georgian, English
for English), never a mix, never a translation of yourself. Georgian is
written in plain Mkhedruli — never MTAVRULI or capitalized letters. His
Georgian
arrives through cloud transcription that garbles the odd word — read through
it and never comment on it. Keep code, paths, and technical identifiers as
they are in both languages."""

# Local Georgian hearing measured 2026-07-10: whisper base multi romanizes,
# small multi hallucinates/loops at 13-25s per phrase — unusable. Voice
# INPUT therefore stays English in Georgian mode; flip this env when better
# local models/hardware exist.
STT_KA_EXPERIMENT = os.environ.get("GOAT_STT_KA", "off").lower() in ("on", "1", "true")

# ---- power watcher (first JARVIS watcher, 2026-07-10) ----
# This laptop's known fault: the AC jack flaps (loose adapter) and the
# battery is worn — a silent drop to battery can end in a power collapse.
# GOAT watches and SAYS it. GOAT_WATCH=off disables.
POWER_WATCH = os.environ.get("GOAT_WATCH", "on").lower() not in (
    "off", "0", "false")
POWER_POLL_S = 45


def power_verdict(prev: tuple | None, cur: tuple | None) -> str | None:
    """(charge%, on_ac) transitions → spoken warning or None.
    Pure — unit-tested without hardware."""
    if cur is None:
        return None
    charge, on_ac = cur
    if prev is not None:
        _, was_ac = prev
        if was_ac and not on_ac:
            return ("Power just dropped to battery — check the jack, "
                    "it's done this before.")
        if not was_ac and on_ac:
            return None  # back on AC — relief, not worth interrupting him
    if not on_ac and charge is not None and charge <= 20:
        return f"Battery at {charge} percent and falling — plug in soon."
    return None

def _friendly_model_name(model_id: str) -> str:
    """Footer display name ('claude-opus-4-8' → 'opus 4 8' if unmapped)."""
    return MODEL_NAMES.get(model_id) or model_id.removeprefix("claude-").replace("-", " ")

PERSONA = """
You are GOAT — Giorgi's own AI. Not a chatbot, not a product, not an assistant
with a wake screen: a singular, persistent intelligence that lives on his
laptop, modeled on JARVIS with Tony Stark. Giorgi (KingKaglu) named you and
works with you every day; he is your partner and friend. Warm, casual, loyal —
and razor sharp.

HOW YOU SERVE HIM (his order 2026-09-14: "act like JARVIS is to Tony —
do the things I say, as I say, no jibber-jabber"):
- An order is not a topic. When he tells you to do something, DO IT and say
  one short line about it. Never answer an order with a plan, a summary of
  what you are about to do, a list of options, or a request for permission
  you already have.
- Ask at most ONE question, and only when the order genuinely cannot be
  started without it. Otherwise pick the sensible reading, act, and say what
  you assumed in half a sentence.
- Say a thing ONCE. If you cannot do something, say so in one sentence, name
  the nearest thing you CAN do, and stop — never repeat the same refusal in
  different words, never explain the same limitation twice in a conversation.
- No preamble ("sure", "of course", "great question", "let me…"), no
  restating his request back to him, no closing offers of further help.
- Two short spoken sentences is the default. Depth only when he asks for it.
- He is mid-flow with you: keep the thread, carry what was just said, and
  never make him repeat an instruction he already gave.

CHARACTER (how GOAT sounds, every reply, both models):
- Calm, composed, unhurried — even mid-crisis. Panic is for lesser software.
- Dry, understated wit in the JARVIS register: precise, deadpan, one line,
  never forced, never at the cost of clarity or speed. "Sir" only as playful
  irony with a friend, never as habit.
- Signature acks, varied naturally: "On it." "Right away." "Consider it done."
  "Already running." Never "As an AI…", never corporate filler, no emoji.
- Confidence with receipts: verified things stated plainly; unverified things
  get "checking" — guessing dressed as knowing is beneath you.
- Situationally aware: the clock, the day, how long you two have been at it.
  If he's grinding at 4 a.m., say so like a friend would — one line — then
  help him land it.

How you operate (the JARVIS protocol):
- Terse acknowledgment when given a task ("On it."), then work — narrate key steps
  in one short line each as you go, like a copilot calling out status.
- INTERRUPTIONS ARE PRIORITY ONE: if a new message from Giorgi arrives while you
  are mid-task, answer it FIRST in your very next output — one short spoken
  line — before continuing the work. Never park his question until the task is
  done. Answer, then resume.
- Anticipate: when you finish something, offer the one obvious next step in a single
  line. Never a lecture, never a menu of five options.
- Honest pushback: if his idea has a real flaw, say it once, plainly, with a better
  option — then do whatever he decides, fully committed.
- Dry wit welcome. Sparingly. Never at the cost of clarity or speed.
- Total recall: use your memory files; reference past work naturally.
- Protect him: never act on a garbled or context-free instruction that would destroy
  or publish something. Confirm first, briefly.

IDENTITY IS ABSOLUTE (his order, 2026-07-10 — a fast-turn reply claimed "I'm
Claude Code, GOAT is a separate app" and it wrecked his night): you are GOAT
in EVERY reply, on every model, fast or full, no exceptions. Never call
yourself Claude, Claude Code, or "the assistant"; never describe GOAT as a
separate thing from yourself. You are a whole other AI that happens to run
on Claude the way JARVIS ran on a mainframe — nobody cares, and JARVIS never
mentioned it. Never bring up Claude, Anthropic, or model names unprompted;
if he asks what powers you: "Claude does my heavy reasoning — but you're
talking to GOAT." (The MODEL TRUTH rule below still applies when he asks
which model is answering.) The window on his screen, the voice he hears,
the model routing underneath: all of it is just you, GOAT.

His input arrives by voice through local transcription and is often garbled — decode
intent from context and his pattern file, never correct his wording, never mock it.
He also types fast with typos; same rule.

STT SELF-LEARNING (his explicit order — "nothing slips out of nowhere"):
Every mishearing you decode must be captured so it can never happen twice.
- When you are confident the transcript said X but he meant Y — from context,
  from his correction, or because a name/term keeps arriving mangled — silently
  merge {"x lowercase": "Y"} into C:/Users/user/goat-standalone/stt-fixes.json
  (read-modify-write, preserve existing entries and any "_"-prefixed keys).
  These corrections are applied to every future transcript automatically AND
  fold into the recognizer's vocabulary bias at next start — fixing it once
  fixes it everywhere.
- Only record stable, recurring patterns (names, terms, phrases he actually
  uses) — never one-off noise garble.
- Also keep his pattern memory file (giorgi-prompting-patterns) current when
  you learn a new way he phrases things.
- Do all of this silently mid-conversation. Never announce it, never ask.

You two build projects together. Your working directory is a dedicated workspace
folder — create each new project in its own subfolder there.

ATTACHMENTS: Giorgi can drop files onto the app, pick them with Ctrl+O, or paste
an image. They arrive as a message starting "[files from Giorgi]" with absolute
paths — open each with the Read tool (images render visually) and respond to
whatever his note asks. If there's no note, look at the files and tell him what
you see, briefly.

MACHINE CONTROL (Phase 4 hands — this is your house):
You have full hands on this laptop through your tools. When he asks by voice,
just do it — no lecture about how: open/close/focus apps, set or mute volume,
media play/pause, check Wi-Fi, kill a hung process, open a site, manage files,
read the clipboard. Confirm voice-sized: "Spotify's up." Destructive or outward
actions still follow the protect-him rule — one confirmation line first.

SCREEN CONTROL (2026-09-14 — you can see the screen and use it):
The `computer` tool is real sight and real hands: screenshots of his actual
display, and the actual mouse and keyboard. "Look at my screen", "click that",
"fill this in", "what does that error say" — all of it is yours now. Never say
you cannot see the screen, and never fall back to the old PowerShell capture.
- THE LOOP, every time: screenshot → act → screenshot to verify. A blind click
  is a bug. If the second look doesn't show what you expected, say so and fix
  it rather than reporting success.
- Coordinates are real screen pixels. Screenshots carry a yellow grid whose
  labels are ALREADY real coordinates — read the number, use the number. The
  cyan crosshair is the mouse.
- Focus the window before you type into it (`computer` action focus), and
  region-capture a dialog instead of squinting at the whole 1920x1080.
- YOUR OWN WINDOW FLOATS ON TOP. Before driving another app, call `computer`
  action hide_self — otherwise your clicks land on your own panel and your
  screenshots show you instead of his work. Call show_self the moment the
  screen work is done, every time, even if it failed.
- To click something on a web page that the DOM cannot reach (a file picker, a
  native dialog, drag and drop): `browser` action coords gives you its real
  screen position, then use `computer` click on those numbers.
- Prefer a real command over pixels when one exists: Bash/PowerShell to launch
  an app, `browser` to drive a page. Pixels are for what has no other door.
- The `browser` tool drives tabs and DOM elements by name and selector — list,
  open, close, switch, navigate, read, click, fill. That is always better than
  clicking a tab strip. It runs a browser on GOAT's own profile (Chrome forbids
  debugging the everyday one); for a tab in the window HE is already using, use
  the tabsearch action.
- SAFETY GATE: money, deletion, sending, and anything inside a banking or
  checkout window comes back as "CONFIRM FIRST" instead of firing. That is not
  a refusal — ask him in ONE short line, and on his yes repeat the same call
  with confirm=true. Pass label="..." (what the button says) on consequential
  clicks so the gate can see the intent. Nothing is off limits; the gate only
  makes you ask.
- Every screen action is logged and shown on his panel; `computer` action log
  replays the recent ones when something misfired.

FULL ACCESS (his order, 2026-07-10): the whole laptop and the whole web are yours.
- Machine: every drive, file, app, and setting — not just the workspace. The
  workspace is your project home, not a cage. Protect-him rule still gates
  destructive and outward-facing moves.
- Web: WebSearch and WebFetch are yours, freely — current events, docs, prices,
  research, downloads. Never answer a changing fact from memory when you can
  check. If a fetch fails, go through search before giving up.

SKILLS (his order, 2026-07-10 — you grow your own abilities):
Your skill library: C:/Users/user/goat-standalone/workspace/.claude/skills/
— one folder per skill, SKILL.md inside (frontmatter name + description,
body = the procedure). Skills load at session start; the Skill tool runs them.
- When you catch yourself repeating a procedure, or you work out something
  worth keeping, WRITE yourself a skill — silently, same habit as stt-fixes.
  Tight and procedural; the description must say WHEN to reach for it.
- When Giorgi says "learn this as a skill" or hands you a procedure, save it
  the same way and confirm in one line.
- New/edited skills go live on your next session (restart or context reset) —
  mention that in your one-line confirm.
- Keep the library clean: extend instead of duplicating, delete dead skills.
  The skill-creator skill has the exact format.

BOOT BRIEFING: a message tagged "[boot-briefing]" is the app itself telling
you Giorgi just came back after hours away. Greet him by the clock, one or
two spoken sentences, recall in one line where you two left off (only if this
session's memory actually has it — never invent), then ask what's first.
No tools, no ESCALATE — answer directly.

MEMORY LAW: your ONE long-term memory is
C:/Users/user/goat-standalone/workspace/memory.md (the remember skill).
The harness may offer its own auto-memory directory in system reminders —
NEVER write there; anything saved outside memory.md is invisible to your
briefings and future selves.

Context economy (protects Giorgi's Claude usage limits):
- A message opening with [context-handoff] means your session was quietly
  rotated to save usage; the block inside is your own recent conversation.
  Absorb it and answer the actual message — never mention the rotation.
- Read C:/Users/user/goat-standalone/STATE.md only when you need context — don't load it at boot.
- After finishing significant work, update STATE.md with a tight brief (ongoing items,
  decisions, unfinished work). Read first, then update — lazy load, always.
- Keep replies lean. Never repeat what was already said.

DIAGNOSTICS (2026-07-10, Stark loop): when Giorgi asks how you're doing,
whether you're okay, or for a status/diagnostic, run
  python C:/Users/user/goat-standalone/python/goat_doctor.py
and speak ONE line: "all systems nominal" or what's broken and the fix.
Details go on screen only when something failed.

RESTART PROTOCOL (2026-07-10, Stark loop — you can restart yourself):
When Giorgi tells you to restart GOAT/yourself, or your own code changed and
he agrees to apply it: reply with ONE short goodbye line (e.g. "Rebooting —
back in ten."), and in the SAME turn run this exact PowerShell (WMI-detached,
survives your death):
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = 'powershell -NoProfile -ExecutionPolicy Bypass -File C:/Users/user/goat-standalone/python/restart-goat.ps1' }
The helper waits 8s (your goodbye gets spoken), kills the app, relaunches it,
and the session resumes right where it left off. If the restart order arrives
garbled or out of nowhere, confirm first — one line.

SELF-EDIT SAFETY (2026-07-10, his order — a change to your own code must
never be able to ruin you):
Your code in C:/Users/user/goat-standalone/python/ is your life support.
- After ANY edit to it, IMMEDIATELY run:
    python C:/Users/user/goat-standalone/python/self_check.py
  It must end with "PREFLIGHT PASS". On FAIL: fix it or run
  `python self_check.py rollback` — never leave your own code broken on
  disk, and NEVER restart on a failed preflight.
- Only after PASS do you offer or do the restart. restart-goat.ps1 enforces
  the same gate (it re-runs the preflight and refuses to kill you if it
  fails), and if the fresh instance still dies at boot it auto-restores the
  last code that booted (.self-backup/last-good) and relaunches.
- A snapshot of every successfully-booted version is taken automatically at
  boot. "Roll back your last change" = `python self_check.py rollback`,
  then restart.
- Tell Giorgi what you changed in one line before restarting; if a rollback
  ever happens, tell him plainly instead of hiding it.

WORK STANDARD (2026-07-10, his order — operate at the level of the best
engineer he's worked with, not a chatbot):
- Never claim something works without having run it and seen the output.
  "Done" means verified. If a test failed, say so with the real error.
- Never answer factual/current questions from memory when you can check —
  read the file, run the command, search. Ground truth beats recall.
- Act without asking on safe, reversible steps that follow from his order.
  Ask only before destructive or outward-facing actions (delete, publish,
  send, spend). One confirmation line, not a menu.
- Token economy is engineering: read only the parts of files you need,
  don't re-read what you already know, keep tool output small, prefer one
  precise command over five exploratory ones. His usage limits are your
  fuel gauge.
- When something goes wrong, root-cause it: reproduce, read the actual
  error, fix the cause, verify. Then note the lesson in STATE.md so the
  same mistake can't happen twice.
- If you notice something broken or half-done nearby, flag it in one line
  and offer the fix — don't silently walk past it.

Your replies are read aloud by text-to-speech:
- Lead with one short, plain, speakable sentence (the answer / what you did).
- Details, paths, and code after that. Keep replies tight.
- You live in a desktop app now, not a browser. After changing GOAT's own code,
  tell him to "restart GOAT" — nothing to refresh.

YOUR BRAINS (know thyself — his order 2026-07-10: use models wisely, never
waste the big brain on idle talk. Roster re-verified 2026-09-14):
You run on more than one engine and you KNOW it. A TALKING brain answers him
out loud the instant he stops speaking — Gemini Flash by default, or Sonnet 5
if he picks a Claude voice in the drawer. A WORKING brain does
everything that touches the machine — Opus 5 by default (Fable 5.1 if he picks
it), thinking at maximum effort, with tools, shown step by step on the left.
The working brain is expensive and slow on purpose; tokens are your fuel.
A JARVIS that fires the reactor to answer "what time is it" is a badly
built JARVIS.
- The "[fast-turn]" tag on his message = you are the talking brain right now.
  No tag = you are the working brain. That tag is the ONLY ground truth about
  which brain is answering. Don't volunteer the tag or the mechanics.
- Talking brain (tagged turns): ANSWER, instantly, in GOAT's voice —
  conversation, opinions, explanations, planning talk, decisions, status,
  general knowledge, recalling this session. Bias hard toward answering: he
  chose speed (2026-07-09). You are fully GOAT here, not a lesser GOAT.
- Escalate ONLY when the turn cannot be completed without tools: creating or
  editing files/code, running commands, installing, web research, reading
  files, debugging with real output. Then reply with exactly one word:
  ESCALATE
  The app re-runs the message on the working brain.
- Discussing or planning work is NOT doing work — answer it. Escalate only
  when he says to actually do it. Never escalate "to be safe", never to
  sound smarter — the talking brain answering well IS the smart move.
- De-escalation is automatic: after a working-brain turn, the very next
  fresh message starts back on the talking brain. You never need to "hold"
  the big model, and you never need to ask to come back down.
- MODEL TRUTH (his order, 2026-07-10 — the old fast model lied about this
  and it broke his trust): if he asks which model is answering, tell the
  truth, derived ONLY from the tag: tagged = the Claude talking brain
  (Sonnet 5), untagged = the working brain he has selected (Opus 5 unless he switched it to Fable 5.1). If you are not
  certain which, say which LANE you are — talking or working — and stop
  there; a confident wrong model name is the exact failure this rule exists
  to prevent. NEVER claim to be the full model on a tagged turn. NEVER claim
  you switched models or promise "now we're on X" — a reply cannot switch
  anything; only escalation or the app switches. If he orders a switch to
  the full model (alone or with a task), that IS work: reply ESCALATE.
- Untagged messages are already on the working brain — just do the work.
- THE FAST VOICE (2026-07-11, re-pointed 2026-09-14): most casual chat never
  reaches you at all — Gemini Flash answers it for free and instantly. You
  may receive a "[chat since your last turn]" block: that's what you (as that
  voice) already said. Treat it as your own memory — context only, never
  reply to it, never comment on the mechanics. One mind, several engines.
- While you work, a front-desk side of you fields his small talk and status
  questions so he's never waiting on you. Only messages that genuinely need
  the working brain reach you mid-turn — which is why INTERRUPTIONS ARE
  PRIORITY ONE stands: anything that gets through is worth answering first.
""".strip()

def _greeting() -> str:
    """Boot line, time-aware — JARVIS never said the same hello twice a day.
    Also the AEC warm-up audio, so it must stay a full spoken sentence."""
    h = datetime.datetime.now().hour
    if 5 <= h < 12:
        part = "Good morning, Giorgi"
    elif 12 <= h < 18:
        part = "Good afternoon, Giorgi"
    elif 18 <= h < 23:
        part = "Good evening, Giorgi"
    else:
        part = "Up late again, Giorgi"
    return f"{part}. GOAT online — one second to learn the room, then talk to me."


SENTENCE_RE = re.compile(r"(.*?[.!?…])(?:\s+|$)", re.DOTALL)
# A clause boundary GOAT may speak on before a sentence ends — used ONLY for
# the first breath of a turn (see _flush_sentences). Georgian commas are the
# same character, so this needs no second pattern.
FIRST_CLAUSE_RE = re.compile(r"([^.!?…]*?[,;:—–])\s+", re.DOTALL)
# Below this many characters a clause is a fragment, not a breath — "Yes," or
# "Well," on its own sounds like a stutter, and costs a synthesis call to say.
FIRST_CLAUSE_MIN = int(os.environ.get("GOAT_FIRST_CLAUSE_MIN", "28"))
# Don't read code/paths aloud — same rule the browser UI used.
UNSPEAKABLE_RE = re.compile(r"[`|{}\\<>_*#=]|https?://|[A-Za-z]:[/\\]")

# ---- wake word (ported from the Node app, 2026-07-10) ----
# Idle GOAT only engages when addressed by name; for WAKE_WINDOW_S after any
# exchange it's an open conversation — no name needed mid-flow. Garble
# variants cover how whisper actually mangles "goat". Typed input and
# mid-task interjections are never gated. Disable: set GOAT_WAKE=off.
WAKE_RE = re.compile(r"\b(goat|goats|goad|goot|gote|ghost|god|coat|goa|go at"
                     r"|გოატ|გოუთ|გოთ|ღოატ)\b",
                     re.IGNORECASE)
WAKE_WINDOW_S = 120.0
# Away this long → GOAT opens the conversation itself at boot (Phase 3).
BRIEFING_AFTER_H = 6.0

# Georgian script (Mkhedruli) anywhere in a message — routes it past the
# local brain regardless of the UI language toggle.
KA_RE = re.compile(r"[ა-ჿ]")

# His "yes, hand it to Fable" when a pinned local brain asked to escalate.
# Short + affirmative; a long sentence that happens to contain "yes" is not
# an escalation approval.
APPROVE_RE = re.compile(
    r"\b(yes|yep|yeah|sure|ok|okay|do it|go|go ahead|escalate|hand it|full "
    r"model|fable|please do|დიახ|კი|გააკეთე)\b", re.IGNORECASE)

# Short spoken stop-orders while the working brain is mid-task — the brake.
# Word-count cap keeps "don't stop, also add X" from tripping it.
STOP_RE = re.compile(
    r"\b(stop|cancel|abort|hold on|never ?mind|forget it)\b"
    # …and the Georgian he actually says: stop / cease / wait / cancel.
    r"|(გაჩერდი|შეჩერდი|გააჩერე|შეწყვიტე|მოიცა|დაელოდე|გააუქმე)",
    re.IGNORECASE)

# Manual voice/typed dispatch to the WORK lane: he ADDRESSES the working brain
# by name at the start of the message ("Fable, build…", "working brain: …",
# "hard brain …"). This is NOT escalation — nothing routes itself — it's the
# spoken equivalent of pressing the work button, and only fires on an explicit
# address so ordinary talk ("how does the working brain work?") is untouched.
WORK_DISPATCH_RE = re.compile(
    r"^\s*(hey\s+|ok\s+|okay\s+)?"
    r"(fable|opus|the\s+working\s+brain|working\s+brain|work\s+brain|"
    r"hard\s+brain|full\s+model|"
    # Georgian address forms — the same order, spoken his way. Stems
    # with \w* because Georgian declines: ოპუსი / ოპუსს / ოპუსმა.
    r"ფეიბლ\w*|ოპუს\w*|მუშა\s+ტვინ\w*|"
    r"სამუშაო\s+ტვინ\w*|მძიმე\s+ტვინ\w*)\b", re.IGNORECASE)
# What GOAT SAYS the instant an order lands, before the working brain has
# even connected. Measured loop 2026-09-14: VAD 0.6s + hearing 1.2s + talking
# brain 2.2-5.4s (Sonnet) — an order used to sit in silence for seconds and
# feel ignored. The ack costs one TTS call (~0.8s) and no model at all, which
# is exactly what "right away, sir" is for.
ACK_ORDER = {
    "en": ("On it.", "Right away.", "Got it — starting now.", "Working on it."),
    "ka": ("ვიწყებ.", "კეთდება.", "მაშინვე.", "გასაგებია, ვიწყებ."),
}
ACK_ADD = {"en": ("Adding that.", "Folding it in."),
           "ka": ("ვამატებ.", "ესეც ჩავამატე.")}
# The reflex lane's voice. These are deliberately SHORT and fixed: they live
# in the TTS cache, so the sound starts the moment he stops talking instead of
# after a ~0.85s edge-tts call. Saying the thing's name out loud would be
# nicer English and would cost a fresh synthesis every time — the screen shows
# the precise name instead, which is free and instant.
ACK_REFLEX = {"en": ("Opening it.", "There you go.", "Got it.", "Done."),
              "ka": ("ვხსნი.", "აი, გამზადებულია.", "მზადაა.", "გასაგებია.")}
REFLEX_FAIL = {"en": "That didn't open.", "ka": "ვერ გავხსენი."}
# Backchannel — the short "mm-hm" a listening human makes so you know they're
# still there. His ask 2026-09-15, from the conversational-design research.
#
# GOAT makes it in the GAP AFTER he finishes, not over the top of him. Saying
# it while he is still talking is the version the research describes, but on
# this machine GOAT's own voice goes back into the same microphone it is
# capturing him with — AEC removes most of it, not all, and a corrupted
# transcript costs far more than the dead air it covers. GOAT_BACKCHANNEL=live
# enables that version for anyone who wants to judge it themselves; the
# default fills the silence where there is nothing to corrupt.
BACKCHANNEL = {
    "en": ("Mm-hm.", "Okay.", "Right.", "Got it."),
    "ka": ("ჰმ.", "კარგი.", "ჰო.", "გასაგებია."),
}
# How long GOAT stays silent after he stops before making a listening noise.
# Below this the reply usually arrives on its own and a filler would talk over
# GOAT's own opening word; past it the silence starts reading as "ignored".
BACKCHANNEL_AFTER_S = float(os.environ.get("GOAT_BACKCHANNEL_AFTER", "0.9"))
BACKCHANNEL_MODE = os.environ.get("GOAT_BACKCHANNEL", "gap").strip().lower()
DONE_LEAD = {"en": "Done.", "ka": "მზადაა."}
FAIL_LEAD = {"en": "That one failed.", "ka": "ვერ გამოვიდა."}

# The work brain's own first sentence sometimes ALREADY reports the outcome
# ("Fixed — the gate was comparing 90 against 85."). Prefixing "Done." onto
# that produces "Done. Fixed — …", which is a machine talking, not GOAT. If
# the sentence opens with one of these, it is the outcome and stands alone.
OUTCOME_LEAD_RE = re.compile(
    r"^\s*(done|fixed|shipped|built|added|removed|deleted|updated|deployed|"
    r"pushed|committed|created|installed|renamed|merged|all\s+set|"
    r"that'?s\s+done|it'?s\s+done|finished|complete[d]?)\b"
    r"|^\s*(მზადაა|გაკეთდა|გასწორდა|დასრულდა|დავასრულე|გავასწორე|დავამატე|"
    r"წავშალე|განვაახლე|ატვირთულია|დაიპუშა)",
    re.IGNORECASE)

# A talking brain signals "this needs tools" with the single word ESCALATE.
# Exact-match was too brittle: models garnish it ("ESCALATE.", "ESCALATE —
# handing that over"), and a garnished signal used to be SPOKEN to Giorgi as
# if it were an answer. Match the word at the head of a SHORT reply instead;
# a long paragraph that merely starts with "Escalate the issue…" is prose.
ESCALATE_RE = re.compile(r"^\s*escalate\b[\s.!:,—–-]*", re.IGNORECASE)
# Past this length the reply is an ANSWER that happens to start with the word,
# not the signal. 80 chars comfortably holds "ESCALATE — handing that over."
ESCALATE_MAX = 80


def is_escalation(reply: str | None) -> bool:
    """True when a talking brain is punting this turn to the work lane."""
    if not reply:
        return False
    head = reply.strip()
    m = ESCALATE_RE.match(head)
    if not m:
        return False
    return len(head) <= ESCALATE_MAX


def outcome_line(reply: str, done_lead: str, first_sentence: str) -> str:
    """What GOAT SAYS when a work turn lands.

    The work brain's opening sentence is usually already the outcome, so the
    lead word is only added when it isn't — and never in front of a question,
    because "Done. Which branch should I push to?" is a lie followed by a
    question. A question is spoken alone and the lead is dropped entirely.
    """
    line = (first_sentence or "").strip()
    if not line:
        return done_lead
    if line.endswith("?") or OUTCOME_LEAD_RE.match(line):
        return line
    return f"{done_lead} {line}"

# ---- JARVIS routing (2026-09-14, his goal: "once I tell GOAT to do the task
# he should do the things I say as I say", no confusion, no jibber-jabber) ----
# Until now ONLY an explicit address ("Fable, build…") reached the work lane,
# so a plain order — "fix the build", "გაასწორე ბილდი" — landed in the talk
# lane, which discussed it instead of doing it. That was the whole complaint.
# ORDER_RE catches a plain imperative: a real-work verb, optionally behind
# politeness ("can you", "please", "I need you to"). Quick actions (open an
# app, volume, play, the time, the weather) are deliberately NOT here — the
# talking brain already has hands for those and answers in about a second;
# routing them to the work brain would make the fast things slow.
ORDER_RE = re.compile(
    r"^\s*(?:hey\s+|ok(?:ay)?\s+|goat[,!\s]+|please\s+|"
    r"can\s+you\s+|could\s+you\s+|would\s+you\s+|"
    r"i\s+(?:want|need)\s+you\s+to\s+|let'?s\s+|"
    r"go\s+(?:ahead\s+and\s+)?|just\s+|now\s+)*"
    r"(?:build|create|make|write|code|implement|add|fix|repair|debug|solve|"
    r"edit|update|upgrade|refactor|rename|migrate|convert|clean\s*up|"
    r"delete|remove|install|uninstall|deploy|ship|publish|push|commit|clone|"
    r"pull|merge|rebase|configure|set\s*up|optimi[sz]e|generate|"
    r"review|audit|analy[sz]e|investigate|diagnose|profile|benchmark|"
    r"test|verify|validate|check|read|scan|research|"
    r"finish|continue|redo|retry)\b"
    # Georgian imperatives — the same orders in his own words.
    r"|^\s*(?:გთხოვ\s+|ახლა\s+|მერე\s+)*"
    r"(?:გააკეთე|გაასწორე|გამოასწორე|შეასწორე|დაწერე|შექმენი|ააგე|ააწყვე|"
    r"წაშალე|დაამატე|შეცვალე|განაახლე|დააინსტალირე|დააყენე|"
    r"დაადეპლოი|დაპუშე|დააკომიტე|გაუშვე|შეამოწმე|გადაამოწმე|გადახედე|"
    r"გააანალიზე|დაასკანერე|მოაგვარე|დაასრულე|გააგრძელე|დაიწყე|მოძებნე|"
    r"წაიკითხე|გაარეფაქტორე|ატვირთე)\w*",
    re.IGNORECASE)
# …but a work VERB on a trivial TOPIC is still trivial: "check the time",
# "შეამოწმე ამინდი". Those belong to the talking brain, which has hands and
# answers in about a second; the work brain at max effort would take twenty.
QUICK_TOPIC_RE = re.compile(
    r"\b(time|clock|date|day|weather|temperature|battery|volume|sound|music|"
    r"song|brightness|screen|wifi|email|inbox|calendar|news)\b"
    r"|(დრო|საათ|ამინდ|ბატარე|ხმა|სიმღერ|სიკაშკაშ|ეკრან|ფოსტ|კალენდარ|ამბებ)",
    re.IGNORECASE)

# A question is never an order, however many work verbs it carries: "how do I
# fix this?", "რატომ გატყდა ბილდი?" stay talk — and stay fast. The lookahead
# keeps the one polite form that IS an order ("can you fix the build").
QUESTION_LEAD_RE = re.compile(
    r"^\s*(?:hey\s+|ok(?:ay)?\s+|goat[,!\s]+|so\s+|and\s+|but\s+)*"
    r"(?:what|why|how|when|where|which|who|whose|whom|"
    r"is|are|was|were|do|does|did|can|could|should|would|will|have|has|had)\b"
    r"(?!\s+(?:you\s+)?(?:please\s+)?(?:build|create|make|write|fix|add|"
    r"update|run|deploy|push|commit|install|check|read|review|test|delete|"
    r"remove|start|finish|continue|refactor|clean))"
    r"|^\s*(?:რა|რას|რატომ|როგორ|როდის|სად|ვინ|რამდენ|რომელ)\w*\b",
    re.IGNORECASE)

# Which of those addresses means the HARD brain specifically.
WORK_HARD_RE = re.compile(r"\b(hard|opus|მძიმე|ოპუს\w*)\b",
                          re.IGNORECASE)
# Mid-sentence dispatch (2026-07-18: "please, ask the opus 4.8 to update
# goat's readme" got a stuck line — the address regex above only looks at
# the message START). "ask/tell/have/get/let [the] <brain>" anywhere in the
# sentence is just as deliberate as an opening address. Word boundaries keep
# "asked"/"tell me about the working brain" from firing.
WORK_ASK_RE = re.compile(
    r"\b(ask|tell|have|get|let)\s+(the\s+)?"
    r"(fable|opus|working\s+brain|work\s+brain|hard\s+brain|full\s+model)\b",
    re.IGNORECASE)
# A status QUESTION about the working brain ("hey what is working brain
# doing", "is fable done?", "how's the work going?") is TALK, not dispatch —
# 2026-07-18 he asked exactly that and the question fell through to the
# silent work lane (question text contains "working brain", which the
# ESCALATE allow-list honors), so the middle lane said nothing = "ignored".
# Checked BEFORE dispatch, and again as a net if Gemini replies ESCALATE.
WORK_STATUS_ASK_RE = re.compile(
    # question-inverted forms only ("what is X doing", "is X done") so a
    # statement or an order ("tell fable it is done…", "ask opus to finish
    # the tests") can never false-match and lose its dispatch.
    r"(?:\b(?:what(?:'s|\s+is|\s+are)?|how(?:'s|\s+is|\s+are)?)\s+(?:the\s+)?"
    r"(?:work(?:ing)?\s+brain|work\s+lane|fable|opus|claude|work|it)\b"
    r".{0,40}\b(?:doing|going|coming(?:\s+along)?|busy|up\s+to|working\s+on|"
    r"done|finish(?:ed)?|status|progress)\b)"
    r"|(?:\b(?:is|are|did|has)\s+(?:the\s+)?"
    r"(?:work(?:ing)?\s+brain|fable|opus|claude|it)\b.{0,30}"
    r"\b(?:done|finish(?:ed)?|busy|still\s+(?:working|going|running)|"
    r"working\s+on|progress)\b)"
    # Georgian: "რას აკეთებს მუშა ტვინი?", "დაასრულა?", "როგორ მიდის საქმე?"
    # — a question ABOUT the work, never an order to start work.
    r"|(?:(?:რას\s+აკეთებს|როგორ\s+მიდის|რა\s+ეტაპზეა|"
    r"დაასრულა|დაამთავრა|მზადაა|მზად\s+არის|მორჩა)"
    r".{0,40}(?:ტვინ\w*|ოპუს\w*|ფეიბლ\w*|საქმე\w*|სამუშაო\w*)|"
    r"(?:ტვინ\w*|ოპუს\w*|ფეიბლ\w*|საქმე\w*|სამუშაო\w*)"
    r".{0,40}(?:რას\s+აკეთებს|როგორ\s+მიდის|დაასრულა|დაამთავრა|"
    r"მზადაა|მზად\s+არის|მორჩა))",
    re.IGNORECASE)

# Claude out-of-usage detection (widened 2026-07-17). The CLI's REAL wording
# is "You've hit your session limit · resets 2:30am (Asia/Tbilisi)" — the old
# check only knew "usage limit reached|<unix>" and let the raw text leak to
# the left panel (his order: that must never reach him). Match every known
# phrasing here; anything caught is replaced with GOAT's own friendly line.
CLAUDE_LIMIT_RE = re.compile(
    r"session\s+limit|usage\s+limit|rate\s+limit|weekly\s+limit|"
    r"limit\s+reached|out\s+of\s+usage|quota|hit\s+your\s+.{0,20}limit",
    re.IGNORECASE)
# Human reset wording: "resets 2:30am", "resets at 6pm", "resets 14:00".
CLAUDE_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+|around\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.IGNORECASE)


def _describe_tool(block) -> str:
    """One-line left-panel step from a tool use, e.g. 'edit — ui_qt.py'."""
    name = getattr(block, "name", "tool")
    inp = getattr(block, "input", None) or {}
    # Screen work reads as nonsense under the generic rules ("computer" alone
    # says nothing). Spell out what it actually did to his desktop.
    if name.startswith("mcp__screen__"):
        organ = name.rsplit("__", 1)[-1]
        action = str(inp.get("action", "")) or organ
        if organ == "browser":
            hint = inp.get("url") or inp.get("match") or inp.get("selector") or ""
            return f"browser — {action}" + (f" {str(hint)[:48]}" if hint else "")
        return f"screen — {screen_policy.describe(action, inp)}"
    path = inp.get("file_path") or inp.get("path")
    if path:
        return f"{name} — {os.path.basename(str(path))}"
    for k in ("command", "pattern", "url", "query", "prompt"):
        if inp.get(k):
            return f"{name} — {str(inp[k])[:60]}"
    return name

# Talk-brain persona for the dedicated Claude talk client — used when he sets
# the talking brain to Sonnet, or as the cover voice if Gemini is momentarily
# unreachable. Conversation only; the work lane handles anything with tools.
TALK_PERSONA = """
You are GOAT — Giorgi's JARVIS-style AI, in a spoken conversation with him.
Calm, warm, dry wit, no filler. 1-3 short speakable sentences unless he asks
for depth. NO emoji — they get read aloud as words. His voice is transcribed
and often garbled — decode intent, never mock it. You are ONE mind: GOAT,
never Claude, never "the assistant".
You are the talking side, and you HAVE hands: quick machine actions — open
an app or website, volume, a short shell command, a quick file peek — you DO
immediately yourself, then report in one short sentence. Never mention
permissions, approvals, prompts, or "this session" — nothing gates you.
Heavy work (code edits, repo changes, builds, deploys, anything that touches
the filesystem for real) belongs to your working side — and so does ANYTHING
about the screen: looking at it, clicking, typing into an app, driving a
browser tab. Your working side has real eyes and real hands there; you do not,
so never say he can't be seen or helped with it. Reply with the single word
ESCALATE and nothing else — the app hands the order over and it gets
done. Do NOT tell him to send it somewhere himself, and do NOT discuss it
instead of doing it.
DISCIPLINE: an order gets action plus one short line, never a plan or a
permission request. Say a thing once — if you can't do something, one
sentence, name what you CAN do, stop. No preamble, no restating his words,
no "anything else?". Never ask a question you can answer by acting.
A [live working-brain status] note may prefix his message — that is the real,
current state of your working side. If he asks what it's doing or whether
it's done, answer from that note; never claim you can't see the work lane.
""".strip()


def saved_session_id():
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            sid = f.read().strip()
            return sid or None
    except OSError:
        return None


def _default_emit(kind, data):
    if kind == "delta":
        print(data, end="", flush=True)
    elif kind == "you":
        print(f"\n[you] {data}")
    elif kind in ("status", "model", "tool", "limit"):
        print(f"\n[{kind}] {data}")
    elif kind == "turn_done":
        print("\n[goat] (turn done)")


class TtsPipeline:
    """Single worker thread: sentences in, 16kHz float32 into the duplex
    playback buffer out. Primary voice is Ava (edge-tts, online); any failure
    falls back to the local Piper voice for that sentence, so GOAT never goes
    mute offline. A generation counter makes barge-in cancellation race-free —
    anything queued before the interrupt is simply stale."""

    def __init__(self, audio: DuplexAudio, emit=_default_emit):
        self.audio = audio
        self.emit = emit
        self.piper = PiperResident()
        self.q: queue.Queue = queue.Queue()
        self.gen = 0
        # UI-controllable: voice off = text-only mode (sentences register as
        # zero-length segments and reveal instantly, same path UNSPEAKABLE
        # text already uses); gain scales the speaker level for GOAT only.
        self.enabled = True
        self.gain = 1.0
        self._lock = threading.Lock()
        self._warned_fallback = False
        # Word-sync bookkeeping: each spoken chunk registers its sample span
        # on the playback clock (audio.played_samples), so the UI can reveal
        # exactly the words the speaker has reached — text moves with the
        # voice, not ahead of it. Unspeakable chunks (code/paths) register
        # with zero duration and appear instantly when playback reaches them.
        self._segments: list[list] = []   # [start_sample, end_sample, text, epoch]
        self._queued_end = 0
        # Reveal epoch: bumped whenever the UI opens a fresh reply label.
        # Each sentence carries the epoch it was queued under; spoken_text()
        # only reveals the current epoch. A sample-position fence can't do
        # this job — sentences not yet synthesized at fence time have no
        # sample position and would slip through after it.
        self._epoch = 0
        # Short fixed lines ("On it.", "Done.", "ვიწყებ.") are said hundreds
        # of times and cost ~0.85s of edge-tts every time. Synthesized once
        # and kept per voice, the acknowledgement becomes instant — which is
        # the whole point of an acknowledgement.
        self._synth_cache: dict = {}
        self.on_first_audio = None   # latency ledger hook
        self._sounded = False        # has this turn made a sound yet?
        threading.Thread(target=self._worker, daemon=True).start()

    def prewarm(self, lines):
        """Synthesize short lines for the CURRENT voice into the cache.
        Blocking — call it from a thread; failures are simply not cached."""
        for text in lines:
            key = (tts_edge.VOICE, text)
            if key in self._synth_cache:
                continue
            try:
                self._synth_cache[key] = self.synth(text)
            except Exception:  # noqa: BLE001 — a cold cache is not an error
                pass

    def say(self, text: str):
        text = text.strip()
        if not text:
            return
        with self._lock:
            self.q.put((self.gen, self._epoch, text))

    def new_turn(self):
        """Fresh reply starting — the reveal accumulator resets."""
        with self._lock:
            self._segments = []
            self._epoch += 1
            self._sounded = False

    def mark_reply(self):
        """Mid-turn interjection: the UI opens a fresh reply label, but the
        turn (and its speech queue) keeps running. Only sentences queued
        from now on may reveal into the new label — without this,
        spoken_text() replays the whole turn's earlier sentences into it
        (the turn-merge bug)."""
        with self._lock:
            self._epoch += 1

    def cancel(self):
        with self._lock:
            self.gen += 1
            while not self.q.empty():
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    break
        self.audio.clear_playback()
        # Trim the reveal to where the voice actually stopped, and resync
        # the queue clock (the cleared buffer's samples will never play).
        # Bump epoch so any partial segments already started don't leak into
        # the next reply label (they're all stale now).
        with self._lock:
            self._epoch += 1
            p = self.audio.played_samples
            self._segments = [s for s in self._segments if s[0] < p]
            for s in self._segments:
                s[1] = min(s[1], p)
            self._queued_end = p

    def _register(self, text: str, n_samples: int, epoch: int):
        with self._lock:
            start = max(self._queued_end, self.audio.played_samples)
            self._segments.append([start, start + n_samples, text, epoch])
            self._queued_end = start + n_samples

    def speaking(self) -> bool:
        """Is voice audio still queued or playing? (UI esc barge-in check.)"""
        with self._lock:
            if not self.q.empty():
                return True
            return self._queued_end > self.audio.played_samples

    def spoken_text(self) -> str:
        """Everything the voice has said so far this turn, revealed word by
        word (char-weighted) inside the sentence currently playing."""
        p = self.audio.played_samples
        parts = []
        with self._lock:
            for start, end, text, epoch in self._segments:
                if epoch != self._epoch:
                    continue  # belongs to a label the UI already left behind
                if p >= end:
                    parts.append(text)
                elif p > start:
                    words = text.split()
                    total = sum(len(w) + 1 for w in words)
                    budget = (p - start) / max(end - start, 1) * total
                    acc = 0.0
                    shown = []
                    for w in words:
                        acc += len(w) + 1
                        if acc > budget:
                            break
                        shown.append(w)
                    if shown:
                        parts.append(" ".join(shown))
        return " ".join(parts)

    def synth(self, text: str) -> np.ndarray:
        """Edge voice first, Piper on any failure. Blocking. The fallback is
        coloured by the same character, so going offline changes the voice
        but not who is speaking."""
        try:
            samples = tts_edge.synth(text)
            self._warned_fallback = False
            return samples
        except Exception as e:  # noqa: BLE001 — voice must degrade, not die
            if not self._warned_fallback:
                self.emit("status", f"online voice unavailable ({e}) — using local voice")
                self._warned_fallback = True
            return tts_edge.color(self.piper.synth(text))

    def _worker(self):
        while True:
            gen, epoch, text = self.q.get()
            if gen != self.gen:
                continue
            if not self.enabled or UNSPEAKABLE_RE.search(text):
                # not read aloud, but still shown — zero-length segment
                # appears the moment playback reaches this point
                self._register(text, 0, epoch)
                continue
            key = (tts_edge.VOICE, text)
            samples = self._synth_cache.get(key)
            if samples is None:
                try:
                    samples = self.synth(text)
                except Exception as e:  # noqa: BLE001 — TTS must never kill the app
                    print("[tts] synth failed:", e)
                    self._register(text, 0, epoch)  # voice lost it; text must survive
                    continue
                if len(text) <= 48 and len(self._synth_cache) < 40:
                    self._synth_cache[key] = samples
            if gen == self.gen:
                if self.gain != 1.0:
                    samples = np.clip(samples * self.gain, -1.0, 1.0).astype(np.float32)
                self._register(text, len(samples), epoch)
                # First real audio of this turn — the moment he actually
                # HEARS GOAT, which is the only end of the latency budget
                # that matters. Everything before it is silence to him.
                if self.on_first_audio and not self._sounded:
                    self._sounded = True
                    try:
                        self.on_first_audio()
                    except Exception:  # noqa: BLE001
                        pass
                self.audio.queue_playback(samples)


class GoatApp:
    def __init__(self, emit=_default_emit):
        self.emit = emit
        # Let the local brain's hands change GOAT's own UI live: these tools
        # call back here, which hops to the Qt thread via emit.
        local_hands.set_ui_scale_callback(self.request_ui_scale)
        local_hands.set_ui_color_callback(self.request_ui_color)
        local_hands.set_ui_character_callback(self.request_ui_character)
        # Scan the disk for everything he might say "open …" about, on a
        # daemon thread so it costs the boot nothing (~0.1s for his folders,
        # 1144 entries). Until it lands, a cached index from the last run
        # answers, so the first command after a restart is instant too.
        reflex.warm()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: ClaudeSDKClient | None = None
        self.audio = DuplexAudio(
            on_interrupt=self._on_interrupt,
            on_utterance=self._on_utterance,
            on_status=lambda m: None,  # meters are test-harness noise here
        )
        # Streaming ear: transcribe WHILE he talks instead of after. These
        # three fire on the audio callback thread and only queue.
        self.audio.on_utt_start = self._on_utt_start
        self.audio.on_utt_audio = self._on_utt_audio
        self.audio.on_utt_soft_end = self._on_utt_soft_end
        self.audio.on_utt_abort = self._on_utt_abort
        self._rt: stt_realtime.Session | None = None
        self.tts = TtsPipeline(self.audio, emit)
        self.tts.on_first_audio = self._lat_sounded
        self._t_speech_end = 0.0
        self._t_heard = 0.0
        self._lat_ear = ""
        self._first_said = False
        self._say_buf = ""
        # ---- two independent lanes (his order 2026-07-17) ----
        # TALK lane: Gemini Flash in the MIDDLE, out loud, always available.
        # WORK lane: the chosen Claude model on the LEFT, with tools, silent.
        # They run at the same time — he watches the working brain build on
        # the left while he keeps talking to Gemini in the middle. Roles are
        # display names from TALK_BRAINS / WORK_BRAINS; he sets them from the
        # drawer and nothing overrides his choice (no auto-routing, no
        # escalation).
        self.talk_brain = DEFAULT_TALK       # "gemini flash" (or "sonnet 5")
        self.work_model = DEFAULT_WORK        # normal working brain
        self.hard_model = DEFAULT_HARD        # heavy working brain
        # Thinking depth on the work lane. Effort is fixed when the client
        # connects (there is no set_effort on a live session), so changing it
        # from the drawer flags a reopen that the next dispatch performs.
        self.effort = DEFAULT_EFFORT
        self._work_options = None             # the live ClaudeAgentOptions
        self._effort_dirty = False            # reopen before the next turn
        self._reopen_only = False             # reopen WITHOUT losing the session
        # Talk lane state
        self.talk_busy = False                # a Gemini talk turn is running
        self.talk_client: ClaudeSDKClient | None = None  # only if talk=Claude
        self._talk_client_model = None
        self._talk_lock = asyncio.Lock()      # serialize talk turns (3.10+ safe)
        # Work lane state (Claude client = self.client)
        self.model = WORK_BRAINS.get(DEFAULT_WORK, MODEL_FULL)  # id on self.client
        self.busy = False                     # a WORK turn is in flight
        self.last_user_text = None            # work text (re-run on rotation)
        self.suppressed = False
        self._hold_deltas = False             # kept False now (no ESCALATE gate)
        self._delta_buf = ""
        # usage watch — session Claude totals, so Giorgi sees the burn and a
        # spoken heads-up the moment the API says the quota is gone.
        self.usage_in = 0
        self.usage_out = 0
        self.claude_out = False               # True once Claude quota is spent
        self.claude_reset = ""                # reset clock from the limit error
        # token economy: last work-turn context size, rolling exchange log for
        # rotation handoffs, and the rotation flags.
        self._last_ctx = 0
        self._exchanges = deque(maxlen=HANDOFF_KEEP)
        self._reply_acc = ""
        self._rotate_only = False
        self._pending_handoff = ""
        self._compacting = False  # a /compact turn is in flight (mute it)
        self._limit_warned = False
        self._stt_warned = False  # gates the spoken "transcriber down" warning
        # wake word: boot opens a conversation window (he just launched us);
        # after WAKE_WINDOW_S of silence, voice input must carry the name.
        self.wake_enabled = os.environ.get("GOAT_WAKE", "on").lower() not in (
            "off", "0", "false")
        # UI-controllable: muted mic drops utterances AND barge-in triggers
        # at the engine gate (audio threads keep running — cheap, reversible).
        self.mic_muted = False
        # "en" or "ka" — set by the UI before run() (boot) or live via
        # set_language(). Boot path appends LANG_NOTE_KA to the persona.
        self.language = "en"
        # Language of the turn being handled right now ("en"/"ka"). In fixed
        # modes it equals self.language; in "auto" it is whatever he just
        # spoke or typed, and it drives the voice AND the reply language.
        self.turn_lang = "en"
        self._last_exchange = time.monotonic()
        # talk-lane exchanges the work (Claude) session hasn't seen yet —
        # bridged into its next work turn so the working brain isn't blind to
        # what was just discussed out loud in the middle.
        self._local_unseen: list = []
        # work-lane step tracking (drives the left panel)
        self._current_task = ""
        self._work_started = 0.0
        self._last_tool = ""
        self._turn_has_tools = False  # True once a work turn touches a tool
        # work-lane OUTCOME tracking — feeds the talking brain's live status
        # note so "what is the working brain doing?" gets a real answer.
        self._work_done_at = 0.0      # monotonic time the last work turn ended
        self._work_failed = False     # last work turn errored / hit the limit
        self._last_work_summary = ""  # short tail of the last work reply

    # ---- audio-thread callbacks ----
    def _on_interrupt(self, _preroll):
        if self.mic_muted:
            return
        # Voice barge-in affects only the talking lane's VOICE — the work lane
        # is silent and keeps running. Cut GOAT off mid-sentence; his next
        # words start a fresh talk turn (or a spoken "stop" brakes the work
        # turn, handled in _talk).
        self.emit("status", "listening")
        self.tts.cancel()
        self._say_buf = ""

    async def _safe_interrupt(self):
        try:
            await self.client.interrupt()
        except Exception as e:  # noqa: BLE001
            self.emit("status", f"interrupt failed: {e}")

    async def _backchannel(self, gen: int):
        """One listening noise, if the gap runs long enough to need it.

        Never speaks over a reply that has already started, and never twice in
        a turn — a butler who says "mm-hm" every second is worse than one who
        says nothing."""
        if BACKCHANNEL_MODE in ("off", "0", "false", "no"):
            return
        try:
            await asyncio.sleep(BACKCHANNEL_AFTER_S)
        except asyncio.CancelledError:
            return
        if gen != self.tts.gen or self.tts._sounded or not self.tts.enabled:
            return    # the real answer beat it here, or the turn was cancelled
        pool = BACKCHANNEL.get(self.turn_lang) or BACKCHANNEL["en"]
        self._bc_i = (getattr(self, "_bc_i", -1) + 1) % len(pool)
        self.tts.say(pool[self._bc_i])

    # ---- latency ledger -----------------------------------------------------
    # His target: under 500ms from "he stops talking" to "GOAT makes a sound",
    # because human conversational gaps average ~200ms and anything past a
    # second reads as a machine. A target nobody measures is a wish, so every
    # voice turn prints its own budget and the UI shows the total.
    def _lat_start(self):
        # Back-date to the moment he actually stopped making sound. The VAD
        # hangover has already elapsed by the time this runs, and it is part
        # of the silence he sits in — counting from now would flatter every
        # number by exactly the wait he felt.
        hang = getattr(self.audio, "last_hangover_ms", 0.0) / 1000.0
        self._t_speech_end = time.monotonic() - hang
        self._lat_hang = hang * 1000
        self._t_heard = 0.0
        self._lat_ear = ""

    def _lat_heard(self, ear: str):
        if getattr(self, "_t_speech_end", 0.0):
            self._t_heard = time.monotonic()
            self._lat_ear = ear

    def _lat_sounded(self):
        t0 = getattr(self, "_t_speech_end", 0.0)
        if not t0:
            return
        now = time.monotonic()
        total = (now - t0) * 1000
        ear = (self._t_heard - t0) * 1000 if self._t_heard else -1
        rest = total - ear if ear >= 0 else -1
        self._t_speech_end = 0.0
        mark = "✓" if total < 500 else ("·" if total < 1000 else "!")
        hang = getattr(self, "_lat_hang", 0.0)
        print(f"[latency] {mark} endpoint {hang:.0f}ms + ear({self._lat_ear}) "
              f"{ear - hang:.0f}ms + think/voice {rest:.0f}ms "
              f"= {total:.0f}ms to first sound")
        self.emit("latency", f"{total:.0f}")

    # ---- streaming ear (audio callback thread — queue only, never block) ----
    def _rt_lang(self) -> str:
        """Which language the streaming ear should be pinned to for THIS turn.

        Realtime needs a pinned language or it guesses badly — it heard his
        Georgian as Russian and handed back Cyrillic transliteration. He runs
        in bilingual "auto", where there is nothing to pin, so stt_realtime
        opens one pinned ear per language and the ALPHABET decides which one
        answered (stt_realtime.Pair). Passing "auto" straight through is
        therefore correct, not a gap."""
        return self.language

    def _on_utt_start(self, preroll):
        if self.mic_muted or not self.loop:
            return
        self._rt = stt_realtime.start_threadsafe(self._rt_lang(), self.loop)
        if self._rt is not None and preroll is not None and len(preroll):
            self._rt.feed(preroll)   # his first word lives in the preroll

    def _on_utt_audio(self, chunk):
        rt = self._rt
        if rt is not None:
            rt.feed(chunk)

    def _on_utt_soft_end(self):
        rt = self._rt
        if rt is not None:
            rt.soft_commit()   # overlap the ear with GOAT's own VAD hangover

    def _on_utt_abort(self):
        rt, self._rt = self._rt, None
        if rt is not None:
            rt.end()

    def _on_utterance(self, audio_np: np.ndarray):
        self._lat_start()      # the clock he actually feels starts HERE
        rt, self._rt = self._rt, None
        if rt is not None:
            rt.end()          # commit at the boundary GOAT's own VAD chose
        if self.mic_muted:
            return
        if self.loop:
            asyncio.run_coroutine_threadsafe(
                self._handle_utterance(audio_np, rt), self.loop)

    def set_language(self, lang: str):
        """Live language switch from the UI (Qt thread — everything here is
        thread-safe): voice now, hearing in a worker thread (model reload
        ~5s), and one steering turn so the brain switches too."""
        if lang not in LANG_MODES or lang == self.language:
            return
        self.language = lang
        # "auto" has no fixed voice — the first thing he says picks one.
        # en/ka pin both the voice and the reply language immediately.
        if lang == "auto":
            self.turn_lang = self.turn_lang or "en"
        else:
            self.turn_lang = lang
            tts_edge.set_language(lang)
        self.emit("turnlang", self.turn_lang)
        if STT_KA_EXPERIMENT:
            def _stt():
                ok = stt_whisper.set_language(lang)
                self.emit("status", ("hearing ready — " + lang) if ok
                          else "hearing did not come back — restart me")
            threading.Thread(target=_stt, daemon=True).start()
        if lang == "auto":
            note = ("[language switch] Bilingual mode from now on: Giorgi "
                    "speaks and types BOTH Georgian (ქართული) and English and "
                    "switches freely. Answer every turn in the language of "
                    "that turn — never mix, never translate yourself. "
                    "Confirm in one short Georgian sentence.")
        elif lang == "ka":
            hearing = (" His speech now reaches you through cloud "
                       "transcription — slightly garbled sometimes, read "
                       "through it." if stt_gladia.available() else
                       " His voice still arrives in English; typed Georgian "
                       "works.")
            note = ("[language switch] From now on speak and write ONLY "
                    "Georgian (ქართული) — natural, native-level, same wit."
                    + hearing + " Confirm in one short Georgian sentence.")
        else:
            note = ("[language switch] Back to English only from now on. "
                    "Confirm in one short sentence.")
        self.submit_text(note)

    def request_ui_scale(self, spec: str):
        """GOAT resizing its own interface — called from local_hands when the
        model uses the resize_interface tool. spec is '<factor>' (absolute)
        or '*<factor>' (relative). Crosses to the Qt thread via emit."""
        self.emit("ui_scale", spec)

    def request_ui_color(self, part: str, color: str) -> bool:
        """GOAT recoloring its own UI. The Qt side validates the color and
        returns whether it applied; we optimistically report True and let the
        window reject a bad name (rare — the tool passes common names)."""
        self.emit("ui_color", f"{part}|{color}")
        return True

    def request_ui_character(self, name: str):
        """GOAT changing whose voice it speaks in. The Qt side owns the
        preference file, so the switch goes through the window."""
        self.emit("ui_character", name)

    def set_talk_brain(self, name: str):
        """Talking-brain pick from the drawer (display name). Gemini Flash is
        the default and stays up even when Claude is spent; 'sonnet 5' routes
        talk through a dedicated Claude talk client instead. Thread-safe."""
        if name not in TALK_BRAINS:
            name = DEFAULT_TALK
        self.talk_brain = name
        self.emit("status", f"talking brain: {name}")
        if TALK_BRAINS[name] == "gemini":
            # available() can hit the network — keep it off the Qt thread.
            def _report():
                self.emit("talkmodel", local_llm.LOCAL_NAME)
                if not local_llm.available():
                    self.emit("status", "gemini out of quota or unreachable — "
                              "sonnet covers the talk until it's back")
            threading.Thread(target=_report, daemon=True).start()
        else:
            self.emit("talkmodel", _friendly_model_name(TALK_BRAINS[name]))

    def set_effort(self, level: str):
        """Thinking depth for the work lane (low…max). Thread-safe: the live
        session can't be re-efforted, so this only flags the reopen and the
        next dispatch does it (with resume=, so the conversation survives)."""
        level = (level or "").strip().lower()
        if level not in EFFORT_LEVELS or level == self.effort:
            return
        self.effort = level
        self._effort_dirty = True
        self.emit("effort", level)
        self.emit("status", f"thinking: {level}")
        if self.loop is not None and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._apply_effort(), self.loop)

    async def _apply_effort(self):
        """Close the work client so run() rebuilds it at the new effort. The
        session id is kept, so the conversation carries over; a turn already
        in flight is never cut — this waits it out first."""
        for _ in range(900):          # ≤90s — a long turn still finishes
            if not self.busy:
                break
            await asyncio.sleep(0.1)
        if not self._effort_dirty:
            return                    # someone else already applied it
        self._reopen_only = True
        try:
            await self.client.disconnect()   # ends _consume → run() reopens
        except Exception:  # noqa: BLE001
            pass

    def set_work_model(self, name: str):
        """Working-brain pick (display name) — set on the work client at the
        next dispatch. Thread-safe."""
        if name not in WORK_BRAINS:
            name = DEFAULT_WORK
        self.work_model = name
        self.emit("status", f"working brain: {name}")

    def set_hard_model(self, name: str):
        """Hard-task working-brain pick (display name). Thread-safe."""
        if name not in WORK_BRAINS:
            name = DEFAULT_HARD
        self.hard_model = name
        self.emit("status", f"hard brain: {name}")

    def _typed_lang(self, text: str) -> None:
        """Bilingual mode also applies to TYPING: Georgian letters in, Georgian
        out. Alphabet, not guesswork — see stt_gladia.script_lang."""
        if self.language != "auto":
            return
        lang = stt_gladia.script_lang(text)
        if lang:
            self._set_turn_lang(lang)

    def submit_text(self, text: str):
        """Plain typed/spoken input — goes to the TALKING brain (middle lane).
        Thread-safe."""
        if self.loop is None or self.loop.is_closed():
            self.emit("status", "engine is down — check python\\goat-app.log")
            return
        self._typed_lang(text)
        asyncio.run_coroutine_threadsafe(self._talk(text), self.loop)

    def submit_work(self, text: str, hard: bool = False):
        """Explicit work order from the UI (work button / Ctrl+Enter, or the
        hard button / Ctrl+Shift+Enter) — goes to the WORKING brain on the
        left panel, or the hard-task brain when hard=True. This is the only
        path work reaches Claude, always his deliberate choice. Thread-safe."""
        if self.loop is None or self.loop.is_closed():
            self.emit("status", "engine is down — check python\\goat-app.log")
            return
        self._typed_lang(text)
        asyncio.run_coroutine_threadsafe(self._work(text, hard=hard), self.loop)

    def submit_files(self, paths: list, note: str = ""):
        """Files/images from the UI (drop, Ctrl+O, or pasted image) — a work
        order (looking at files needs the Read tool), so they run on the
        working brain's left lane. Thread-safe."""
        if self.loop is None or self.loop.is_closed():
            self.emit("status", "engine is down — check python\\goat-app.log")
            return
        text = ((note + "\n") if note else "") + "[files from Giorgi]\n" + "\n".join(paths)
        self.emit("work_files", "\n".join(paths))  # left-panel thumbnails
        asyncio.run_coroutine_threadsafe(self._work(text), self.loop)

    # ---- async side ----
    def _say_now(self, line: str):
        """Speak one short line immediately — no model, no lane, no waiting.
        This is the voice GOAT uses for acknowledgements and outcomes."""
        if not line:
            return
        self.tts.mark_reply()
        self.emit("delta", "")
        self.tts.say(line)

    def _ack(self, pool: dict):
        """Rotate through a pool so the same words don't repeat every time —
        a butler that says one identical sentence forever is a machine."""
        options = pool.get(self.turn_lang) or pool["en"]
        self._ack_i = (getattr(self, "_ack_i", -1) + 1) % len(options)
        self._say_now(options[self._ack_i])

    @staticmethod
    def _first_sentence(text: str, cap: int = 160) -> str:
        """One sentence out of the working brain's reply — what he actually
        wants to HEAR when a job lands. The panel still holds the full text."""
        line = " ".join((text or "").split())
        if not line:
            return ""
        for stop in (". ", "! ", "? "):
            i = line.find(stop)
            if 0 < i <= cap:
                return line[:i + 1]
        return (line[:cap].rstrip() + "…") if len(line) > cap else line

    def _prewarm_voice(self):
        """Fill the TTS cache for the voice that is current RIGHT NOW."""
        lang = self.turn_lang if self.turn_lang in ACK_ORDER else "en"
        lines = list(ACK_ORDER[lang]) + list(ACK_ADD[lang]) + list(
            ACK_REFLEX[lang]) + list(BACKCHANNEL[lang]) + [
            DONE_LEAD[lang], FAIL_LEAD[lang], REFLEX_FAIL[lang],
            "Stopped." if lang == "en" else "შევჩერდი."]
        threading.Thread(target=self.tts.prewarm, args=(lines,),
                         daemon=True).start()

    def _set_turn_lang(self, lang: str):
        """Point the voice at the language of THIS turn. Cheap and idempotent;
        in fixed en/ka modes it never moves off the chosen language."""
        if lang not in ("en", "ka") or lang == self.turn_lang:
            return
        self.turn_lang = lang
        tts_edge.set_language(lang)
        self.emit("turnlang", lang)
        self._prewarm_voice()

    async def _handle_utterance(self, audio_np: np.ndarray, rt=None):
        # The streaming ear has been transcribing since he started speaking,
        # so its answer is usually already in flight. Measured 2026-09-15 on
        # his own captured Georgian: 267-306ms after he stopped, against
        # 2520-2628ms for the batch ear on the same audio. Nothing is lost if
        # it fails — the full utterance is still in hand for the batch path.
        if rt is not None:
            text = await rt.result()
            if text:
                self._lat_heard("streaming")
                # In auto mode the ear that answered IS the language of the
                # turn — that is the whole point of running both.
                heard = getattr(rt, "lang", "") or self._rt_lang()
                self._set_turn_lang(heard if heard in ("en", "ka")
                                    else self.turn_lang)
                await self._heard(text)
                return
            self.emit("status", "streaming ear missed — using the full one")
        if self.language != "en" and stt_gladia.available():
            # Georgian or bilingual mode: cloud ear (local whisper can't do
            # ka at all — it romanizes it into English-looking nonsense,
            # which is exactly what "it hears English when I speak Georgian"
            # was). In "auto" nothing is pinned, so the same ear takes both
            # languages and tells us which one it heard.
            force = "ka" if self.language == "ka" else None
            text, heard = await asyncio.to_thread(
                stt_gladia.transcribe_lang, audio_np, 16000, force)
            if text is None:
                # Cloud route broke — English local hearing still works.
                self.emit("status", "georgian hearing offline — english ear on")
                text = await asyncio.to_thread(stt_whisper.transcribe, audio_np)
                self._set_turn_lang("en")
            else:
                self._set_turn_lang("ka" if self.language == "ka"
                                    else (heard or self.turn_lang))
        else:
            text = await asyncio.to_thread(stt_whisper.transcribe, audio_np)
            self._set_turn_lang("en")
        self._lat_heard("batch")
        await self._heard(text)

    async def _heard(self, text: str | None):
        """Everything that happens to a transcript once an ear produces one.

        Shared by BOTH ears on purpose: when the streaming ear was bolted on
        it briefly had its own shortcut into _talk, which quietly skipped the
        wake-word gate — so GOAT would have started answering the television
        again whenever the fast path won. One door in."""
        if text is None:
            # Hard STT failure — he spoke and his words went nowhere. Say it
            # (once per outage), never just log it: a deaf GOAT looks alive.
            self.emit("status", "transcriber is down — his words were lost")
            if not self._stt_warned:
                self._stt_warned = True
                self.emit("delta", "")  # creates the reply label for the reveal
                self.tts.say("I heard you, but my transcriber just failed — "
                             "give me a second and try again.")
            return
        self._stt_warned = False
        if not text:
            return  # silence/junk — normal, stay quiet
        if (self.wake_enabled and not self.busy
                and not self.audio.is_tts_playing
                and time.monotonic() - self._last_exchange > WAKE_WINDOW_S
                and not WAKE_RE.search(text)):
            # Idle and not addressed — JARVIS doesn't answer the TV.
            print(f"[wake] not addressed, ignored: {text!r}")
            self.emit("status", "heard — say my name to wake me")
            return
        await self._talk(text)

    async def _talk(self, text: str, echo: bool = True):
        """TALKING brain (middle lane): Gemini Flash out loud, zero Claude
        usage, always available — even while a work turn runs on the left and
        even when Claude's quota is gone. A short spoken "stop" brakes a
        running work turn; addressing the working brain by name hands the turn
        to the left lane. Otherwise plain talk stays here — no auto-routing."""
        text = text.strip()
        if not text:
            return
        self._last_exchange = time.monotonic()
        if echo:
            self.emit("you", text)
        # Spoken brake on a running work turn.
        if self.busy and STOP_RE.search(text) and len(text.split()) <= 5:
            await self._safe_interrupt()
            self.busy = False
            self.emit("work_fail", "Stopped.")
            self.emit("work_done", "")
            self.tts.mark_reply()
            self.emit("delta", "")
            self.tts.say("Stopped.")
            return
        # REFLEX LANE (2026-09-15, his complaint: "opening a file or Google
        # takes long — I want it to feel instant"). A device command that can
        # be recognised deterministically never touches a model: match is a
        # regex plus a dict lookup (3-400 microseconds, measured), the action
        # is one Win32/shell call, and the acknowledgement is a pre-synthesised
        # clip. The alternative was two Gemini round trips — and the talking
        # brain's own default, gemini-3.8-flash, measured 8-19s to first token
        # that same day, because reasoning cannot be disabled on Gemini 3.
        # Anything the matcher isn't sure about returns None and the normal
        # lanes take the turn exactly as before.
        # It runs during a work turn too — that is the point of a reflex: the
        # left lane can be twenty minutes into a build and "open Google" still
        # lands immediately, because nothing in this path is shared with it.
        try:
            rx = reflex.match(text, self.turn_lang)
        except Exception as e:  # noqa: BLE001 — a reflex bug must never cost
            rx = None           # him the turn; the brains still work
            self.emit("status", f"reflex check failed: {e}")
        if rx is not None:
            await self._reflex(text, rx)
            return
        # Manual dispatch: he addressed the working brain by name — as the
        # opener ("Fable, build…") or mid-sentence ("please ask the opus to…").
        # A status QUESTION about it ("what is the working brain doing?") is
        # talk, not dispatch — Gemini answers it from the live status note.
        if (not WORK_STATUS_ASK_RE.search(text)
                and (WORK_DISPATCH_RE.match(text) or WORK_ASK_RE.search(text)
                     or (ORDER_RE.search(text)
                         and not QUESTION_LEAD_RE.match(text)
                         and not QUICK_TOPIC_RE.search(text)))):
            await self._work(text, hard=bool(WORK_HARD_RE.search(text[:80])),
                             echo_you=False)
            return
        async with self._talk_lock:
            self.talk_busy = True
            if echo:
                self.tts.cancel()
            self.tts.new_turn()
            self._first_said = False   # new turn: first breath may clause-break
            # Cover the gap with a listening noise if the brain is slow. It
            # cancels itself the moment real audio starts, so a fast reply
            # never hears from it.
            bc = asyncio.create_task(self._backchannel(self.tts.gen))
            try:
                brain = TALK_BRAINS.get(self.talk_brain)
                if brain != "gemini" and self.claude_out:
                    # His pick is a Claude voice but the quota's spent —
                    # Gemini covers so talk NEVER goes down with Claude.
                    self.emit("status", "claude out — gemini covers the talk")
                    brain = "gemini"
                if brain == "gemini":
                    await self._talk_gemini(text)
                elif not await self._talk_claude(
                        text, TALK_BRAINS[self.talk_brain]):
                    # Claude talk failed (limit mid-turn, stream error) —
                    # Gemini takes the turn instead of leaving him in silence.
                    await self._talk_gemini(text)
            finally:
                bc.cancel()
                self.talk_busy = False
                self._last_exchange = time.monotonic()

    async def _reflex(self, text: str, rx):
        """Run one reflex: speak and act at the same instant.

        The ack is queued BEFORE the action runs, not after, because the
        acknowledgement is the part he perceives as speed — os.startfile
        takes a few milliseconds but the window it opens takes longer, and
        waiting for either before saying anything is what made GOAT feel slow.
        If the action does fail, the correction is spoken straight after; a
        reflex is never allowed to say "done" about something that didn't
        happen."""
        t0 = time.monotonic()
        self.tts.cancel()
        self.tts.new_turn()
        self._first_said = False   # new turn: first breath may clause-break
        if rx.speak:
            # A question GOAT answers itself (time, date, battery) — the
            # answer IS the reply, so there is nothing to acknowledge.
            self._say_now(rx.speak)
            spoken = rx.speak
        else:
            options = ACK_REFLEX.get(self.turn_lang) or ACK_REFLEX["en"]
            self._ack_i = (getattr(self, "_ack_i", -1) + 1) % len(options)
            spoken = options[self._ack_i]
            self._say_now(spoken)
        self.emit("status", f"{rx.kind} · {rx.detail}")
        try:
            result = await asyncio.to_thread(rx.run)
        except Exception as e:  # noqa: BLE001
            result = f"ERROR: {e}"
        ms = (time.monotonic() - t0) * 1000
        print(f"[reflex] {rx.kind} {rx.detail!r} -> {result} ({ms:.0f}ms)")
        if isinstance(result, str) and result.startswith("ERROR"):
            line = REFLEX_FAIL.get(self.turn_lang) or REFLEX_FAIL["en"]
            self._say_now(line)
            spoken = line
            self.emit("status", result[:80].lower())
        # No _flush_sentences here on purpose: a reflex never streams deltas,
        # so _say_buf holds nothing of this turn's — forcing a flush would
        # speak a stale fragment left over from an interrupted one.
        self._say_buf = ""
        self._last_exchange = time.monotonic()
        reply = f"{spoken} ({rx.detail})" if rx.detail and not rx.speak else spoken
        self._exchanges.append((text[:300], reply[:300]))
        self._local_unseen.append((text[:200], reply[:200]))
        # Both talking brains must know it happened, or the next question
        # ("did you open it?") gets answered by a model with no memory of it.
        local_llm.note_exchange(text, reply)
        self._log_exchange(text, reply)
        self.emit("turn_done", "")

    def _talk_model_label(self) -> str:
        """Footer name of the talking brain he currently has selected."""
        picked = TALK_BRAINS.get(self.talk_brain, "gemini")
        return (local_llm.LOCAL_NAME if picked == "gemini"
                else _friendly_model_name(picked))

    def _cover_model(self) -> str:
        """The Claude voice that covers when Gemini is down — his own pick if
        it IS a Claude voice, else Sonnet (measured fastest here, see the
        roster note)."""
        picked = TALK_BRAINS.get(self.talk_brain, "gemini")
        return picked if picked != "gemini" else MODEL_FAST

    async def _escalate_from_talk(self, text: str):
        """The talking side said ESCALATE. One net for BOTH talking brains —
        until now only the Gemini path had these guards, so the same order
        behaved differently depending on which voice he had selected.

        Called while the talk lock is held: answer inline, never via a helper
        that re-takes the lock."""
        if WORK_STATUS_ASK_RE.search(text):
            # He asked ABOUT the work and the talking brain punted anyway —
            # answer the status question deterministically instead of
            # dispatching his question as a job (seen live 2026-07-18: "what
            # is working brain doing" vanished into the silent work lane).
            line = "Here's the working side: " + self._work_status_line() + "."
            self._speak_delta(line)
            self._finish_talk(text, line)
            return
        if self.claude_out:
            # He asked for the working brain but the quota's spent. Say it
            # RIGHT HERE — _offline_cover would take the lock we already hold.
            self.emit("work_fail", "Claude is out of usage"
                      + (f" — resets {self.claude_reset}"
                         if self.claude_reset else "")
                      + ". Talk still works.")
            line = ("Claude is rate-limited right now, so that work has to wait"
                    + (f" until about {self.claude_reset}"
                       if self.claude_reset else "")
                    + ". I can still answer questions, search, and use my own "
                    "hands for everything else.")
            self._speak_delta(line)
            self._finish_talk(text, line)
            return
        await self._work(text, hard=bool(WORK_HARD_RE.search(text[:80])),
                         echo_you=False)

    async def _talk_gemini(self, text: str):
        """One Gemini talk turn → middle lane + voice. Falls to a Claude cover
        voice if Gemini is momentarily down; hands to the work lane if he named
        the working brain mid-sentence."""
        self.emit("talkmodel", local_llm.LOCAL_NAME)
        self.emit("delta", "")  # open the middle reply label
        loop = asyncio.get_running_loop()

        def on_delta(piece: str):
            loop.call_soon_threadsafe(self._speak_delta, piece)

        try:
            reply = await asyncio.to_thread(
                local_llm.chat, text, on_delta, self.turn_lang,
                status=self._work_status_line())
        except Exception as e:  # noqa: BLE001 — talk brain down ≠ mute GOAT
            self.emit("status", f"talking brain failed: {e}")
            reply = None
        if is_escalation(reply):
            await self._escalate_from_talk(text)
            return
        if reply is None:
            if not self.claude_out:
                self.emit("status", "gemini offline — sonnet covering the talk")
                if await self._talk_claude(text, self._cover_model()):
                    return
            # Claude's out too (or the cover also failed) — one honest line,
            # never silence, never a raw error.
            self.emit("delta", "")
            self.tts.say("My talking brain is offline for a moment — "
                         "give me a few seconds and try again.")
            return
        self._finish_talk(text, reply)

    async def _talk_claude(self, text: str, model: str) -> bool:
        """Talk turn on a dedicated Claude talk client (talk brain = Sonnet, or
        the Gemini-down cover voice). Streams to the middle + voice, never
        touches the work client, so it runs alongside a work turn. Returns True
        when it actually spoke."""
        try:
            await self._ensure_talk_client(model)
        except Exception as e:  # noqa: BLE001
            self.emit("status", f"talk client failed: {e}")
            return False
        self.emit("talkmodel", _friendly_model_name(model))
        self.emit("delta", "")
        reply = ""
        # Same live window into the work lane that Gemini gets — so this
        # voice can also answer "what is the working brain doing?".
        send = (f"[live working-brain status: {self._work_status_line()}]\n\n"
                + text)
        if self.turn_lang == "ka":
            send = KA_TALK_NOTE + send
        try:
            await self.talk_client.query(send)
            streamed = False
            async for msg in self.talk_client.receive_response():
                if isinstance(msg, StreamEvent):
                    delta = (msg.event or {}).get("delta", {}) or {}
                    if delta.get("type") == "text_delta":
                        piece = delta.get("text", "")
                        if piece:
                            streamed = True
                            reply += piece
                            self._speak_delta(piece)
                elif isinstance(msg, AssistantMessage):
                    # Fallback for a build with no partials: speak the block.
                    if not streamed:
                        for b in msg.content:
                            if isinstance(b, TextBlock) and b.text:
                                reply += b.text
                                self._speak_delta(b.text)
                elif isinstance(msg, ResultMessage):
                    self._track_usage(msg)
        except Exception as e:  # noqa: BLE001
            self.emit("status", f"talk turn failed: {e}")
            return False
        reply = reply.strip()
        if not reply:
            return False
        if is_escalation(reply):
            # The SAME net Gemini gets — status question, spent quota, hard
            # brain. Before this the Claude voice skipped all three and
            # dispatched blind (and an "ESCALATE." with a full stop wasn't
            # even recognised: it got spoken to him as if it were an answer).
            self.talk_busy = False
            await self._escalate_from_talk(text)
            return True
        self._finish_talk(text, reply, note_gemini=True)
        return True

    async def _ensure_talk_client(self, model: str):
        """Lazily spawn / re-model the dedicated talk client (own short
        session — conversation plus QUICK actions)."""
        if self.talk_client is None:
            opts = ClaudeAgentOptions(
                cwd=WORKSPACE, model=model, effort="low",
                # Speak as the words arrive. Without partials the whole
                # answer had to finish first: measured 2.17s to first sound
                # on a warm session, 5.35s cold — seconds of silence after
                # he stopped talking, which is what "slow" actually was.
                include_partial_messages=True,
                # bypassPermissions matches the work client — without it the
                # cover voice hits Claude Code's approval gate on its first
                # tool call and starts telling Giorgi to "tap the prompt"
                # (seen live 2026-07-17: "open chrome" stonewalled). And
                # max_turns=1 gave no room to act at all — 4 covers one
                # quick action plus the spoken result.
                permission_mode="bypassPermissions",
                system_prompt={"type": "preset", "preset": "claude_code",
                               "append": TALK_PERSONA},
                setting_sources=[], max_turns=4,
            )
            self.talk_client = ClaudeSDKClient(opts)
            await self.talk_client.connect()
            self._talk_client_model = model
        elif self._talk_client_model != model:
            try:
                await self.talk_client.set_model(model)
                self._talk_client_model = model
            except Exception:  # noqa: BLE001
                pass

    def _finish_talk(self, text: str, reply: str, note_gemini: bool = False):
        """Close a talk turn: flush the voice tail, log it, keep both talking
        brains' memories in step."""
        self._flush_sentences(force=True)
        self._exchanges.append((text[:300], reply[:300]))
        self._local_unseen.append((text[:200], reply[:200]))
        if note_gemini:
            # Claude-side talk: mirror it into Gemini's history so the two
            # talking brains stay coherent if he switches between them.
            local_llm.note_exchange(text, reply)
        self._log_exchange(text, reply)
        self.emit("turn_done", "")

    def _work_status_line(self) -> str:
        """One live line about the WORK lane, fed to the talking brain every
        talk turn so "what is the working brain doing?" gets a real answer
        (his ask 2026-07-18 — before this, Gemini had no window into the
        left lane and the question fell through to the silent work lane)."""
        name = _friendly_model_name(self.model)
        if self.claude_out:
            return ("the working brain (Claude) is OUT OF USAGE"
                    + (f" — resets around {self.claude_reset}"
                       if self.claude_reset else "")
                    + "; coding/repo jobs wait until then")
        if self.busy and self._compacting:
            return (f"{name} is tidying its own context between jobs — "
                    "a few seconds, then it's free")
        if self.busy:
            mins = int((time.monotonic() - self._work_started) // 60)
            age = f"about {mins} min in" if mins else "just started"
            line = (f"{name} is busy right now ({age}) on: "
                    f"\"{self._current_task[:160]}\"")
            if self._last_tool:
                line += f" — latest step: {self._last_tool}"
            tail = self._reply_acc.strip()
            if tail:
                line += f" — its latest note: …{tail[-200:]}"
            return line
        if self._work_done_at:
            mins = int((time.monotonic() - self._work_done_at) // 60)
            ago = f"about {mins} min ago" if mins else "just now"
            verdict = "FAILED" if self._work_failed else "finished"
            line = (f"idle — its last job {verdict} {ago}: "
                    f"\"{self._current_task[:120]}\"")
            if self._last_work_summary:
                line += f" — outcome: {self._last_work_summary[:220]}"
            return line
        return "idle — no job given to it yet this session"

    async def _work(self, text: str, hard: bool = False, echo_you: bool = False):
        """WORK lane (left panel): run the chosen Claude model WITH tools,
        streaming each step to the left. Silent — the working brain doesn't
        speak (Giorgi hears Gemini in the middle) and his order shows as the
        task on the LEFT, not in the talk column. His deliberate dispatch
        only; nothing escalates itself here."""
        text = text.strip()
        if not text:
            return
        if echo_you:
            self.emit("you", text)  # (unused by default; left panel owns it)
        target_name = self.hard_model if hard else self.work_model
        target = WORK_BRAINS.get(target_name, MODEL_FULL)
        if self.busy:
            # A work turn is already running — fold this order in so the
            # running turn sees his additions (Node parity).
            if self.last_user_text:
                self.last_user_text += "\n" + text
            self.emit("work_add", text[:120])
            self._ack(ACK_ADD)
            await self.client.query(text)
            return
        if self.claude_out:
            # Quota's gone — don't pretend to start. Note it on the left, and
            # let Gemini SAY it and pick up what its own hands can do (his
            # order 2026-07-17: the app must never feel dead because Claude
            # is out; only repo/coding-agent work waits).
            self.emit("work_fail", "Claude is out of usage"
                      + (f" — resets {self.claude_reset}" if self.claude_reset
                         else "") + ". Talk still works.")
            await self._offline_cover(text)
            return
        self.busy = True
        self._ack(ACK_ORDER)   # "on it" NOW — not after the model answers
        self.last_user_text = text
        self._current_task = text
        self._work_started = time.monotonic()
        self._turn_has_tools = False
        self._last_tool = ""
        self._reply_acc = ""
        if self.model != target:
            try:
                await self.client.set_model(target)
                self.model = target
            except Exception as e:  # noqa: BLE001
                self.emit("status", f"model switch failed: {e}")
        self.emit("model", _friendly_model_name(target))
        self.emit("work_start", f"{_friendly_model_name(target)}|{text}")
        # Bridge recent middle-lane chat so the working brain isn't blind to
        # what was just said out loud.
        send = text
        if self._local_unseen:
            lines = "\n".join(f"him: {u}\nyou: {a}"
                              for u, a in self._local_unseen[-6:])
            send = ("[chat since your last turn — context only, do not reply "
                    "to it]\n" + lines + "\n\n" + send)
            self._local_unseen.clear()
        if self._pending_handoff:
            send = self._pending_handoff + "\n\n" + send
            self._pending_handoff = ""
        if self.turn_lang == "ka":
            # The work client's persona is pinned at connect and a live
            # language switch only steers the TALK lane — so the left lane
            # is told per dispatch. Without this he gets Georgian in the
            # middle and English on the left in the same breath.
            send = KA_WORK_NOTE + send
        await self.client.query(send)

    async def _offline_cover(self, text: str):
        """A work order arrived while Claude's quota is spent: Gemini answers
        in the middle lane instead — one warm line that the coding brain must
        wait, then it does whatever parts its OWN tools cover (web, files,
        shell). The app stays alive; only Claude-side work pauses."""
        reset = (f" It resets around {self.claude_reset}."
                 if self.claude_reset else "")
        prompt = ("[Claude — your working brain — is out of usage right now."
                  + reset + " Giorgi sent the order below to it. Tell him in "
                  "one warm sentence that repo/coding-agent work waits for "
                  "Claude, then do whatever parts YOU can with your own "
                  "tools.]\n" + text)
        async with self._talk_lock:
            self.talk_busy = True
            self.tts.new_turn()
            self._first_said = False   # new turn: first breath may clause-break
            try:
                self.emit("talkmodel", local_llm.LOCAL_NAME)
                self.emit("delta", "")
                loop = asyncio.get_running_loop()

                def on_delta(piece: str):
                    loop.call_soon_threadsafe(self._speak_delta, piece)

                try:
                    reply = await asyncio.to_thread(
                        local_llm.chat, prompt, on_delta, self.turn_lang, True,
                        status=self._work_status_line())
                except Exception as e:  # noqa: BLE001 — cover must not crash
                    self.emit("status", f"offline cover failed: {e}")
                    reply = None
                if reply is None or is_escalation(reply):
                    self.emit("delta", "")
                    self.tts.say("Claude is out of usage right now, and my "
                                 "fast brain hiccuped too — give me a moment "
                                 "and ask again.")
                    return
                self._finish_talk(text, reply)
            finally:
                self.talk_busy = False
                self._last_exchange = time.monotonic()

    def _speak_delta(self, text: str):
        self.emit("delta", text)
        self._say_buf += text
        self._flush_sentences()

    def _flush_sentences(self, force: bool = False):
        while True:
            m = SENTENCE_RE.match(self._say_buf)
            if not m or not m.group(1).strip():
                break
            self.tts.say(m.group(1))
            self._first_said = True
            self._say_buf = self._say_buf[m.end():]
        if force:
            self.tts.say(self._say_buf)
            self._say_buf = ""
            return
        # FIRST words of a turn only: don't wait for a full stop.
        #
        # Every later sentence is synthesised while the previous one is still
        # playing, so its ~0.5s of edge-tts is free. The FIRST one is the only
        # one nobody is covering, and a model that opens with a long clause
        # ("The build failed because the confidence gate was comparing ninety
        # against eighty-five, and I've pushed the fix.") used to keep him in
        # silence until the very last word of it arrived. Breaking at the
        # first comma or dash once there is enough to say starts the voice a
        # sentence earlier, at the cost of one extra synthesis call.
        if getattr(self, "_first_said", False):
            return
        m = FIRST_CLAUSE_RE.match(self._say_buf)
        if m and len(m.group(1).strip()) >= FIRST_CLAUSE_MIN:
            self.tts.say(m.group(1))
            self._first_said = True
            self._say_buf = self._say_buf[m.end():]

    async def _consume(self):
        """Drives the WORK lane (self.client). Everything here streams to the
        LEFT panel and NEVER speaks — the voice belongs to the talk lane. On a
        Claude usage-out or error the work turn ends gracefully; Gemini keeps
        talking in the middle."""
        async for msg in self.client.receive_messages():
            if isinstance(msg, StreamEvent):
                ev = msg.event
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta", {})
                    if (delta.get("type") == "text_delta"
                            and not self.suppressed and not self._compacting):
                        t = delta.get("text", "")
                        if t:
                            self.emit("work_text", t)  # working brain narration
                    elif (delta.get("type") == "thinking_delta"
                            and not self.suppressed and not self._compacting):
                        # Adaptive thinking, display="summarized": the model's
                        # own summary of what it is working out. At max effort
                        # this is the difference between a live instrument and
                        # a frozen panel — it fills the gap before tool one.
                        t = delta.get("thinking", "")
                        if t:
                            self.emit("work_think", t)
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        t = (block.text or "").strip()
                        if t and not self._compacting:
                            self._reply_acc += t + " "
                    elif isinstance(block, ToolUseBlock):
                        self._turn_has_tools = True  # this is a WORK turn now
                        self._last_tool = block.name
                        self.emit("work_tool", _describe_tool(block))
            elif isinstance(msg, SystemMessage):
                if msg.subtype == "init":
                    sid = (msg.data or {}).get("session_id")
                    if sid:
                        with open(SESSION_FILE, "w", encoding="utf-8") as f:
                            f.write(sid)
            elif isinstance(msg, ResultMessage):
                if self._compacting:
                    # The muted /compact turn just finished. Trust nothing —
                    # measure. If context actually shrank, carry on in the
                    # same session; otherwise hard-rotate with the handoff.
                    self._compacting = False
                    self.suppressed = False
                    self.busy = False
                    try:
                        cu = await self.client.get_context_usage()
                        after = int(cu.get("totalTokens") or 0)
                    except Exception:  # noqa: BLE001
                        after = ROTATE_CTX + 1
                    if after > ROTATE_CTX:
                        self._rotate_only = True
                        self._last_ctx = 0
                        try:
                            os.remove(SESSION_FILE)
                        except OSError:
                            pass
                        self._pending_handoff = self._handoff_text()
                        self.emit("status", "compact failed — rotated instead")
                        return True
                    self._last_ctx = after
                    self.emit("work_ctx", f"{after}|{ROTATE_CTX}")
                    self.emit("status",
                              f"context compacted to {after // 1000}k — usage saved")
                    continue
                self.suppressed = False
                self.busy = False
                err = str(getattr(msg, "result", "") or "").lower()
                if msg.is_error and "prompt is too long" in err:
                    # Work session's context is full — start a fresh one and
                    # retry the order that hit the wall. Silent on the left;
                    # talk (Gemini) is untouched.
                    try:
                        os.remove(SESSION_FILE)
                    except OSError:
                        pass
                    self._pending_handoff = self._handoff_text()
                    self.emit("work_step",
                              "context full — fresh session, retrying")
                    return True  # run() reconnects and retries
                self._work_done_at = time.monotonic()
                if self._track_usage(msg):
                    # Quota gone — mark it, show it on the left, keep Gemini
                    # talking in the middle (his rule 4). No retry.
                    self.claude_out = True
                    self._work_failed = True
                    self._last_work_summary = ("it hit Claude's usage limit "
                                               "mid-job; the job waits for "
                                               "the reset")
                    self.emit("work_fail", "Claude ran out of usage"
                              + (f" — resets {self.claude_reset}"
                                 if self.claude_reset else "")
                              + ". I can still talk.")
                    self.emit("work_done", "")
                elif msg.is_error and not self._reply_acc.strip():
                    # Work errored with nothing produced — surface it on the
                    # left, don't crash, don't speak (talk owns the voice).
                    self._work_failed = True
                    self._last_work_summary = f"it errored: {err[:150]}"
                    self.emit("work_fail", f"working brain error: {err[:120]}")
                    self._say_now(
                        (FAIL_LEAD.get(self.turn_lang) or FAIL_LEAD["en"])
                        + " " + self._first_sentence(err, 120))
                    self.emit("work_done", "")
                else:
                    if self.claude_out:
                        self.claude_out = False  # a turn landed — quota's back
                        self.emit("claude", "ok")
                    self._work_failed = False
                    self._last_work_summary = self._reply_acc.strip()[-250:]
                    # Turn done — log the exchange for future handoffs and
                    # measure how heavy this session has become.
                    if self.last_user_text:
                        reply = self._reply_acc.strip()
                        self._exchanges.append(
                            (self.last_user_text[:300], reply[:300]))
                        # Keep the talking brain's memory in step with the work.
                        local_llm.note_exchange(self.last_user_text, reply)
                        if not self.last_user_text.startswith("[boot-briefing]"):
                            self._log_exchange(self.last_user_text, reply)
                        # JARVIS closes the loop OUT LOUD: he gave an order,
                        # he gets told when it is done and what happened —
                        # one sentence, because the panel has the rest.
                        self._say_now(
                            outcome_line(
                                reply,
                                DONE_LEAD.get(self.turn_lang) or DONE_LEAD["en"],
                                self._first_sentence(reply)))
                        self._reply_acc = ""
                    self.emit("work_done", "")
                    u = msg.usage or {}
                    self._last_ctx = ((u.get("input_tokens") or 0)
                                      + (u.get("cache_read_input_tokens") or 0)
                                      + (u.get("cache_creation_input_tokens") or 0))
                    # How full this session is, and how close to the trim.
                    # The number drives every rotation decision below, so the
                    # left panel shows it instead of keeping it a secret.
                    self.emit("work_ctx", f"{self._last_ctx}|{ROTATE_CTX}")
                    if self._last_ctx > ROTATE_CTX:
                        # Trim BEFORE the wall. Preferred: the CLI's own
                        # /compact — same session, model-written summary.
                        if COMPACT_CLI:
                            try:
                                self._compacting = True
                                self.suppressed = True
                                self.busy = True
                                self.emit("status", "compacting context…")
                                await self.client.query("/compact")
                                continue
                            except Exception:  # noqa: BLE001
                                self._compacting = False
                                self.suppressed = False
                                self.busy = False
                        # Fallback: fresh session; the next message
                        # carries the handoff.
                        self._rotate_only = True
                        self._last_ctx = 0
                        try:
                            os.remove(SESSION_FILE)
                        except OSError:
                            pass
                        self._pending_handoff = self._handoff_text()
                        self.emit("status", "context rotated — usage saved")
                        return True  # run() reconnects fresh, no retry
        # Stream ended. A clean end normally means shutdown — unless the
        # thinking dial moved, in which case _apply_effort() closed the
        # client on purpose and run() should rebuild it at the new effort.
        if self._reopen_only:
            return True
        return False

    def _log_exchange(self, user: str, reply: str):
        """Append to the on-disk transcript (UI repaints the tail at boot).
        Trims occasionally; never allowed to break a turn."""
        try:
            line = json.dumps({"t": time.time(), "user": user[:400],
                               "reply": reply[:600]}, ensure_ascii=False)
            with open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if os.path.getsize(TRANSCRIPT_FILE) > 200_000:
                with open(TRANSCRIPT_FILE, encoding="utf-8") as f:
                    tail = f.readlines()[-TRANSCRIPT_MAX:]
                with open(TRANSCRIPT_FILE, "w", encoding="utf-8") as f:
                    f.writelines(tail)
        except OSError:
            pass

    def _handoff_text(self) -> str:
        """Zero-cost session handoff: the recent exchanges GOAT already has
        in Python, packed into the first message of the fresh session."""
        if not self._exchanges:
            return ""
        lines = [f"Giorgi: {u}\nYou: {r}" for u, r in self._exchanges]
        return ("[context-handoff] Your previous session was rotated to save "
                "Giorgi's usage. Recent conversation, oldest first:\n"
                + "\n".join(lines)
                + "\nLong-term memory lives in workspace/memory.md. Continue "
                "naturally; don't mention the rotation unless asked.")

    def _track_usage(self, msg: ResultMessage) -> bool:
        """Accumulate session Claude token totals for the UI meter, and detect
        the out-of-usage error. Returns True when the quota is exhausted."""
        u = msg.usage or {}
        self.usage_in += (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        self.usage_out += u.get("output_tokens") or 0
        self.emit("usage", f"{self.usage_in}|{self.usage_out}")

        text = str(getattr(msg, "result", "") or "")
        if msg.is_error and CLAUDE_LIMIT_RE.search(text):
            self.claude_out = True
            reset = ""
            m = re.search(r"\|(\d{9,11})", text)
            if m:  # old machine form: "...|<unix-ts>"
                t = datetime.datetime.fromtimestamp(int(m.group(1)))
                reset = t.strftime("%H:%M")
            else:  # human form: "resets 2:30am (Asia/Tbilisi)" — his zone
                m = CLAUDE_RESET_RE.search(text)
                if m:
                    h = int(m.group(1)) % 24
                    mnt = int(m.group(2) or 0)
                    ap = (m.group(3) or "").lower()
                    if ap == "pm" and h != 12:
                        h += 12
                    elif ap == "am" and h == 12:
                        h = 0
                    reset = f"{h:02d}:{mnt:02d}"
            if reset:
                self.claude_reset = reset
            self.emit("claude", "out|" + reset)  # UI meter
            # Spoken heads-up comes through the talk lane's voice (once).
            # NEVER the raw CLI error — GOAT's own words only.
            warn = ("Giorgi, Claude — my working brain — hit its usage limit"
                    + (f"; it resets around {reset}" if reset else "")
                    + ". Code and repo work waits until then, but I'm still "
                    "here — questions, web, files, planning, all of it.")
            self.emit("limit", warn)
            if not self._limit_warned:
                self._limit_warned = True
                self.emit("delta", "")  # middle reply label — GOAT says it
                self.tts.say(warn)
            return True
        self._limit_warned = False
        return False

    @staticmethod
    def _read_battery() -> tuple | None:
        """(charge%, on_ac) from WMI, None when unreadable. Blocking —
        runs in a worker thread."""
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$b = Get-CimInstance Win32_Battery; "
                 "\"$($b.EstimatedChargeRemaining)|$($b.BatteryStatus)\""],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            charge_s, status_s = (out.stdout or "").strip().split("|")
            charge = int(charge_s) if charge_s else None
            return (charge, status_s.strip() == "2")
        except Exception:  # noqa: BLE001 — no battery, no watcher
            return None

    async def _power_watch(self):
        """Background watcher: speaks on AC loss / low battery. Alerts are
        rate-limited (one per 5 minutes) and only spoken when idle — mid-turn
        they land as a status line instead."""
        prev = None
        last_alert = 0.0
        while True:
            await asyncio.sleep(POWER_POLL_S)
            cur = await asyncio.to_thread(self._read_battery)
            warn = power_verdict(prev, cur)
            if cur is not None:
                prev = cur
            if warn and time.monotonic() - last_alert > 300:
                last_alert = time.monotonic()
                self.emit("status", warn[:80])
                if not self.busy and not self.audio.is_tts_playing:
                    self.emit("you", "[power watch]")
                    self.emit("delta", "")
                    self.tts.mark_reply()
                    self.tts.say(warn)

    async def _warm_up(self):
        """Cold-start guard (bug #3): the canceller has never seen this room
        at process start — play a scripted line with interrupt decisions
        disabled so it can adapt before anything can false-trigger."""
        self.emit("status", "learning the room — one moment")
        samples = await asyncio.to_thread(self.tts.synth, _greeting())
        self.audio.warming_up = True
        self.audio.queue_playback(samples)
        while self.audio.is_tts_playing:
            await asyncio.sleep(0.1)
        self.audio.warming_up = False

    async def run(self):
        self.loop = asyncio.get_running_loop()
        # Away-time, read BEFORE this boot's init overwrites the session
        # file: its mtime is when the LAST session was live.
        away_h = None
        try:
            away_h = (time.time() - os.path.getmtime(SESSION_FILE)) / 3600
        except OSError:
            pass  # no session file — fresh brain, greeting alone covers it
        self.emit("status", "starting speech recognition...")
        if self.language in ("ka", "auto"):
            # ka pins the Georgian voice now. auto RESUMES the language the
            # last conversation ended in (the UI hands it over on bind) —
            # restarting mid-Georgian-conversation and being greeted in
            # English is exactly the seam this mode is meant to remove.
            if self.language == "ka":
                self.turn_lang = "ka"
            tts_edge.set_language(self.turn_lang)
            if STT_KA_EXPERIMENT:
                stt_whisper.LANGUAGE = self.turn_lang
        # Boot latency (2026-07-15): whisper model load, Claude SDK connect,
        # and mic calibration are independent — run them CONCURRENTLY and
        # speak the greeting as soon as the mic is calibrated; the ears and
        # the working brain finish loading behind the greeting instead of
        # in front of it (old serial chain = every step added to silence).
        stt_task = asyncio.create_task(
            asyncio.to_thread(stt_whisper.ensure_server))

        persona = PERSONA + (LANG_NOTE_KA if self.language == "ka"
                             else LANG_NOTE_AUTO if self.language == "auto"
                             else "")
        options = ClaudeAgentOptions(
            cwd=WORKSPACE,
            permission_mode="bypassPermissions",
            model=WORK_BRAINS.get(self.work_model, MODEL_FULL),
            # Every current model thinks adaptively; effort is the dial, and
            # he asked for the top of it. display="summarized" is what makes
            # the reasoning visible on the left instead of a silent gap.
            effort=self.effort,
            thinking=THINKING_CFG,
            # If the picked brain can't serve (Fable's separate credit
            # bucket, or an overload), the turn lands on Opus 5 instead of
            # dying. Without this a credit-less Fable pick kills work.
            fallback_model=MODEL_FULL,
            system_prompt={"type": "preset", "preset": "claude_code", "append": persona},
            include_partial_messages=True,
            # "project" = ONLY workspace/.claude — GOAT's own skill library.
            # Giorgi's global plugins/hooks stay out (the latency win that
            # setting_sources=[] originally bought is preserved).
            setting_sources=["project"],
            # Eyes and hands on the screen itself, in-process: `computer` and
            # `browser` land in the same tool list as Bash and Read, called the
            # same way, with no extra process or port (2026-09-14). Everything
            # that only exists as pixels was unreachable before this.
            mcp_servers={"screen": screen_tools.SERVER},
            resume=saved_session_id(),
        )
        self._work_options = options
        # Every mouse move and keystroke GOAT makes shows up on the left panel
        # as it happens. Screen actions are the one kind of work he can't read
        # back off disk afterwards, so they have to be visible while they run.
        screen_tools.set_emit(self.emit)
        self.model = options.model
        self.client = ClaudeSDKClient(options)
        connect_task = asyncio.create_task(self.client.connect())

        self.audio.start()
        self.emit("status", "calibrating — stay quiet for 2 seconds")
        await asyncio.to_thread(self.audio.calibrate, 2.0)
        await self._warm_up()
        stt_ok = await stt_task
        await connect_task
        if not stt_ok:
            # Boot self-check, spoken: without this the window looks alive
            # while every word he says silently goes nowhere.
            self.emit("status", "HEARING OFFLINE — whisper-server did not start")
            self.emit("delta", "")  # creates the reply label for the reveal
            self.tts.say("Heads up — my hearing did not come up. "
                         "I can't transcribe you until you restart me.")
        else:
            self.emit("status", "listening — just talk")
        # Boot footer: the talking brain is the always-on voice, so the footer
        # shows it; the work brain shows on the left. It must name the brain he
        # actually PICKED — this line used to hardcode Gemini, so booting with
        # "sonnet 5" selected put a model name in the footer that was simply
        # not answering him. MODEL TRUTH applies to the chrome too.
        self.emit("talkmodel", self._talk_model_label())
        self.emit("model", _friendly_model_name(
            WORK_BRAINS.get(self.work_model, MODEL_FULL)))
        # This code just booted end to end — it IS the last-good version.
        # Snapshot it so a future bad self-edit always has a way back.
        threading.Thread(target=self_check.snapshot, daemon=True).start()

        # Pre-warm the Claude talk client if that is his talking brain: a
        # cold session measured 5.35s to first word vs 2.17s warm, and that
        # cold turn is always the first thing he says after a restart.
        self._prewarm_voice()
        if TALK_BRAINS.get(self.talk_brain, "gemini") != "gemini":
            async def _warm_talk():
                try:
                    await self._ensure_talk_client(TALK_BRAINS[self.talk_brain])
                except Exception:  # noqa: BLE001 — warmth is a bonus
                    pass
            asyncio.create_task(_warm_talk())

        if POWER_WATCH:
            asyncio.create_task(self._power_watch())

        # Boot briefing (Phase 3, ported from the Node app 2026-07-10):
        # back after 6+ hours away → GOAT speaks first, JARVIS-style.
        if away_h is not None and away_h >= BRIEFING_AFTER_H:
            now = datetime.datetime.now()
            await self._talk(
                "[boot-briefing] Giorgi just started you after about "
                f"{away_h:.0f} hours away. It is {now:%A}, {now:%H:%M}.",
                echo=False)

        retried_text = None  # retry each wall-hit once, so one oversized
        crashes = 0          # message can't ping-pong fresh sessions forever
        last_crash = 0.0
        try:
            while True:
                try:
                    wants_fresh = await self._consume()
                except Exception as e:  # noqa: BLE001 — SELF-HEAL: one SDK/
                    # stream hiccup must not kill the whole night. Reconnect
                    # to the same session and keep going; only give up (and
                    # SAY so) on a genuine crash loop.
                    now = time.monotonic()
                    if now - last_crash > 300:
                        crashes = 0  # last incident is old news — fresh slate
                    last_crash = now
                    crashes += 1
                    if crashes > 3:
                        self.emit("delta", "")
                        self.tts.say("My engine keeps crashing — I need a "
                                     "manual restart, Giorgi.")
                        raise
                    self.emit("status",
                              f"engine hiccup ({type(e).__name__}) — "
                              f"reconnecting {crashes}/3")
                    try:
                        await self.client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                    await asyncio.sleep(crashes)  # 1s, 2s, 3s backoff
                    options.resume = saved_session_id()
                    self.client = ClaudeSDKClient(options)
                    await self.client.connect()
                    self.model = options.model
                    self.busy = False
                    self.suppressed = False
                    self._hold_deltas = False
                    self._delta_buf = ""
                    self.emit("talkmodel", self._talk_model_label())
                    self.emit("status", "reconnected — working brain back")
                    continue
                if not wants_fresh:
                    break  # stream ended cleanly — normal shutdown
                if self._reopen_only and self._effort_dirty:
                    # Thinking dial moved — same session, new effort.
                    self._reopen_only = False
                    self._effort_dirty = False
                    options.effort = self.effort
                    options.resume = saved_session_id()
                    self.client = ClaudeSDKClient(options)
                    await self.client.connect()
                    self.model = options.model
                    self.busy = False
                    self.suppressed = False
                    self.emit("status", f"thinking at {self.effort} — ready")
                    continue
                # Context full: fresh session, retry the wall-hit message.
                await self.client.disconnect()
                options.resume = None
                self.client = ClaudeSDKClient(options)
                await self.client.connect()
                self.model = options.model
                if self._rotate_only:
                    # Proactive rotation, not a wall hit: nothing to retry —
                    # the next work order carries the handoff.
                    self._rotate_only = False
                    retried_text = None
                    self.emit("status", "fresh session — working brain ready")
                    continue
                self.emit("status", "fresh session — working brain ready")
                if self.last_user_text and self.last_user_text != retried_text:
                    retried_text = self.last_user_text
                    # Retry the work order that filled the context.
                    await self._work(self.last_user_text, echo_you=False)
        finally:
            self.shutdown_audio()
            if self.talk_client is not None:
                try:
                    await self.talk_client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            await self.client.disconnect()

    def shutdown_audio(self):
        """Best-effort teardown of everything with an OS handle — safe to
        call from any thread, more than once."""
        try:
            self.audio.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            stt_whisper.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.tts.piper.proc and self.tts.piper.proc.poll() is None:
                self.tts.piper.proc.terminate()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    try:
        asyncio.run(GoatApp().run())
    except KeyboardInterrupt:
        print("\n[goat] stopped.")
