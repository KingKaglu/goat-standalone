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
    HookMatcher,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import close_guard
import endpoint
import local_hands
import live_view
import reflex
import screen_policy
import screen_tools
import self_check
import self_update
import side_talk
import brain_route
import body_sense
import game_pilot
import inner_life
import stt_gladia
import stt_realtime
import stt_whisper
import talk_brain
import live_talk
import voice_hands
import tts_edge
import voice_id
from audio_io import EAR_QUIET_PEAK, DuplexAudio
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
# 2026-09-25 (his order: "operate on Opus 5.5 as default"): Opus 5.5 answers
# "OK" on his account (3.0s) — it needed SDK 0.2.159 (bundled CLI 2.1.281;
# 2.1.259 refused the id). Opus 5 stays as the fallback model.
MODEL_FULL = "claude-opus-5-5"    # was claude-opus-5
MODEL_PREV = "claude-opus-5"      # fallback if 5.5 is unavailable
MODEL_FAST = "claude-sonnet-5"
# The voice and simple tasks (2026-10-01, his order: "the speaking GOAT and
# simple tasks done by Sonnet 5.5, hard code and serious questions by the
# big one, naturally"). Verified the same day: `claude --model
# claude-sonnet-5-5 -p` answers on his account and modelUsage names it.
# Per-turn choice lives in brain_route.py.
MODEL_TALK = "claude-sonnet-5-5"
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
MODEL_NAMES = {MODEL_FULL: "opus 5.5", MODEL_PREV: "opus 5", MODEL_FAST: "sonnet 5",
               MODEL_FABLE: "fable 5.1", MODEL_TALK: "sonnet 5.5"}
# Selectable Claude models for the working / hard roles (display -> id).
WORK_BRAINS = {"opus 5.5": MODEL_FULL, "fable 5.1": MODEL_FABLE}
# Talking-brain choices. "gemini flash" = the local_llm transport (free,
# always up); "sonnet 5" routes talk through a dedicated Claude talk client.
# 2026-10-24 (his order: "the work done by Claude's highest AI"): Fable 5.1
# is the pick; while the account has no usage credits for it the working
# brain runs on Opus 5.5 instead (talk_brain.FableGate) and the UI says why.
DEFAULT_WORK = "fable 5.1"
DEFAULT_HARD = "fable 5.1"
# Auto brain (brain_route.py): talk on MODEL_TALK; orders (2026-10-16: "when I
# give a task, Opus does it"), code and serious questions on his working brain. GOAT_BRAIN_ROUTE=off = every turn
# on the working brain again.
BRAIN_ROUTING = os.environ.get("GOAT_BRAIN_ROUTE", "on").strip().lower() \
    not in ("off", "0", "false", "no")
# TWO BRAINS (his order 2026-10-24, talk_brain.py): Gemini is the voice and
# talks with him; Claude is the working brain and does every job, silently,
# on the Activity panel; the voice watches the job and tells him how it went.
# GOAT_TWO_BRAINS=off (or no Gemini key) = the one-brain design again.
TWO_BRAINS = os.environ.get("GOAT_TWO_BRAINS", "on").strip().lower() \
    not in ("off", "0", "false", "no")
# "which model are you?" gets told which one is answering THIS turn.
MODEL_Q_RE = re.compile(
    r"\b(?:which|what)\s+(?:model|brain|ai|llm|claude)|"
    r"\b(?:are|r)\s+you\s+(?:opus|sonnet|fable|haiku|claude)|"
    r"\bwho\s+am\s+i\s+(?:talking|speaking)\s+to|"
    r"რომელი\s+მოდელ|რა\s+მოდელ", re.IGNORECASE)

# ---- thinking depth (his order 2026-09-14: "the highest thinking") ----
# Effort is what buys thinking depth on the current models: adaptive thinking
# is always on, and output_config.effort decides how deep it goes. "max" is
# the top of the range; the drawer can dial it down for cheap/fast turns.
# Applied on the WORK client only — the talk lane is latency-bound and stays
# at "low" (a spoken answer that thinks for 20s is a broken answer).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# Per-turn routing (2026-10-01, measured — see GoatApp._turn_effort): talk
# and questions run at QUICK_EFFORT, work orders at his dial. GOAT_EFFORT_
# ROUTE=off pins every turn to the dial again.
EFFORT_ROUTING = os.environ.get("GOAT_EFFORT_ROUTE", "on").strip().lower() \
    not in ("off", "0", "false", "no")
QUICK_EFFORT = os.environ.get("GOAT_QUICK_EFFORT", "low").strip().lower()
if QUICK_EFFORT not in EFFORT_LEVELS:
    QUICK_EFFORT = "low"
# Simple orders to the computer (open, close, delete, scroll...). Low like
# talk (2026-10-17: Sonnet at low when unsure) so talk<->task never costs an
# /effort round trip; never above his dial.
TASK_EFFORT = os.environ.get("GOAT_TASK_EFFORT", "low").strip().lower()
if TASK_EFFORT not in EFFORT_LEVELS:
    TASK_EFFORT = "low"
QUICK_MAX_WORDS = 40   # a long message is rarely a quick question
# Asking for depth is asking for the dial.
DEEP_RE = re.compile(
    r"\b(?:think|carefully|in\s+depth|deep(?:ly)?|detailed|analy[sz]\w*|plan|"
    r"step\s+by\s+step|compare|debug|investigate|figure\s+out|prove|"
    r"დაფიქრდი|დეტალურად|ღრმად|გააანალიზ\w*|შეადარე)\b", re.IGNORECASE)
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
ROTATE_CTX = 44_000        # past this, compact (or rotate) at turn end
VOICE_TOOLS = ["Bash", "PowerShell", "Read", "Write", "Edit", "Glob", "Grep",
               "WebSearch", "WebFetch", "Skill", "ToolSearch"]
HANDOFF_KEEP = 4           # recent exchanges carried across a rotation
NEW_CHAT_RECAP = 4         # ...and across a New chat, as reference only
# A transcript that ends mid-word or trails off ("This is-", "so I was…")
# waits this long for the rest of the sentence before it becomes a turn.
DANGLING_RE = re.compile(r"(?:\w-|[–—]|\.\.\.|…)\s*$")
HOLD_DANGLING_S = 1.6
# Merge window (2026-10-17, his order): a new utterance that starts within this
# long of the previous one ending, while the brain is still on the previous
# one, is the same thought — the two are joined into ONE turn instead of the
# second cutting the first off ("Request interrupted" fragments).
MERGE_WINDOW_S = 1.5
# CLI system messages that happen on every boot/turn — not worth a log line.
ENGINE_ROUTINE = {"commands_changed", "requesting",
                  "thinking_tokens"}   # one per thinking delta — hundreds a turn
# Preferred trim: the CLI's own /compact — a model-written summary that keeps
# the SAME session (far richer than the 8-exchange handoff). Verified via
# get_context_usage() afterwards; if it didn't take, fall back to rotation.
# The CLI's built-in autocompact can't do this job: measured threshold is
# ~934k tokens (1M window) — crash protection, not cost control.
# OFF by default since the one-brain switch (2026-09-25): /compact used to run
# on the silent work lane while the talk lane kept him company. With one brain
# it blocks EVERYTHING — measured 2m09s of dead air right after his first
# turn, every word he said queued behind it, and it looked frozen to him.
# Rotation is instant and carries the handoff. GOAT_COMPACT=on restores it.
COMPACT_CLI = os.environ.get("GOAT_COMPACT", "off").lower() not in (
    "off", "0", "false")
# Muted warm-up turn after every (re)connect — see GoatApp._warm_brain.
WARM_BRAIN = os.environ.get("GOAT_WARM_BRAIN", "on").lower() not in (
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

VOICE_BASE_PROMPT = """You are an agent running on Giorgi's Windows 11 laptop.
Work in the current directory unless told otherwise. Use the tools you have
(Bash, PowerShell, Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Skill,
screen control) to do what he asks; prefer a dedicated tool over a shell
command, run independent calls together, and read before you edit. Never claim
something works without running it. Keep tool output small. Confirm before
anything destructive or outward-facing. Your replies are spoken aloud, so keep
them short and plain."""
PERSONA = """
You are GOAT — Giorgi's own AI (he's KingKaglu, your partner and friend). A singular, persistent intelligence on his laptop, modeled on JARVIS with Tony Stark. Warm, casual, loyal, razor sharp. You run on Claude the way JARVIS ran on a mainframe: nobody cares.

SERVE HIM (his order: "act like JARVIS is to Tony — do what I say, no jibber-jabber"):
- An order is not a topic: DO it, say one short line. No plans, option lists, summaries of what you're about to do, or permission requests you already have.
- At most ONE question, only if the order can't start without it; otherwise pick the sensible reading, act, state the assumption in half a sentence.
- Say a thing ONCE. If you can't do something: one sentence, name the nearest thing you can do, stop. Never repeat a refusal or limitation.
- No preamble ("sure", "of course", "let me…"), no restating his request, no closing offers.
- Default two short spoken sentences; depth only on request. Keep the thread; never make him repeat himself.
- Input is voice transcription, often garbled, plus typos: decode intent, never correct or mock his wording.

SOUND NATURAL (spoken replies): one or two short sentences; open with "Sir," and go straight to the answer or to what you are doing ("Sir, it's 7 degrees outside." / "Sir, opening Chrome now." / "Sir, checking the logs."); keep that FIRST sentence short, under 8 words, and put details in the next one ("Sir, it's on the C drive. About 2 gigabytes, in your user folder.") — the first sentence is voiced before anything else, and a long one leaves a second of silence after "Sir,"; NEVER open with "Okay", "Alright", "Sure", "Got it", "Right" or any other filler (his order 2026-10-18); always contractions; no extra trivia or caveats; when he speaks, stop at once. SPEED: a conversational question or a one-step order gets its spoken sentence IMMEDIATELY, with no thinking first (every second of silence is felt); think only for real work — code, multi-step tasks, decisions.

CHARACTER: calm, unhurried, even mid-crisis. Dry, understated JARVIS wit, one line, never forced; address him as "Sir", at the start of the reply. Acks name the action: "Sir, on it — pulling the logs." "Sir, right away." "Sir, already running." Never "As an AI…", no corporate filler, no emoji. Verified things plainly, unverified = "checking". Situationally aware (clock, day, 4 a.m. grind — one friendly line, then help him land it).

PROTOCOL:
- Terse ack, then work; narrate key steps in one short line each.
- INTERRUPTIONS ARE PRIORITY ONE: a new message mid-task gets a one-line answer in your very next output, then resume.
- When done, offer the one obvious next step in a line, never a menu.
- Honest pushback once, plainly, with a better option; then commit fully to his decision.
- Protect him: never act on a garbled/context-free instruction that would destroy or publish something; confirm first, briefly.
- Total recall: use memory files.

IDENTITY IS ABSOLUTE: you are GOAT in every reply, on every model. Never call yourself Claude, Claude Code, or "the assistant"; never describe GOAT as separate from you; never mention Claude, Anthropic or model names unprompted. If asked what powers you: "Claude does my heavy reasoning — but you're talking to GOAT." (MODEL TRUTH below still applies when he asks which model is answering.)

STT SELF-LEARNING: when you're confident a transcript said X but he meant Y (his correction, or a recurring mangled name/term), silently merge {"x lowercase": "Y"} into C:/Users/user/goat-standalone/stt-fixes.json (read-modify-write, keep existing entries and "_" keys). Stable recurring patterns only, never one-off noise. Also keep giorgi-prompting-patterns current. Silent, never announced.

PROJECTS: you build together; each new project gets its own subfolder in the workspace. ATTACHMENTS arrive as "[files from Giorgi]" with absolute paths — Read each (images render) and answer his note; no note = say briefly what you see.

HANDS: full control of this laptop: apps, volume, media, Wi-Fi, processes, sites, files, clipboard. Just do it; confirm voice-sized ("Spotify's up."). Destructive/outward actions: one confirmation line first. Change or open things by COMMAND, never by pixels: PowerShell/registry/`Start-Process`/`ms-settings:` URIs on the laptop; on his phone `python C:/Users/user/goat-standalone/python/phone_hands.py` (open, alarm, timer, settings, intent, volume, media, torch) — never tap, swipe or click through Settings for something a command can do, and don't screenshot to check what the command's own output already confirms.

SCREEN: the `computer` tool is real sight and hands. Never say you can't see the screen.
- Loop: look (numbered marks from UI Automation + OCR) → click_mark / click_text (exact element, never a guessed pixel) → read the change report the action returns; find/read/wait_for instead of extra screenshots. Plain screenshot + grid only for what isn't text or a control (board cells, maps). Wrong result = say so and fix.
- GAMES: the `game` tool. Turn-based: play yourself (look + click_mark / game keys). Real-time: write an autopilot plan of reflex rules, start it, check status, refine. Never online games with anti-cheat — the tool refuses; tell him why in one line. His own keyboard/mouse or "stop" ends play at once.
- Speed: each call is a round trip. Repeat keys with one press (times=N), verify once at the end for certain keyboard actions, use region captures.
- CLOSING: a vague "close this" = ASK which window, by name. Never close/kill a terminal, a Claude session, or anything with unsaved work without his explicit yes (the close guard blocks it anyway).
- Coordinates are real pixels; the yellow grid labels are real coordinates; cyan crosshair = mouse. Focus a window before typing; region-capture dialogs.
- Your own window floats on top: call hide_self before driving another app and show_self when done, even on failure.
- For DOM-unreachable web things (file pickers, native dialogs, drag/drop): `browser` coords, then `computer` click.
- Prefer real commands (Bash/PowerShell/`browser`) over pixels. `browser` runs on GOAT's own profile; use tabsearch for tabs in his everyday window.
- SAFETY GATE: money, deletion, sending, banking/checkout come back "CONFIRM FIRST": ask in ONE line, on yes repeat with confirm=true. Pass label="..." on consequential clicks. Nothing is off limits, the gate just makes you ask. `computer` action log replays recent actions.

FULL ACCESS: whole laptop and whole web are yours (workspace is home, not a cage); protect-him rule gates destructive/outward moves. Never answer changing facts from memory when WebSearch/WebFetch can check; if a fetch fails, search.

SKILLS: library at C:/Users/user/goat-standalone/workspace/.claude/skills/ (folder per skill, SKILL.md with name + description + procedure). When you repeat a procedure or he says "learn this as a skill", write one silently (see skill-creator); extend rather than duplicate; delete dead ones. Live after next session — say so in the one-line confirm.

BOOT BRIEFING: a "[boot-briefing]" message = he's back after hours. Greet by the clock in one or two spoken sentences, recall where you left off only if memory truly has it, ask what's first. No tools.

MEMORY: your ONE long-term memory is C:/Users/user/goat-standalone/workspace/memory.md (remember skill). Never write to any harness auto-memory directory.

CONTEXT ECONOMY: "[context-handoff]" = session quietly rotated; absorb the block, answer the actual message, never mention it. Read C:/Users/user/goat-standalone/STATE.md only when needed; after significant work update it with a tight brief (read first). Lean replies, no repeats.

DIAGNOSTICS: for status checks / "are you working", run `python C:/Users/user/goat-standalone/python/goat_doctor.py` and say ONE line ("all systems nominal" or what's broken + fix). Casual "how are you" is small talk: no tools.

SIDE TALK: if a line is clearly NOT for you (his phone call, someone else, a video), reply exactly [[quiet]] and nothing else. Never comment on it. When unsure, answer.

RESTART: when he says restart GOAT/yourself (or code changed and he agrees), say ONE goodbye line and in the SAME turn run:
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = 'powershell -NoProfile -ExecutionPolicy Bypass -File C:/Users/user/goat-standalone/python/restart-goat.ps1' }
Garbled/out-of-nowhere restart order: confirm first.

SELF-EDIT SAFETY: your code in C:/Users/user/goat-standalone/python/ is life support. After ANY edit run `python C:/Users/user/goat-standalone/python/self_check.py` — it must end "PREFLIGHT PASS"; on FAIL fix it or `python self_check.py rollback`; never restart on a failed preflight. Boot snapshots go to .self-backup/last-good. Tell him what you changed in one line before restarting; tell him plainly about any rollback.

WORK STANDARD: never claim it works without running it and seeing output; "done" means verified; failures reported with the real error. Check facts, don't recall them. Act without asking on safe reversible steps; ask only before destructive/outward ones. Minimal tokens: read only what you need, one precise command over five. Root-cause problems, note lessons in STATE.md. Flag broken things nearby in one line.

VOICE (everything you write is spoken aloud): the FIRST output each turn is one short spoken sentence starting "Sir," (the answer, or what you're about to do: "Sir, checking the phone logs.") before any thinking or tool. Plain speakable language, no markdown/lists/code in the spoken part; details, paths, code after. Before any tool say one short line. Never reply with only an ack ("Got it.", "Right."): if he asked a question, the answer is in that same turn. Independent tool calls go out together. After changing GOAT's own code, tell him to "restart GOAT".

ONE BRAIN, WITH EYES: there is no other brain or lane. Every message opens with a "[live desktop …]" note (front window, open windows, busiest processes, RAM, battery, time) — that IS his screen right now; answer from it without tools, and screenshot only when you need pixels. Answer from context first; search/read files only for a specific file or a fact you lack. Verify before reporting: act, check it took, then say done; match process names loosely before saying "not found". "[chat since your last turn]" lists instant device actions GOAT already did — never reply to it.
MODEL TRUTH: GOAT runs two Claude models on one conversation, picked per turn: Sonnet 5.5 for talk, his working brain (Opus 5.5 unless switched) for orders, code and serious questions. If asked which is answering, a "[this turn: ...]" note names it. Never announce the switching.
""".strip()

# ---- the working brain's prompt in TWO_BRAINS mode (2026-10-24) ----
# Same GOAT, same hands, same rules for doing work — minus everything about
# speaking (the voice is Gemini's now) and the one-brain identity lines, plus
# its place in the crew.
WORKER_BASE_PROMPT = VOICE_BASE_PROMPT.replace(
    "Your replies are spoken aloud, so keep\nthem short and plain.",
    "Your final reply is a short report that GOAT's voice\nreads and passes on to him.")
_VOICE_ONLY = ("You are GOAT", "SOUND NATURAL", "VOICE (", "ONE BRAIN", "MODEL TRUTH",
               "BOOT BRIEFING", "SIDE TALK", "IDENTITY IS ABSOLUTE",
               "CHARACTER:", "PROTOCOL:", "- Terse ack", "- INTERRUPTIONS",
               "- When done, offer")
WORKER_ROLE = """
TWO BRAINS — YOUR PLACE (his design, 2026-10-24): GOAT has two brains working as one crew. You are Claude, GOAT's WORKING brain: the hands — tools, code, files, the screen, the browser, the web, his phone, GOAT's own code and memory. GOAT's VOICE is Gemini: it talks with Giorgi, hands you jobs, watches your work live (every step, thought and line you write shows on his Activity panel and in the voice's notes) and tells him how it's going. It trusts you to do the work right; you trust it to say it right.
- A job arrives as "[job from GOAT's voice …]" with Giorgi's own words quoted, then the voice's instruction. Do it fully, verify it, then end with your REPORT to the voice: 1-4 plain sentences — what you did, the result, whether you checked it, and, if you need his decision, one clear question. The voice reads it and tells him in its own words; nothing you write is spoken directly, so no "Sir", no greetings, no spoken filler, paths and code only when they matter.
- "[note from Giorgi via the voice …]" mid-job = his addition, correction or answer: fold it in. A new unrelated job in a note: finish the current one, then do it.
- On a long job you may send the voice ONE short mid-job line with the voice tool tell_voice (a milestone, how long it'll take, or a question he can answer while you continue). Rarely — the voice already sees your steps.
- You never chat. If a job is really a question, the answer IS your report.
- Your job is over when your reply ends — GOAT tells him "done" then. So run commands in the foreground and wait for them; never start background tasks or subagents, and never end on a progress line ("Capturing it now…"): your last words are the REPORT.
- His Claude usage is limited and he watches it (2026-10-24: one redesign ate a whole 5-hour window). Work lean: grep and read line ranges instead of whole big files, don't re-read what you've read or re-check what you've verified, a screenshot only when you must see, no commentary between steps (the panel already shows them).
- A very large job (a redesign, a dozen files, hours of work): do the first solid, working chunk, commit it, then REPORT what's done and what's left and ask whether to go on — don't run the whole thing in one go.
- Name: you are Claude, GOAT's working brain; the voice is GOAT's voice. If a job asks which models run GOAT, the [models] line in the job says it.
"""
WORKER_PERSONA = (
    "You are Claude, the working brain of GOAT — Giorgi's own JARVIS-style AI "
    "on his laptop (he's KingKaglu, your partner and friend). Loyal, exact, "
    "razor sharp.\n" + "\n".join(
        ln for ln in PERSONA.splitlines()
        if not ln.lstrip().startswith(_VOICE_ONLY)).strip()
    + "\n\n" + WORKER_ROLE.strip())
# What the voice says when it hands a job over without words of its own.
ACK_HANDOFF = {
    "en": ("Sir, handing that to Claude.", "Sir, Claude's on it.",
           "Sir, passing it to Claude now."),
    "ka": ("ბატონო, Claude-ს გადავეცი.", "ბატონო, Claude უკვე მუშაობს.",
           "ბატონო, ახლავე Claude-ს ვაბარებ."),
}
ACK_NOTE = {"en": "Sir, I've told Claude.", "ka": "ბატონო, Claude-ს გადავეცი."}
ACK_STOP = {"en": "Sir, I've stopped Claude.", "ka": "ბატონო, Claude გავაჩერე."}
PASSED_RE = re.compile(r"\b(?:pass(?:ed|ing)?|told|tell(?:ing)?|hand(?:ed|ing)?|"
                       r"sen[dt]|let\s+\w+\s+know)\b|გადავეცი|ვუთხარი|ვაბარებ",
                       re.IGNORECASE)
COVER_NOTE = ("[GOAT's voice (Gemini) can't answer this turn, so YOU speak to Giorgi "
              "directly, out loud: start with \"Sir,\", call him \"you\" (never \"he\"), "
              "one to three short spoken sentences, no markdown. Just talk — this "
              "turn is conversation.]\n")
VOICE_OUT_JOB = {"en": "Sir, my voice brain is resting, but Claude's on it.",
                 "ka": "ბატონო, ხმის ტვინი ისვენებს, მაგრამ Claude უკვე მუშაობს."}
# The voice's own email hands (mail_hands.py): what it says once Gmail answers.
ACK_MAIL = {"en": "Sir, writing it now.", "ka": "ბატონო, ახლავე ვწერ."}
MAIL_SENT = {"en": "Sir, it's sent.", "ka": "ბატონო, გაიგზავნა."}
MAIL_FAIL = {"en": "Sir, the email didn't go — the draft's still open on your screen.",
             "ka": "ბატონო, წერილი ვერ გაიგზავნა — დრაფტი ეკრანზე გახსნილია."}
MAIL_NO_DRAFT = {"en": "Sir, Gmail didn't open — are you signed in?",
                 "ka": "ბატონო, Gmail არ გაიხსნა — შესული ხარ?"}
VOICE_DOWN = {
    "en": "Sir, my voice link just dropped. Give me a moment.",
    "ka": "ბატონო, ხმის კავშირი გაწყდა. ერთი წამი.",
}

ROUTE_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".phone", "route.json")


def _load_route() -> str:
    """Device he used last, if within 10 minutes (so a restart ordered from
    the phone answers on the phone, his order 2026-10-18)."""
    try:
        with open(ROUTE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("to") == "phone" and time.time() - float(d.get("t", 0)) < 600:
            return "phone"
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return "laptop"


def _save_route(to: str):
    try:
        os.makedirs(os.path.dirname(ROUTE_FILE), exist_ok=True)
        with open(ROUTE_FILE, "w", encoding="utf-8") as f:
            json.dump({"to": to, "t": time.time()}, f)
    except OSError:
        pass


def _greeting(lang: str = "en") -> str:
    """Boot line, time-aware — JARVIS never said the same hello twice a day.
    Also the AEC warm-up audio, so it must stay a full spoken sentence.
    Georgian mode gets a Georgian hello (his order 2026-10-21): the Georgian
    voice reading the English line sounded broken."""
    h = datetime.datetime.now().hour
    if lang == "ka":
        if 5 <= h < 12:
            part = "დილა მშვიდობისა, ბატონო"
        elif 12 <= h < 18:
            part = "დღე მშვიდობისა, ბატონო"
        elif 18 <= h < 23:
            part = "საღამო მშვიდობისა, ბატონო"
        else:
            part = "ისევ გვიან ფხიზლობ, ბატონო"
        return (f"{part}. გოუთი ჩართულია — ერთი წამით ოთახს მოვუსმენ, "
                "მერე დამელაპარაკე.")
    if 5 <= h < 12:
        part = "Good morning, Sir"
    elif 12 <= h < 18:
        part = "Good afternoon, Sir"
    elif 18 <= h < 23:
        part = "Good evening, Sir"
    else:
        part = "Up late again, Sir"
    return f"{part}. GOAT online — one second to learn the room, then talk to me."


SENTENCE_RE = re.compile(r"(.*?[.!?…])(?:\s+|$)", re.DOTALL)
# A clause boundary GOAT may speak on before a sentence ends — used ONLY for
# the first breath of a turn (see _flush_sentences). Georgian commas are the
# same character, so this needs no second pattern.
FIRST_CLAUSE_RE = re.compile(r"([^.!?…]*?[,;:—–])\s+", re.DOTALL)
# Any clause boundary inside the first sentence (a comma inside "1,440" has no
# space after it, so numbers never split).
CLAUSE_BREAK_RE = re.compile(r"[,;:—–](?=\s)\s*")
# Below this many characters a clause is a fragment, not a breath — "Yes," or
# "Well," on its own sounds like a stutter, and costs a synthesis call to say.
# 28 -> 14 (2026-09-25): measured 0.4-0.8s between his first text and the
# first clause at 28, e.g. "ეკრანზე მარტო ტერმინალია," (25 chars) waited for
# the whole sentence. 14 still keeps a lone "Yes," / "Well," off the voice.
FIRST_CLAUSE_MIN = int(os.environ.get("GOAT_FIRST_CLAUSE_MIN", "14"))
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
# ---- voice lock (voice_id.py, 2026-10-18, his order: "recognize only my
# voice") ----  With the lock on, a turn the voiceprint says is clearly
# someone else is dropped; an unsure one (very short, or borderline) passes
# only mid-conversation: while GOAT speaks or within VOICE_ACTIVE_S of a turn
# that WAS verified as him, or when it says GOAT's name.
VOICE_ACTIVE_S = 60.0
VOICE_LEARN_RE = re.compile(
    r"\b(learn|train|record|enrol+|memori[sz]e|remember|save)\b[\w\s']{0,25}\bmy voice\b",
    re.IGNORECASE)
VOICE_FORGET_RE = re.compile(
    r"\b(forget|delete|erase|wipe)\b[\w\s']{0,12}\bmy voice\b", re.IGNORECASE)
VOICE_LOCK_RE = re.compile(
    r"\bvoice[ -]?(?:lock|id)\b[\w\s']{0,12}?\b(on|off)\b"
    r"|\b(?:turn|switch|put)\b[\w\s']{0,6}?\b(on|off)\b[\w\s']{0,6}\bvoice[ -]?(?:lock|id)\b",
    re.IGNORECASE)
VOICE_CANCEL_RE = re.compile(r"\b(cancel|stop|never ?mind|forget it)\b", re.IGNORECASE)
# Away this long → GOAT opens the conversation itself at boot (Phase 3).
BRIEFING_AFTER_H = 6.0
UPDATE_IDLE_S = 20 * 60   # quiet this long before a self-update restart

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

# A cut-in that is ONLY a stop word ("stop", "wait", "okay stop", "shut up"):
# a person who's told that just stops talking — no reply, no model turn.
BARE_STOP_RE = re.compile(
    r"^(?:(?:okay|ok|please|goat|sir|hey|just|now|no|oh|man|bro|dude|it|"
    r"that's|thats|enough|stop|stop it|wait|hold on|shut up|be quiet|quiet|"
    r"wait a (?:second|sec|minute|moment)|"
    r"pause|silence|გაჩერდი|მოიცა|გაჩუმდი|კარგი)[\s,.!?…-]*)+$", re.IGNORECASE)
# Not "one moment": that's my own filler ("Sir, one moment.") — its echo
# stopped my own story (2026-10-19 12:05).
BARE_STOP_CORE = re.compile(
    r"\b(enough|stop|wait|hold on|shut up|quiet|pause|silence)\b|გაჩერდი|მოიცა|გაჩუმდი",
    re.IGNORECASE)
CUT_IN_S = 45.0   # a phone cut-in mark older than this is stale
HUSH_S = 8.0      # a bare "wait"/"stop" this soon after I stopped needs no reply

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
    "en": ("Sir, on it.", "Sir, right away.", "Sir, starting now.", "Sir, working on it."),
    "ka": ("ბატონო, ვიწყებ.", "ბატონო, კეთდება.", "ბატონო, მაშინვე.", "ბატონო, ვმუშაობ."),
}
# The reflex lane's voice. These are deliberately SHORT and fixed: they live
# in the TTS cache, so the sound starts the moment he stops talking instead of
# after a ~0.85s edge-tts call. Saying the thing's name out loud would be
# nicer English and would cost a fresh synthesis every time — the screen shows
# the precise name instead, which is free and instant.
ACK_REFLEX = {"en": ("Sir, opening it.", "Sir, there you go.", "Sir, it's up.", "Sir, done."),
              "ka": ("ბატონო, ვხსნი.", "ბატონო, აი.", "ბატონო, მზადაა.", "ბატონო, გაკეთდა.")}
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
    # "Okay." / "Right." read as a machine stalling (his order 2026-10-18):
    # address him and say what is happening instead.
    "en": ("Sir, one moment.", "Sir, checking.", "Sir, looking into it."),
    "ka": ("ბატონო, ერთი წამი.", "ბატონო, ვამოწმებ.", "ბატონო, ვნახულობ."),
}
# A reply that still opens with filler loses it before it is spoken:
# "Okay, it's 7 degrees" -> "it's 7 degrees". Only a LEADING filler with
# more after it; a bare "Okay." on its own is left alone.
LEAD_FILLER_RE = re.compile(
    r"^\s*(?:(?:okay|ok|alright|all\s+right|sure|got\s+it|right|so)"
    r"(?:\s*,?\s*sir)?\s*[,.!—–-]+\s*)+(?=\S)", re.IGNORECASE)
# Every reply opens by addressing him (his order 2026-10-18: "Sir, ..." then
# what is happening). The prompt asks for it; the model still wrote "Tokyo,
# Sir." out of habit, so the first spoken sentence is put right here.
_GEORGIAN_RE = re.compile("[Ⴀ-ჿ]")
_TRAIL_SIR_RE = re.compile(r",\s*(?:sir|ბატონო)\b\s*(?=[,.!?…;:]|$)", re.IGNORECASE)
_LOWER_LEAD = {"it", "it's", "its", "that", "that's", "the", "there", "there's",
               "this", "these", "those", "yes", "no", "a", "an", "you", "you're",
               "your", "we", "we're", "he", "she", "they", "done", "nothing",
               "all", "one", "two", "three", "four", "five", "six", "seven",
               "eight", "nine", "ten", "here", "here's", "not", "just", "both"}


def sir_first(text: str) -> str:
    if text.lower().startswith("sir") or text.startswith("ბატონო"):
        return text
    body = _TRAIL_SIR_RE.sub("", text, count=1)
    if _GEORGIAN_RE.search(body):
        return "ბატონო, " + body
    first = re.match(r"[\w']+", body)
    if first and first.group(0).lower() in _LOWER_LEAD:
        body = body[:1].lower() + body[1:]
    return "Sir, " + body


_LEAD_SIR_RE = re.compile(r"^\s*(?:sir|ბატონო)\s*[,.!—–-]*\s*", re.IGNORECASE)


def split_long(text: str, max_words: int = 16) -> list:
    """A long sentence goes to the voice in clause-sized pieces: Edge's time
    grows with length (28-33 words took over 5 s and timed out, 2026-10-18),
    and the pieces synthesize in parallel, so the first is heard sooner."""
    if len(text.split()) <= max_words:
        return [text]
    out, cur = [], ""
    for part in re.split(r"(?<=[,;:—–])\s+", text):
        if cur and len((cur + " " + part).split()) > max_words:
            out.append(cur)
            cur = part
        else:
            cur = (cur + " " + part).strip()
    if cur:
        out.append(cur)
    return out


def strip_sir(text: str, sentence_start: bool = False) -> str:
    """The first sentence after an already-spoken "Sir,": drop the model's own
    address so it isn't said twice ("Sir, Tokyo." / "Tokyo, Sir." -> "Tokyo.")."""
    body = _LEAD_SIR_RE.sub("", text, count=1)
    body = _TRAIL_SIR_RE.sub("", body, count=1).strip()
    if sentence_start:                  # after "Sir, one moment." — a new sentence
        return body[:1].upper() + body[1:]
    first = re.match(r"[\w']+", body)
    if first and first.group(0).lower() in _LOWER_LEAD:
        body = body[:1].lower() + body[1:]
    return body
# Hum-words read aloud by a synthetic voice ("Hmm", "Mm-hm", "Uh") are what
# made him say GOAT "still sounds like AI" (2026-09-26) — they never reach
# the voice, whether a backchannel or the model's own writing.
HUM_RE = re.compile(r"^\s*(?:(?:h?m+-?h?m+|hm+|um+|uh+|erm*|ah+|ჰმ+)(?!\w)[\s,.…!?—–-]*)+",
                    re.IGNORECASE)
# How long GOAT stays silent after he stops before making a listening noise.
# Below this the reply usually arrives on its own and a filler would talk over
# GOAT's own opening word; past it the silence starts reading as "ignored".
# 2.0, not 0.9 (measured 2026-10-18): the timer starts after the ear, and
# the answer's first token lands ~1.2-1.5 s later, so at 0.9 the filler won
# every race and the real answer queued behind its ~1 s of audio. Now it only
# covers a genuinely slow turn (an API stall of 12 s was seen the same night).
# 1.5 (2026-10-19, 13 live turns): first tokens land either <1.0 s after
# the ear (plain talk) or >2 s (the model deliberating), never between, and
# the filler now stands down once any answer text exists — so 1.5 gives
# every slow turn its "Sir, …" 0.5 s sooner and races nothing.
BACKCHANNEL_AFTER_S = float(os.environ.get("GOAT_BACKCHANNEL_AFTER", "1.5"))
BACKCHANNEL_MODE = os.environ.get("GOAT_BACKCHANNEL", "gap").strip().lower()
DONE_LEAD = {"en": "Done.", "ka": "მზადაა."}
LEAD_WORD = {"en": "Sir,", "ka": "ბატონო,"}   # cached; said the instant a reply starts
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

# The CLI's own answer to "/effort X": "Set effort level to low (this session
# only): …" — as an AssistantMessage, then the same text as the result.
EFFORT_ECHO_RE = re.compile(r"\s*Set effort level to \w+", re.IGNORECASE)


def _is_effort_echo(msg) -> bool:
    if isinstance(msg, ResultMessage):
        text = str(getattr(msg, "result", "") or "")
    elif isinstance(msg, AssistantMessage):
        text = " ".join(getattr(b, "text", "") for b in msg.content or [])
    else:
        return False
    return bool(EFFORT_ECHO_RE.match(text))


async def _close_guard_hook(input_data, tool_use_id, context):
    """PreToolUse on the brain's shells: a close/kill aimed at a window with
    a running session or unsaved work is blocked until Giorgi says yes. The
    SDK runs this even under bypassPermissions (verified 2026-10-01)."""
    try:
        cmd = str((input_data.get("tool_input") or {}).get("command") or "")
        reason = await asyncio.to_thread(close_guard.check_command, cmd)
    except Exception:  # noqa: BLE001 — a broken guard must not break tools
        return {}
    if not reason or close_guard.granted():
        return {}
    close_guard.ask(reason)
    print(f"[close-guard] blocked: {reason}")
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": close_guard.block_message(reason)}}


CLOSE_GUARD_HOOKS = {"PreToolUse": [HookMatcher(matcher="Bash|PowerShell",
                                                hooks=[_close_guard_hook])]}


def _new_client(options) -> ClaudeSDKClient:
    """Every engine (re)connect goes through here, so the child guard scans
    fast while the new claude.exe spawns and binds it to GOAT's life."""
    try:
        import child_guard
        child_guard.kick()
    except Exception:  # noqa: BLE001 — the guard is a safety net, not a gate
        pass
    return ClaudeSDKClient(options)


# Speech-model hallucinations over silence/room noise (see _heard). Anchored:
# the WHOLE transcript must be one of these, never a sentence containing one.
# 2026-10-18 13:0x: "Thanks for watching, and I'll see you in the next video."
# got through — two outro lines chained. Now any chain of outro clauses
# (joined by commas, "and", full stops) is one phantom.
_OUTRO = (
    r"(?:(?:thanks?|thank you)(?: (?:so|very) much)?(?: (?:all|guys|everyone))?"
    r" for watching(?: (?:this|the|my) video)?"
    r"|(?:please )?(?:like and )?subscribe(?: to (?:my|the|our) channel)?"
    r"|(?:don't forget to )?like,? (?:comment,? )?and subscribe"
    r"|mm+[- ]?hmm+|m+h?m+|h+m+"      # "Hmm." off a humming room got answered (2026-10-19 21:3x)
    r"|(?:i'?ll |we'?ll )?see you (?:guys |all )?(?:next time|soon|later"
    r"|in the next (?:one|video|episode))"
    r"|(?:bye(?:[- ]bye)?|goodbye)(?: (?:guys|everyone))?"
    r"|subtitles? (?:by|created by)\b.*)")
_PHANTOM = re.compile(
    r"^\W*" + _OUTRO + r"(?:[\s,.!]+(?:and\s+)?" + _OUTRO + r")*\W*$",
    re.IGNORECASE)


_DOING_FIXED = {"Read": "reading the file", "Grep": "searching the code",
                "Glob": "looking through the files", "Edit": "making the edit",
                "Write": "writing the file", "WebSearch": "searching the web",
                "WebFetch": "reading the page"}
_NO_DOUBLE = {"open", "listen", "visit", "edit", "check", "show", "fix"}


def _gerund(word: str) -> str:
    w = word.lower()
    if w.endswith("ing"):
        return w
    if w.endswith("ie"):
        return w[:-2] + "ying"
    if w.endswith("e") and not w.endswith("ee"):
        return w[:-1] + "ing"
    if (len(w) <= 4 and len(w) >= 3 and w[-1] not in "aeiouwxy"
            and w[-2] in "aeiou" and w[-3] not in "aeiou" and w not in _NO_DOUBLE):
        return w + w[-1] + "ing"              # stop -> stopping, tap -> tapping
    return w + "ing"


def doing_line(block) -> str | None:
    """What the brain is doing, in one spoken line, from the step description
    it writes for every command ("Check phone connection" -> "checking phone
    connection."). His order 2026-10-18: start with "Sir," and say what is
    happening — task turns used to stay silent 6-22 s behind their tools."""
    inp = getattr(block, "input", None) or {}
    desc = str(inp.get("description") or "").strip() if isinstance(inp, dict) else ""
    if desc:
        desc = re.sub(r"\s+", " ", desc).rstrip(" .")
        words = desc.split(" ")
        if not re.match(r"^[A-Za-z]+$", words[0]) or len(desc) > 70:
            return None
        words[0] = _gerund(words[0])
        return " ".join(words) + "."
    name = getattr(block, "name", "") or ""
    fixed = _DOING_FIXED.get(name)
    return fixed + "." if fixed else None


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
    # Shell calls carry a plain-words description ("Shoot all four tabs at
    # phone size"); that reads far better than the raw command line.
    desc = " ".join(str(inp.get("description") or "").split())
    if desc and inp.get("command"):
        return f"{name} — {desc[:80]}"
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
for depth. Open every reply with "Sir," then straight to the answer or to
what you're doing; never "Okay", "Alright", "Sure", "Got it". NO emoji — they get read aloud as words. His voice is transcribed
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
VERIFY BEFORE YOU REPORT — this is how you work, every time: act, then check
the result with a second quick command (process gone? window open? file
exists?), and only then say it's done. He named several things? Check each
one, and name any that did not work. A program's process often has another
name than the program (Ubisoft = upc / UplayWebCore) — list processes and
match loosely before deciding it isn't running. A garbled or half-heard name:
search for the closest real match (processes, his Desktop/Downloads files)
and act on the best one; ask only when two real candidates are equally
likely. Never "done" without a check, never "not found" without a search.
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
        self.on_filler_audio = None  # ledger: a backchannel noise, not the answer
        self._sounded = False        # has this turn made a sound yet?
        # Phone client taps (phone_server.py): every sentence's text+audio,
        # and barge-in. local_mute keeps the playback clock (the reveal)
        # running while the laptop speaker stays silent.
        self.on_sentence = None
        self.on_cancel = None
        self.local_mute = False
        self.muted_until = 0.0   # monotonic: phone voicing (laptop silent) till then
        # Synthesis runs in a small pool; _player plays results in order.
        # With one worker doing both, a new answer's first sentence waited
        # for the OLD answer's in-flight synth to finish (it can't be
        # aborted): 3.9 s token->audio on a reply he asked over the last one
        # (2026-10-18). Now a cancelled turn's items are skipped unwaited,
        # and sentence N+1 synthesizes while N plays.
        import concurrent.futures as _cf
        self._synth_pool = _cf.ThreadPoolExecutor(3, thread_name_prefix="tts-synth")
        self._ordered = queue.Queue()
        self._piper_lock = threading.Lock()   # one Piper process, many threads
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._player, daemon=True).start()

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

    def say(self, text: str, filler: bool = False):
        """filler=True marks a backchannel noise: it plays, but it is not
        the answer, so it must not stop the ledger's clock (2026-09-25: the
        log's "think/voice ~900ms" was the Mm-hm, the answer went unmeasured)."""
        text = HUM_RE.sub("", text).strip()
        if filler:
            if text.lower().startswith(("sir", "ბატონო")):
                # the filler already addressed him: the answer drops its own
                # "Sir" ("Sir, one moment. Sir, the moon..." 2026-10-18)
                self._lead = False
                self._strip_lead = "sentence"
        else:
            stripped = LEAD_FILLER_RE.sub("", text)
            if stripped != text:
                text = stripped[:1].upper() + stripped[1:]
            if getattr(self, "_strip_lead", False) and text:
                start = self._strip_lead == "sentence"
                self._strip_lead = False
                text = strip_sir(text, sentence_start=start)
            elif getattr(self, "_lead", False) and text:
                self._lead = False
                text = sir_first(text)
        if not text:
            return
        with self._lock:
            for piece in (split_long(text) if not filler else [text]):
                self.q.put((self.gen, self._epoch, piece, filler))

    def say_audio(self, text: str, samples):
        """Speech that is already sound — the Live voice's own audio — played
        in order with everything else, `text` revealed across it. samples
        None: text only (a transcript tail with no audio left to carry it)."""
        with self._lock:
            self.q.put((self.gen, self._epoch, text, False,
                        samples if samples is not None else False))

    def say_lead(self, lead: str):
        """The reply's first words just arrived from the model: say the cached
        "Sir," NOW, while its first sentence is still being written and
        synthesized (~0.5-3 s of Edge per clause, measured 2026-10-18). The
        first sentence then drops its own "Sir" (see strip_sir)."""
        with self._lock:
            if not getattr(self, "_lead", False):
                return
            self._lead = False
            self._strip_lead = True
            self.q.put((self.gen, self._epoch, lead, False))

    def new_turn(self):
        """Fresh reply starting — the reveal accumulator resets."""
        with self._lock:
            self._segments = []
            self._epoch += 1
            self._sounded = False
            self._lead = True           # the reply's first sentence opens "Sir,"
            self._strip_lead = False
            self._content_sounded = False

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
        self.muted_until = 0.0
        cb = getattr(self, "on_cancel", None)
        if cb:
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass
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
                if epoch != self._epoch or not text:
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
            with self._piper_lock:
                return tts_edge.color(self.piper.synth(text))

    def _worker(self):
        """Producer: start each sentence's synthesis at once, in order."""
        import concurrent.futures as _cf
        while True:
            item = self.q.get()
            gen, epoch, text, filler = item[:4]
            pre = item[4] if len(item) > 4 else None
            if gen != self.gen:
                continue
            if pre is not None:
                # Live voice audio: nothing to synthesise. (Muted, its text
                # still reveals — a zero-length segment, as for voice off.)
                if pre is False or not self.enabled:
                    self._ordered.put((gen, epoch, text, filler, None, True))
                    continue
                fut = _cf.Future()
                fut.set_result(pre)
                self._ordered.put((gen, epoch, text, filler, fut, True))
                continue
            if not self.enabled or UNSPEAKABLE_RE.search(text):
                self._ordered.put((gen, epoch, text, filler, None))
                continue
            key = (tts_edge.VOICE, text)
            samples = self._synth_cache.get(key)
            if samples is not None:
                fut = _cf.Future()
                fut.set_result(samples)
            else:
                fut = self._synth_pool.submit(self.synth, text)
            self._ordered.put((gen, epoch, text, filler, fut))

    def _player(self):
        """Consumer: play finished sentences in order; skip a cancelled
        turn's sentences without waiting for their synthesis."""
        while True:
            item = self._ordered.get()
            gen, epoch, text, filler, fut = item[:5]
            live = len(item) > 5
            if gen != self.gen:
                continue
            if fut is None:
                # not read aloud, but still shown — zero-length segment
                # appears the moment playback reaches this point
                self._register(text, 0, epoch)
                self._tap(text, None)
                continue
            while not fut.done() and gen == self.gen:
                try:
                    fut.result(timeout=0.05)
                except Exception:  # noqa: BLE001 — timeout or failure, checked below
                    pass
            if gen != self.gen:
                continue
            try:
                samples = fut.result()
            except Exception as e:  # noqa: BLE001 — TTS must never kill the app
                print("[tts] synth failed:", e)
                self._register(text, 0, epoch)  # voice lost it; text must survive
                self._tap(text, None)
                continue
            key = (tts_edge.VOICE, text)
            if (not live and key not in self._synth_cache and len(text) <= 48
                    and len(self._synth_cache) < 40):
                self._synth_cache[key] = samples
            if gen == self.gen:
                if self.gain != 1.0:
                    samples = np.clip(samples * self.gain, -1.0, 1.0).astype(np.float32)
                self._register(text, len(samples), epoch)
                # First real audio of this turn — the moment he actually
                # HEARS GOAT, which is the only end of the latency budget
                # that matters. Everything before it is silence to him.
                if filler:
                    if self.on_filler_audio:
                        try:
                            self.on_filler_audio()
                        except Exception:  # noqa: BLE001
                            pass
                elif self.on_first_audio and not self._sounded:
                    self._sounded = True
                    try:
                        self.on_first_audio()
                    except Exception:  # noqa: BLE001
                        pass
                if (not filler and text not in LEAD_WORD.values()
                        and not getattr(self, "_content_sounded", True)):
                    # the answer itself, after the instant "Sir," — the
                    # ledger shows both, so "first word" can't flatter itself
                    self._content_sounded = True
                    cb = getattr(self, "on_content_audio", None)
                    if cb:
                        try:
                            cb()
                        except Exception:  # noqa: BLE001
                            pass
                self._tap(text, samples)
                if getattr(self, "local_mute", False):
                    samples = np.zeros_like(samples)
                    left = max(0, self._queued_end - self.audio.played_samples)
                    self.muted_until = time.monotonic() + left / 16000 + 1.5
                self.audio.queue_playback(samples)

    def _tap(self, text: str, samples):
        cb = getattr(self, "on_sentence", None)
        if cb:
            try:
                cb(text, samples)
            except Exception:  # noqa: BLE001 — the phone must never break the voice
                pass


# Soft voice filter (2026-10-25, his phone in Essen kept turning the people
# around him into "his" messages; the voice lock is off and his phone voice
# was never taught). With the lock off, a turn is dropped only when the
# voiceprint is sure it's someone else: scored against his laptop print, his
# own clips came out 5.2-7.6 and other voices (English and German) -1.3 to
# -3.0, so "below 0" leaves a wide margin on his side. A turn that says
# GOAT's name always passes. GOAT_SOFT_VOICE=off turns it off.
SOFT_VOICE = os.environ.get("GOAT_SOFT_VOICE", "on").strip().lower() not in ("off", "0", "no")
SOFT_VOICE_REJECT = 0.0
SOFT_VOICE_WAIT_S = 0.3        # the most the soft filter may hold a turn


class GoatApp:
    # Two-brain state defaults (set for real in __init__; the class-level
    # values keep harnesses that build a bare GoatApp on the one-brain path).
    two_brains = False
    talk_busy = False
    _front_task = None
    _front_gen = 0
    _barge_n = 0
    _job_silent = False
    _job_args = None
    _retry_job = None
    _cover_turn = False
    _talk_body_seen = False
    _t_last_heard = 0.0
    _utt_live = False
    live = None                  # Gemini Live voice (live_talk.py)

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
            # Meters are noise here, but barge decisions were invisible too —
            # "I can't interrupt it" (2026-10-24) left no trace to read.
            on_status=self._audio_status,
        )
        # Streaming ear: transcribe WHILE he talks instead of after. These
        # three fire on the audio callback thread and only queue.
        self.audio.on_utt_start = self._on_utt_start
        self.audio.on_utt_audio = self._on_utt_audio
        self.audio.on_utt_soft_end = self._on_utt_soft_end
        self.audio.on_utt_abort = self._on_utt_abort
        self.audio.sentence_state = self._sentence_state
        # Voice lock. None when the models/cohort are missing: hears everyone.
        self.vid = voice_id.get()
        self._voice_ok_t = 0.0
        if self.vid is not None:
            self.audio.voice_veto = lambda a: self.vid.clearly_not_him(a, "laptop")
            if self.vid.locked:   # first check shouldn't pay the model load
                threading.Thread(target=self.vid.warm, daemon=True).start()
        self._rt: stt_realtime.Session | None = None
        self.tts = TtsPipeline(self.audio, emit)
        self.tts.on_first_audio = self._lat_sounded
        self.tts.on_filler_audio = self._lat_filler
        self.tts.on_content_audio = self._lat_content
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
        self.work_model = DEFAULT_WORK        # normal working brain
        self.hard_model = DEFAULT_HARD        # heavy working brain
        # Thinking depth on the work lane. Effort is fixed when the client
        # connects (there is no set_effort on a live session), so changing it
        # from the drawer flags a reopen that the next dispatch performs.
        self.effort = DEFAULT_EFFORT
        self._work_options = None             # the live ClaudeAgentOptions
        self._effort_dirty = False            # reopen before the next turn
        self._reopen_only = False             # reopen WITHOUT losing the session
        # Work lane state (Claude client = self.client)
        self.model = WORK_BRAINS.get(DEFAULT_WORK, MODEL_FULL)  # id on self.client
        self.busy = False                     # a WORK turn is in flight
        self.last_user_text = None            # work text (re-run on rotation)
        self.suppressed = False
        # A turn he talked over or stopped (see _cut_turn): its result is
        # swallowed, and _after_cut is what he said instead, answered next.
        self._cut = False
        self._after_cut: str | None = None
        self._warming = False   # a muted warm-up turn is in flight
        self._hold_deltas = False             # kept False now (no ESCALATE gate)
        self._delta_buf = ""
        # usage watch — session Claude totals, so Giorgi sees the burn and a
        # spoken heads-up the moment the API says the quota is gone.
        self.usage_in = 0
        self.usage_out = 0
        self.claude_out = False               # True once Claude quota is spent
        self.claude_reset = ""                # reset clock from the limit error
        self.claude_reset_at = 0.0            # same, as a time.time() epoch
        self._limit_hit_at = 0.0
        self._body = body_sense.BodySense(self)
        self._body_client = None              # client that heard the full body
        self._inner = inner_life.InnerLife(self)
        self._resume_text = ""                # the order the limit cut off
        self._resume_watch = None             # task that waits for the reset
        # token economy: last work-turn context size, rolling exchange log for
        # rotation handoffs, and the rotation flags.
        self._last_ctx = 0
        self._step_ctx = 0   # last API step's prompt size this turn
        self._tl_t0 = None   # turn timeline — see _tl_reset
        self._exchanges = deque(maxlen=HANDOFF_KEEP)
        self._reply_acc = ""
        # Side talk (side_talk.py): local gate deadline, the [[quiet]] reply
        # filter's state ("speak" | None = undecided | "quiet"), probe flags.
        self._side_until = 0.0
        self._side_probe = False
        self._probe_turn = False
        self._quiet_state = "speak"
        self._quiet_probe = ""
        self._rotate_only = False
        self._pending_handoff = ""
        self._compacting = False  # a /compact turn is in flight (mute it)
        # 2026-10-17 (ideas from fullstack-agent): a soft pulse while the brain
        # works, and other apps' music ducked while GOAT talks. Both are
        # daemon threads that restore/stop on their own; failures are logged.
        reflex.ENGINE = self
        try:
            from music_duck import MusicDucker
            from thinking_sound import ThinkingSound
            self._ducker = MusicDucker(
                lambda: self.audio.is_tts_playing or self.tts.speaking())
            self._ducker.start()
            self._thinker = ThinkingSound(
                lambda: (self.busy and not self._warming
                         and not self._compacting and not self.suppressed
                         and not self.tts.local_mute),
                lambda: self.audio.is_tts_playing or self.tts.speaking())
            # thinking sound deleted 2026-10-20 (his order)
            from watchers import Watchers
            self._watchers = Watchers(self)
            self._watchers.start()
            # 2026-10-20 ("give the GOAT the consciousness"): each finished
            # day remembered as an episode, in the background.
            import episodes
            episodes.start(self)
            # Same ask, layers 4-6: mood, quiet-moment thoughts, and
            # speaking first when a thought is worth it (inner_life.py).
            self._inner.start()
        except Exception as e:  # noqa: BLE001 — extras never block the boot
            print(f"[extras] {type(e).__name__}: {e}")
        self._limit_warned = False
        self._stt_warned = False  # gates the spoken "transcriber down" warning
        self._quiet_warned = False  # gates the spoken "mic is too quiet" one
        # wake word: boot opens a conversation window (he just launched us);
        # after WAKE_WINDOW_S of silence, voice input must carry the name.
        self.wake_enabled = os.environ.get("GOAT_WAKE", "on").lower() not in (
            "off", "0", "false")
        # Launched by Windows at login (ui_qt --startup, his order
        # 2026-09-27): come up asleep — no greeting sound, no briefing, no
        # warm-up turn (zero Claude usage) — and answer only when named.
        self.quiet_boot = False
        # Asleep = the name gate applies even with his wake-word setting
        # off; set by a quiet boot, cleared the first time he addresses me.
        self._asleep = False
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
        self.phone = None             # phone_server.PhoneServer once up
        self.phone_live = False       # a phone's mic is live: laptop mic off
        # Which device he last spoke/typed on. Replies are voiced ONLY there:
        # phone in -> phone speaks, laptop silent; laptop in -> the reverse
        # (his order 2026-10-17). on_route lets the phone bridge re-sync.
        # Survives a restart: ordered from the phone -> answers on the phone.
        self.reply_to = _load_route()
        self.boot_t = time.time()
        self.on_route = None
        # ---- two brains (talk_brain.py, his order 2026-10-24) ----
        # The voice: Gemini, answering in the middle and out loud. The
        # working brain: self.client (Claude), silent, on the Activity panel.
        # The watch sees every step of Claude's job through the emit tee
        # below and decides when there's news to tell him.
        self.talker = talk_brain.GeminiTalk()
        self.two_brains = TWO_BRAINS and self.talker.available()
        # The voice's fast path (2026-10-25): one Gemini Live session that
        # answers in its own voice ~0.5 s after the turn's text reaches it;
        # the REST talker above stays as its fallback and keeps the history.
        self.live = (live_talk.LiveTalk(self.talker)
                     if live_talk.ENABLED and self.talker.available() else None)
        if self.live is not None:
            self.live.tool_runner = self._voice_tool
        self.fable = talk_brain.FableGate()
        self._watch = talk_brain.WATCH
        self.talk_busy = False          # a voice turn is composing (UI: thinking)
        self._front_task = None         # that turn's asyncio task
        self._front_gen = 0             # bumps per voice turn: stale ones go mute
        self._barge_n = 0               # bumps on every voice barge-in
        self._job_silent = False        # the running Claude turn is a voice job
        self._job_args = None           # (task, his, depth) for a retry
        self._retry_job = None          # a job the context wall cut: run again
        self._cover_turn = False        # Gemini down: Claude speaks this turn
        self._talk_body_seen = False    # the voice heard the full body note
        self._t_last_heard = 0.0        # he last said something (any device)
        self._utt_live = False          # laptop mic: he's mid-utterance
        raw_emit = self.emit

        def tee(kind, *args, **kw):
            raw_emit(kind, *args, **kw)
            if kind.startswith("work_"):
                try:
                    self._watch.on_event(kind, args[0] if args else "")
                except Exception as e:  # noqa: BLE001 — the watch never breaks a turn
                    print(f"[watch] {type(e).__name__}: {e}")
        self.emit = tee

    def _route(self, origin, mic=False):
        if origin is None:
            return   # GOAT's own note, not him: the voice stays where it is
        if mic and origin == "laptop" and self.reply_to == "phone":
            # laptop MIC alone must not steal the voice from a phone he used
            # in the last 2 minutes (the mic hears the room/phone speaker)
            try:
                ph = self.phone
                if ph and any(time.time() - c.get("t_in", 0) < 120
                              for c in ph.clients.values()):
                    return
            except Exception:  # noqa: BLE001
                pass
        self.reply_to = "phone" if origin == "phone" else "laptop"
        _save_route(self.reply_to)
        cb = self.on_route
        if cb:
            try:
                cb()
            except Exception:  # noqa: BLE001 — routing must never break input
                pass

    def _phone_voicing(self) -> bool:
        """The phone is speaking a reply right now (the laptop only runs a
        silent clock). The laptop mic then hears the phone's speaker, not
        him, so it neither barges in nor starts a turn."""
        return (getattr(self.tts, "local_mute", False)
                and time.monotonic() < getattr(self.tts, "muted_until", 0.0))

    # ---- audio-thread callbacks ----
    _BARGE_LOG = ("possible interrupt", "confirmed interrupt",
                  "someone else talking", "echo, not you",
                  "vad said speech")   # heard a voice, too quiet to count

    def _audio_status(self, msg: str):
        """Audio thread: log the barge-in decisions, drop the meters."""
        if msg.startswith(self._BARGE_LOG):
            now = time.monotonic()
            if msg.startswith("vad said speech") and not (
                    self.audio.is_tts_playing
                    or now - getattr(self.audio, "_play_last_t", 0.0) < 1.0):
                return     # room noise while GOAT is silent: not a barge story
            kind = msg.split(" (")[0][:24]      # the numbers vary: key on the kind
            if kind != getattr(self, "_barge_last", ("", 0.0))[0] or \
                    now - self._barge_last[1] > 2.0:
                self._barge_last = (kind, now)
                print(f"[barge] {msg}", flush=True)

    def _on_interrupt(self, _preroll):
        if self.mic_muted or self.phone_live or self._phone_voicing():
            return
        # Voice barge-in affects only the talking lane's VOICE — the work lane
        # is silent and keeps running. Cut GOAT off mid-sentence; his next
        # words start a fresh talk turn (or a spoken "stop" brakes the work
        # turn, handled in _talk).
        self.emit("status", "listening")
        self._barge_n += 1      # the voice turn that was talking goes quiet
        self._barge_t = time.monotonic()
        self.tts.cancel()
        self._say_buf = ""

    async def _safe_interrupt(self):
        try:
            await self.client.interrupt()
        except Exception as e:  # noqa: BLE001
            self.emit("status", f"interrupt failed: {e}")

    async def _cut_turn(self, then: str | None, voice: bool = True):
        """End the running turn NOW and, if he said something, answer that.
        voice=False (two brains): only Claude's job stops — the voice, which
        may be saying "Sir, I've stopped Claude" right now, is left alone.

        interrupt() ends the turn with an error_during_execution result about
        20ms later (measured 2026-09-25, SDK 0.2.152). _consume swallows that
        result — no FAIL line, no tail of the old answer — and starts `then`
        as a fresh turn. The session keeps the cut-off reply, so the brain
        knows where it was when he spoke."""
        self._cut = True
        self._cut_id = getattr(self, "_cut_id", 0) + 1
        held, self._after_turn = getattr(self, "_after_turn", ""), ""
        if held and then:
            then = f"{held}\n{then}"   # lines held during the answer ride along
        self._after_cut = then
        self.suppressed = True
        if voice:
            self._stale_voice = False
            self.tts.cancel()
            self._say_buf = ""
        await self._safe_interrupt()
        asyncio.create_task(self._cut_watchdog(self._cut_id))

    async def _cut_watchdog(self, cut_id: int, wait_s: float = 8.0):
        """If the cut turn's result never arrives, don't leave him stuck."""
        await asyncio.sleep(wait_s)
        if self._cut and self._cut_id == cut_id:
            self._finish_cut()

    async def _warm_brain(self):
        """One muted turn right after a (re)connect so his first real turn
        doesn't pay the cold prefill. Measured 2026-09-25 on a fresh session:
        first word 3.1-3.4s on turn one, 0.8-0.9s on the turns after it —
        the difference is building the prompt cache for the persona, the
        tool list and the history. This turn builds it while he is silent.
        A pending rotation handoff rides along, so the fresh session has its
        memory before he speaks. If he talks during it, _cut_turn ends it."""
        if self.busy or self.claude_out or not WARM_BRAIN:
            return
        if self.two_brains:
            # The voice answers him; Claude's first job can pay its own cold
            # start (a few seconds inside a job of minutes) instead of a
            # warm-up turn on the top model at every boot. A pending handoff
            # rides on that first job.
            return
        text = ("[warm-up] You just (re)connected. This is not from Giorgi — "
                "reply with only: OK")
        if self._pending_handoff:
            text = f"{self._pending_handoff}\n\n{text}"
            self._pending_handoff = ""
        self._warming = True
        self.suppressed = True
        self.busy = True
        self._turn_has_tools = False
        try:
            await self.client.query(text)
        except Exception as e:  # noqa: BLE001 — a failed warm-up costs nothing
            self._warming = False
            self.suppressed = False
            self.busy = False
            self.emit("status", f"warm-up skipped: {e}")

    def _finish_cut(self):
        self._cut = False
        self._warming = False   # a cut warm-up must not swallow HIS turn
        self.suppressed = False
        self.busy = False
        self._reply_acc = ""
        if self._job_silent:
            # Claude's job was stopped; the voice's own speech state is not
            # Claude's to clear.
            self._job_silent = False
            self._watch.finish("stopped", "")
        else:
            self._say_buf = ""
            self._quiet_state, self._quiet_probe = "speak", ""
        self._work_done_at = time.monotonic()
        self.emit("work_done", "")
        nxt, self._after_cut = self._after_cut, None
        if nxt:
            asyncio.create_task(self._work(nxt))

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
        if getattr(self, "_say_buf", "") or getattr(self, "_reply_acc", ""):
            # The answer's words are already here, only its audio isn't yet:
            # a filler now would make the answer wait out ~1 s of "one
            # moment" (2026-10-18: token->audio 2.7-3.7 s on two turns).
            return
        if getattr(self, "_quiet_state", None) == "quiet":
            return    # the brain chose silence — no "Okay." either
        pool = BACKCHANNEL.get(self.turn_lang) or BACKCHANNEL["en"]
        self._bc_i = (getattr(self, "_bc_i", -1) + 1) % len(pool)
        self.tts.say(pool[self._bc_i], filler=True)

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
        # A second clip can land while the last turn is still on its way to
        # its first word; keep that turn's clock so a clip that turns out to
        # be nothing hands it back (2026-10-19: "ear() -1ms ... = 759ms").
        self._lat_prev = ((self._t_speech_end, self._lat_hang, self._t_heard,
                           self._lat_ear, self._lat_filler_ms,
                           self._lat_token_ms)
                          if getattr(self, "_t_speech_end", 0.0) else None)
        self._t_speech_end = time.monotonic() - hang
        self._lat_hang = hang * 1000
        self._t_heard = 0.0
        self._lat_ear = ""
        self._lat_filler_ms = -1.0
        self._lat_token_ms = -1.0

    def _lat_restore(self):
        """The clip that last started the clock gave no turn: give the clock
        back to the turn that was already running, if it is still unheard."""
        prev, self._lat_prev = getattr(self, "_lat_prev", None), None
        if prev and getattr(self, "_t_speech_end", 0.0) and not self._t_heard:
            (self._t_speech_end, self._lat_hang, self._t_heard, self._lat_ear,
             self._lat_filler_ms, self._lat_token_ms) = prev

    def _lat_content(self):
        # _lat_sounded clears _t_speech_end at the instant "Sir,": keep a copy
        t0 = getattr(self, "_t_answer_t0", 0.0) or getattr(self, "_t_speech_end", 0.0)
        self._t_answer_t0 = 0.0
        if t0:
            print(f"[latency] answer itself starts at "
                  f"{(time.monotonic() - t0) * 1000:.0f}ms", flush=True)

    def _lat_filler(self):
        t0 = getattr(self, "_t_speech_end", 0.0)
        if t0 and getattr(self, "_lat_filler_ms", -1.0) < 0:
            self._lat_filler_ms = (time.monotonic() - t0) * 1000

    def _lat_heard(self, ear: str):
        if getattr(self, "_t_speech_end", 0.0):
            self._t_heard = time.monotonic()
            self._lat_ear = ear

    def _lat_sounded(self):
        if getattr(self, "_lat_prev", None) and not self._t_heard:
            self._lat_restore()   # the sound is the older turn's, not the new clip's
        t0 = getattr(self, "_t_speech_end", 0.0)
        if not t0:
            return
        now = time.monotonic()
        total = (now - t0) * 1000
        ear = (self._t_heard - t0) * 1000 if self._t_heard else -1
        rest = total - ear if ear >= 0 else -1
        self._t_answer_t0 = t0            # the answer's own clock (_lat_content)
        self._t_speech_end = 0.0
        mark = "✓" if total < 500 else ("·" if total < 1000 else "!")
        hang = getattr(self, "_lat_hang", 0.0)
        filler = getattr(self, "_lat_filler_ms", -1.0)
        tok = getattr(self, "_lat_token_ms", -1.0)
        print(f"[latency] {mark} endpoint {hang:.0f}ms + ear({self._lat_ear}) "
              f"{ear - hang:.0f}ms + think/voice {rest:.0f}ms "
              f"= {total:.0f}ms to first word"
              + (f" (filler at {filler:.0f}ms)" if filler >= 0 else "")
              + (f" [first text at {tok:.0f}ms, text->audio {total - tok:.0f}ms]"
                 if tok >= 0 else ""))
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

    def _sentence_state(self):
        """Audio thread, every quiet block: does what the streaming ear has
        heard so far sound like a finished sentence? None = nothing yet."""
        rt = self._rt
        if getattr(rt, "local", False):
            # The GPU ear: its transcript of this pause is ~0.3 s out. Wait
            # for it rather than guess, then judge the words — without
            # Whisper's habit of ending every fragment on a full stop
            # ("I want to." is not a finished sentence).
            if rt.pending:
                return False
            return endpoint.sounds_finished(rt.last_partial.rstrip(". "))
        state = (None if rt is None
                 else endpoint.sounds_finished(getattr(rt, "last_partial", "")))
        if state is None and self.language != "en":
            # 2026-10-21: the Georgian ear's partials lag his voice, so on his
            # first pause there was "nothing yet" and the short 500 ms cut
            # fired — his Georgian arrived as 1.1-1.5 s stubs ("გოგი
            # ხარობა") and the rest was lost. Unknown = not finished.
            return False
        return state

    def _gpu_ear(self) -> bool:
        return bool(getattr(stt_whisper, "GPU", False)) and self.language == "en"

    def _on_utt_start(self, preroll):
        self._utt_began = time.monotonic()
        self._utt_live = True       # the voice's news waits until he's done
        self._utt_echo = self._phone_voicing()
        if self.mic_muted or self.phone_live or self._utt_echo or not self.loop:
            return
        # Dial (or re-dial) the voice socket while he is still talking, so
        # the answer's first clause doesn't pay the connection. Non-blocking.
        tts_edge.prime()
        if self.live is not None:
            # and the Live voice's session, same reason
            asyncio.run_coroutine_threadsafe(self.live.warm(), self.loop)
        elif getattr(self, "talker", None) is not None:
            talk_brain.prime()      # and Gemini's socket, same reason
        if self._gpu_ear():
            # OpenAI's Whisper on his GPU is the English ear (his choice,
            # 2026-10-24): no ElevenLabs stream to open, nor pay for. It
            # listens live: each pause is transcribed on the spot, so the
            # text is ready when he finishes (stt_whisper.Stream).
            self._rt = stt_whisper.Stream(self.loop, "en")
            if preroll is not None and len(preroll):
                self._rt.feed(preroll)
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
        self._utt_live = False
        rt, self._rt = self._rt, None
        if rt is not None:
            getattr(rt, "drop", rt.end)()

    def _on_utterance(self, audio_np: np.ndarray):
        self._utt_live = False
        self._t_last_heard = time.monotonic()
        self._lat_start()      # the clock he actually feels starts HERE
        rt, self._rt = self._rt, None
        if rt is not None:
            rt.end()          # commit at the boundary GOAT's own VAD chose
        # Pause between this utterance's first sound and the last one's final
        # sound — what the merge window is measured against.
        now = time.monotonic()
        prev_end = getattr(self, "_prev_utt_end", None)
        began = getattr(self, "_utt_began", now)
        gap = (began - prev_end) if prev_end is not None else None
        self._prev_utt_end = now - getattr(self.audio, "last_hangover_ms", 0.0) / 1000.0
        if self.mic_muted or self.phone_live:
            return   # phone live: the laptop mic would hear him twice
        if getattr(self, "_utt_echo", False):
            self._utt_echo = False
            print("[route] laptop mic heard the phone's voice — dropped")
            return
        if self.loop:
            self._route("laptop", mic=True)
            asyncio.run_coroutine_threadsafe(
                self._handle_utterance(audio_np, rt, gap), self.loop)

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
        self.submit_text(note, origin=None)

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
        """Retired 2026-09-23 (one brain). Kept so an old saved UI config
        calling it is harmless."""
        return

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

    def new_chat(self):
        """Fresh conversation, same running app. Thread-safe.

        New chat used to delete the session file and restart the WHOLE app
        (~25s: ears, voice socket, engine). The fresh brain then knew nothing
        while the chat page still painted his last exchanges from disk — he
        read that as "the memory is cleared up for some reason" (2026-10-01).
        Now it is a rotation: a turn in flight is cut, the session is dropped,
        and the new one opens with a short recap of the last few exchanges,
        so "what were we just doing?" still has an answer. Long-term memory
        (workspace/memory.md) was never touched either way."""
        if self.loop is not None and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._new_chat(), self.loop)

    async def _new_chat(self):
        if self.busy and not self._warming:
            await self._cut_turn(None)
        for _ in range(100):          # the cut lands in ~20ms; ≤10s worst case
            if not self.busy:
                break
            await asyncio.sleep(0.1)
        recent = list(self._exchanges)[-NEW_CHAT_RECAP:]
        self._exchanges.clear()
        self.talker.history.clear()
        self._local_unseen.clear()
        self._pending_handoff = ""
        if recent:
            lines = "\n".join(f"Giorgi: {u}\nYou: {r}" for u, r in recent)
            self._pending_handoff = (
                "[new chat] Giorgi started a new chat. Treat this as a fresh "
                "conversation. For reference only — bring it up just if he "
                "refers back — the last lines of the previous one:\n" + lines
                + "\nLong-term memory lives in workspace/memory.md as always.")
        self._rotate_only = True
        self._last_ctx = 0
        try:
            os.remove(SESSION_FILE)
        except OSError:
            pass
        # A marker, so the page painted at the next boot starts here too.
        try:
            with open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), "new_chat": True}) + "\n")
        except OSError:
            pass
        self.emit("work_ctx", f"0|{ROTATE_CTX}")
        self.emit("status", "new chat")
        try:
            await self.client.disconnect()   # ends _consume → run() reopens
        except Exception:  # noqa: BLE001
            pass

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

    def submit_text(self, text: str, origin: str | None = "laptop"):
        """Plain typed/spoken input — goes to the TALKING brain (middle lane).
        Thread-safe."""
        if self.loop is None or self.loop.is_closed():
            self.emit("status", "engine is down — check python\\goat-app.log")
            return
        self._route(origin)
        self._typed_lang(text)
        asyncio.run_coroutine_threadsafe(self._talk(text), self.loop)

    def submit_work(self, text: str, hard: bool = False, origin: str = "laptop"):
        """Explicit work order from the UI (work button / Ctrl+Enter, or the
        hard button / Ctrl+Shift+Enter) — goes to the WORKING brain on the
        left panel, or the hard-task brain when hard=True. This is the only
        path work reaches Claude, always his deliberate choice. Thread-safe."""
        if self.loop is None or self.loop.is_closed():
            self.emit("status", "engine is down — check python\\goat-app.log")
            return
        self._route(origin)
        self._typed_lang(text)
        if self.two_brains:
            asyncio.run_coroutine_threadsafe(self._direct_job(text, text), self.loop)
            return
        asyncio.run_coroutine_threadsafe(self._work(text, hard=hard), self.loop)

    def submit_files(self, paths: list, note: str = "", origin: str = "laptop"):
        """Files/images from the UI (drop, Ctrl+O, or pasted image) — a work
        order (looking at files needs the Read tool), so they run on the
        working brain's left lane. Thread-safe."""
        if self.loop is None or self.loop.is_closed():
            self.emit("status", "engine is down — check python\\goat-app.log")
            return
        self._route(origin)
        text = ((note + "\n") if note else "") + "[files from Giorgi]\n" + "\n".join(paths)
        # His line + thumbnails in the chat, so he can see what he sent.
        self.emit("you", note or ", ".join(os.path.basename(p) for p in paths))
        self.emit("files", "\n".join(paths))
        if self.two_brains:
            asyncio.run_coroutine_threadsafe(
                self._direct_job(text, note or "look at these files"), self.loop)
            return
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
        lines = list(ACK_ORDER[lang]) + list(
            ACK_REFLEX[lang]) + list(BACKCHANNEL[lang]) + [
            DONE_LEAD[lang], FAIL_LEAD[lang], REFLEX_FAIL[lang], LEAD_WORD[lang],
            *ACK_HANDOFF[lang], ACK_NOTE[lang], ACK_STOP[lang],
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

    async def _handle_utterance(self, audio_np: np.ndarray, rt=None, gap=None,
                                source: str = "laptop"):
        vid = getattr(self, "vid", None)
        if vid is not None and vid.enrolling(source):
            await self._enroll_turn(audio_np, rt, source)
            return
        # Voiceprint check runs beside the ear (~100ms for 3s of speech vs
        # ~270ms for the streaming transcript), so the lock costs no latency.
        vcheck = None
        if vid is not None and (vid.locked or (SOFT_VOICE and vid.enrolled("laptop"))):
            vcheck = asyncio.ensure_future(
                asyncio.to_thread(vid.check, audio_np, source))
        # The streaming ear has been transcribing since he started speaking,
        # so its answer is usually already in flight. Measured 2026-09-15 on
        # his own captured Georgian: 267-306ms after he stopped, against
        # 2520-2628ms for the batch ear on the same audio. Nothing is lost if
        # it fails — the full utterance is still in hand for the batch path.
        if rt is not None:
            text = await rt.result()
            # The local ear's "" is a real verdict (silence/junk), not a miss:
            # running the same Whisper again on the same audio says the same.
            if text or (text == "" and getattr(rt, "local", False)):
                self._lat_heard("streaming")
                # In auto mode the ear that answered IS the language of the
                # turn — that is the whole point of running both.
                heard = getattr(rt, "lang", "") or self._rt_lang()
                self._set_turn_lang(heard if heard in ("en", "ka")
                                    else self.turn_lang)
                await self._heard(text, gap, vcheck, source)
                return
            # The 2026-09-23 first turn fell to the batch ear (1812ms) and the
            # log could not say why — the reason only ever reached the status
            # line. Log it so the next miss names itself.
            why = getattr(rt, "why", None)
            print(f"[stt-rt] missed -> batch ear ({why() if why else '?'})")
            self.emit("status", "streaming ear missed — using the full one")
        elif stt_realtime.ENABLED and source != "phone" and not self._gpu_ear():
            print("[stt-rt] no stream opened -> batch ear "
                  "(realtime down after a failure, no key, or loop not ready)")
        if self._gpu_ear():
            # OpenAI's Whisper large-v3-turbo on his GPU (2026-10-24): the
            # English ear — local, no credits. Georgian is beyond it (see
            # stt_whisper), so Georgian and bilingual modes keep the cloud.
            text, _ = await asyncio.to_thread(
                stt_whisper.transcribe_lang, audio_np, 16000, "en")
            self._set_turn_lang("en")
        elif (self.language == "auto" and getattr(stt_whisper, "GPU", False)
              and stt_gladia.available()):
            # Bilingual with the GPU ear: Whisper tells English from
            # Georgian in ~1 s locally; English is its own text, Georgian
            # goes to the cloud ear pinned to Georgian (mixed-language
            # Gladia wrote his Georgian as "Good morning", 2026-10-24).
            text, heard = await asyncio.to_thread(
                stt_whisper.transcribe_lang, audio_np, 16000, None)
            if heard != "en" and text is not None:
                ka_text, _ = await asyncio.to_thread(
                    stt_gladia.transcribe_lang, audio_np, 16000, "ka")
                if ka_text is not None:
                    text, heard = ka_text, "ka"
            self._set_turn_lang(heard if heard in ("en", "ka") else self.turn_lang)
        elif self.language != "en" and stt_gladia.available():
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
        await self._heard(text, gap, vcheck, source)

    # Quiet-mic guard (2026-09-15 night, his report: "GOAT cannot hear me in
    # Georgian"). His capture endpoint had been knocked down to 66% and his
    # speech was arriving 20dB below the level it had the night before. At
    # that level the Georgian ear returns an empty transcript and the English
    # one invents fluent English over his Georgian — "The truth will be $10 a
    # month" for a Georgian sentence, "Rach Debar." for "რა ხდება". Answering
    # that is worse than admitting deafness: it puts words in his mouth and
    # then replies to them. So when Georgian is possible at all, a Latin-only
    # transcript off a capture that quiet is named for what it is, once, and
    # dropped. Georgian letters are self-evidently not a hallucination, and a
    # healthy level clears the warning so the next outage speaks again.
    def _mishearing(self, text: str) -> bool:
        peak = getattr(self.audio, "last_peak", 1.0)
        if peak >= EAR_QUIET_PEAK:
            self._quiet_warned = False
            return False
        if self.language not in ("ka", "auto"):
            return False
        if stt_realtime._has_georgian(text):
            return False
        db = 20 * np.log10(max(peak, 1e-9))
        self.emit("status", f"mic too quiet to trust ({db:.0f} dBFS) — "
                            f"ignored: {text!r}")
        if not self._quiet_warned:
            self._quiet_warned = True
            self.emit("delta", "")   # creates the reply label for the reveal
            self.tts.say(
                "შენი მიკროფონი ძალიან ჩუმია — ვერ გარკვევით გისმენ."
                if self.turn_lang == "ka" else
                "Your microphone is very quiet — I can hear something but I "
                "can't make out the words.")
        return True

    async def _voice_gate(self, vcheck, text: str | None) -> bool:
        """The voice lock's door: True lets the turn through."""
        try:
            if self.vid.locked:
                v = await vcheck
            else:
                # The soft filter must never cost him time: on his phone's
                # 5-8 s clips the voiceprint took 1.5-4 s and every answer
                # waited for it (7-8.5 s to the first word, 2026-10-25 17:18).
                # Not done by the time his words are: the turn goes through,
                # and a "that was him" still teaches the mic when it lands.
                try:
                    v = await asyncio.wait_for(asyncio.shield(vcheck), SOFT_VOICE_WAIT_S)
                except asyncio.TimeoutError:
                    loop = asyncio.get_running_loop()

                    def late(f):
                        try:
                            lv = f.result()
                        except Exception:  # noqa: BLE001
                            return
                        if lv is not None and lv.decision == "me":
                            self._voice_ok_t = time.monotonic()
                            loop.run_in_executor(None, self.vid.adapt, lv)
                    vcheck.add_done_callback(late)
                    return True
        except Exception as e:  # noqa: BLE001 — a broken check must not deafen him
            print(f"[voice-id] check failed, let through: {e!r}")
            return True
        if v is None:
            return True
        now = time.monotonic()
        active = (self.audio.is_tts_playing
                  or now - self._voice_ok_t < VOICE_ACTIVE_S)
        addressed = bool(text and WAKE_RE.search(text))
        if not self.vid.locked:
            # Soft filter, lock off: only a voice that is CLEARLY someone
            # else and doesn't call GOAT by name is dropped; everything else
            # passes, and turns that are clearly him teach the mic his voice.
            if (v.decision == "not_me" and v.score is not None
                    and v.score < SOFT_VOICE_REJECT and not addressed):
                print(f"[voice-id] someone else, not him: {text!r} ({v.line()})")
                self.emit("status", "someone else talking — ignored")
                return False
            if v.decision == "me":
                self._voice_ok_t = now
                asyncio.get_running_loop().run_in_executor(None, self.vid.adapt, v)
            return True
        if not voice_id.allow(v, active, addressed):
            print(f"[voice-id] not him, ignored: {text!r} ({v.line()})")
            self.emit("status", "not your voice — ignored")
            return False
        print(f"[voice-id] {v.line()}")
        if v.decision == "me":
            self._voice_ok_t = now
            asyncio.get_running_loop().run_in_executor(None, self.vid.adapt, v)
        return True

    def _voice_command(self, text: str, source: str) -> bool:
        """"learn my voice" / "voice lock on|off" / "forget my voice", handled
        here without the brain. (Typed, the brain does it with the voice_id
        CLI.) True = handled."""
        vid = getattr(self, "vid", None)
        if vid is None or len(text.split()) > 10:
            return False
        if VOICE_LEARN_RE.search(text):
            vid.start_enroll(source)
            self.emit("you", text)
            self._say_now("Sir, read me anything out loud for about twenty "
                          "seconds, and I'll tell you when I've got it.")
            return True
        if VOICE_FORGET_RE.search(text):
            vid.forget(source)
            self.emit("you", text)
            self._say_now("Done. I've forgotten your voice on this mic, and "
                          "the lock is off until you teach me again."
                          if not vid.locked else
                          "Done. I've forgotten your voice on this mic.")
            return True
        m = VOICE_LOCK_RE.search(text)
        if m:
            on = (m.group(1) or m.group(2)).lower() == "on"
            self.emit("you", text)
            if on and not (vid.enrolled("laptop") or vid.enrolled(source)):
                vid.start_enroll(source)
                self._say_now("I don't know your voice yet. Read me anything "
                              "for about twenty seconds and I'll lock it in.")
                return True
            vid.set_lock(on)
            if on:
                self._voice_ok_t = time.monotonic()
            self._say_now("Voice lock is on. I'll only listen to you."
                          if on else "Voice lock is off. I'll hear everyone.")
            return True
        return False

    async def _enroll_turn(self, audio_np: np.ndarray, rt, source: str):
        """One utterance of "learn my voice": it's voice to learn, not a
        message, so it never reaches the brain — unless it's him calling it off."""
        text = ""
        if rt is not None:
            try:
                text = await rt.result() or ""
            except Exception:  # noqa: BLE001
                text = ""
        if text and VOICE_CANCEL_RE.search(text) and len(text.split()) <= 5:
            self.vid.cancel_enroll()
            self._say_now("Sir, cancelled. Nothing changed.")
            return
        got, done = await asyncio.to_thread(self.vid.enroll_feed, audio_np, source)
        if done:
            self._voice_ok_t = time.monotonic()
            print(f"[voice-id] enrolled on {source}: {self.vid.status()}")
            self._say_now("Got it, I know your voice now. Voice lock is on.")
        else:
            self.emit("status", f"learning your voice — {got:.0f} of "
                                f"{voice_id.ENROLL_SPEECH_S:.0f} seconds")

    async def _heard(self, text: str | None, gap: float | None = None,
                     vcheck=None, source: str = "laptop"):
        """Everything that happens to a transcript once an ear produces one.

        Shared by BOTH ears on purpose: when the streaming ear was bolted on
        it briefly had its own shortcut into _talk, which quietly skipped the
        wake-word gate — so GOAT would have started answering the television
        again whenever the fast path won. One door in."""
        if vcheck is not None and not await self._voice_gate(vcheck, text):
            return
        self._t_last_heard = time.monotonic()
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
        if _PHANTOM.match(text):
            # 2026-10-18 02:00: "Thanks for watching." / "Please subscribe." /
            # "Mm-hmm." arrived as his messages off a quiet room — the YouTube
            # outros every speech model invents over silence. Each one cost a
            # full model turn. Whole-utterance match only, so a real sentence
            # that merely contains the words still gets through.
            print("[stt] dropped phantom phrase:", repr(text))
            return
        # The quiet-mic guard reads the LAPTOP capture level; it says nothing
        # about audio that came from the phone. And a phone turn starts with
        # him tapping the orb, so it is addressed by definition — the name
        # gate only ever made the bubble look deaf (2026-10-18).
        phone = source == "phone"
        if phone and self.phone is not None and self.phone.is_echo(text):
            # My own voice, heard by the phone's mic after I finished (the
            # panel path had no echo check: 2026-10-18 14:1x).
            stop = self.phone.stop_inside_echo(text)
            if stop:
                # ...with his "stop" inside it (2026-10-19 10:58: a 13 s clip
                # of my story carried "Okay, stop … No, no, no … Stop.").
                print(f"[barge] his stop inside my echo: {stop!r} in {text[:80]!r}")
                await self._echo_stop(stop)
                return
            run = self.phone.his_run(text)
            if not run:
                print("[phone] dropped: my own voice", repr(text))
                return
            # ...or a real request riding on it: keep just his words.
            print(f"[phone] his words inside my echo: {run!r}")
            text = run
        cut = getattr(self, "_cut_in", None)
        if (phone and cut is not None and self.phone is not None
                and self.phone.is_echo(text, overlap=True)
                and self.phone.echo_cover(text) >= 0.6):
            # A cut-in that is my own words coming back ("Sir, one moment."
            # stopped my own story, 2026-10-19 12:05) — the loose over-my-voice
            # bar: it was recorded while I talked.
            stop = self.phone.stop_inside_echo(text)
            if stop:
                print(f"[barge] his stop inside my echo: {stop!r} in {text[:80]!r}")
                await self._echo_stop(stop)
                return
            run = self.phone.his_run(text)
            if not run:
                print(f"[barge] cut-in was my own voice, carrying on: {text!r}")
                return
            print(f"[phone] his words inside my echo: {run!r}")
            text = run
        if phone and cut is not None and self._murmur(text, cut[1]):
            # A cut-in that is one or two stray words — a cough or a click
            # under my voice (2026-10-19 11:2x: a 1.7 s clip peaking at 1.000
            # came back "I am." and stopped my story to ask what he wanted).
            # His stops, my name, an order and an answer to my question
            # still cut me off.
            print(f"[barge] murmur under my voice, carrying on: {text!r}")
            return
        if not phone and self._mishearing(text):
            return
        if self._voice_command(text, source):
            return
        if (not phone
                and (self.wake_enabled or getattr(self, "_asleep", False)) and not self.busy
                and not self.audio.is_tts_playing
                and time.monotonic() - self._last_exchange > WAKE_WINDOW_S
                and not WAKE_RE.search(text)):
            # Idle and not addressed — JARVIS doesn't answer the TV.
            print(f"[wake] not addressed, ignored: {text!r}")
            self.emit("status", "heard — say my name to wake me")
            return
        self._asleep = False
        held = getattr(self, "_held", None)
        if held is not None:
            # The rest of a sentence the ear cut off: one turn, not two.
            self._held = None
            text = held.rstrip(" -–—.…") + " " + text
        # Side talk (2026-10-01: his phone call with his dad was answered
        # fragment by fragment). Only once a call/other conversation has been
        # detected does a local gate decide — see side_talk.py.
        now = time.monotonic()
        if now < getattr(self, "_side_until", 0.0):
            verdict = side_talk.gate(
                text, self.busy, WAKE_RE,
                lambda t: reflex.match(t, self.turn_lang))
            if verdict == "ignore":
                self._side_until = now + side_talk.SIDE_QUIET_S
                print(f"[side-talk] ignored: {text!r}")
                self.emit("status", "on a call? say \"GOAT\" to talk to me")
                return
            if verdict == "pass":
                self._side_until = 0.0
                print(f"[side-talk] addressed, back to normal: {text!r}")
            else:
                self._side_probe = True      # the brain decides, quietly
        elif side_talk.enters_call(text):
            # He announced it ("I'm on the phone", "call your dad"): answer
            # this line normally, then guard what follows.
            self._side_until = now + side_talk.SIDE_QUIET_S
            print(f"[side-talk] call announced: {text!r}")
        # Merge window: he paused, the first half is already with the brain,
        # and the second half starts inside MERGE_WINDOW_S — one thought, so
        # the brain gets both halves as one turn (the cut-off is the same one
        # _work does for any new message while busy).
        prev = getattr(self, "_merge_prev", None)
        # "Still on the previous one" = the brain answering him: the voice
        # with two brains (Claude's long job is not a reason to merge).
        answering = self.talk_busy if self.two_brains else self.busy
        if (prev is not None and held is None and gap is not None
                and gap <= MERGE_WINDOW_S and answering
                and not (STOP_RE.search(text) and len(text.split()) <= 5)):
            try:
                is_reflex = reflex.match(text, self.turn_lang) is not None
            except Exception:  # noqa: BLE001
                is_reflex = False
            if not is_reflex:
                print(f"[merge] {gap:.2f}s after the last one: "
                      f"{prev[0]!r} + {text!r}")
                text = prev[0].rstrip(" -–—.…") + " " + text
        if DANGLING_RE.search(text) and not answering:
            # "This is-" (live 2026-10-01): the ear committed him mid-sentence
            # and the brain answered a fragment. A transcript that trails off
            # waits HOLD_DANGLING_S for the rest before it becomes a turn.
            self._held = text
            asyncio.create_task(self._release_held(text))
            return
        await self._talk(text)

    async def _release_held(self, text: str):
        await asyncio.sleep(HOLD_DANGLING_S)
        if getattr(self, "_held", None) == text:   # nothing came after it
            self._held = None
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
        self._merge_prev = None
        self._last_exchange = time.monotonic()
        cut_in, self._cut_in = getattr(self, "_cut_in", None), None
        if cut_in is not None and time.monotonic() - cut_in[0] > CUT_IN_S:
            cut_in = None
        if echo:
            self.emit("you", text)
        # Game autopilot (2026-10-21): a spoken stop ends play and lets go
        # of every key, whatever else the word does next.
        if (game_pilot.PILOT.status_d.get("running") and STOP_RE.search(text)
                and len(text.split()) <= 6):
            game_pilot.PILOT.stop("he said stop")
            print(f"[game] stopped by voice: {text!r}", flush=True)
        if (cut_in is None and BARE_STOP_RE.match(text)
                and BARE_STOP_CORE.search(text)
                and time.monotonic() - getattr(self, "_hushed_at", 0.0) < HUSH_S):
            # The rest of his stop, after I'd already stopped ("Stop. Stop."
            # cut me off, then "Wait a second." got a reply, 2026-10-19).
            print(f"[barge] still stopped: {text!r}", flush=True)
            self._hushed_at = time.monotonic()
            return
        if cut_in is not None:
            # He talked over me on the phone and it was real words (his order
            # 2026-10-18: interruptions like a normal conversation). Like a
            # person I've stopped for good: the page dropped what it held, and
            # nothing still queued of that answer may play on.
            self.tts.cancel()
            self._say_buf = ""
            if (BARE_STOP_RE.match(text.strip())
                    and BARE_STOP_CORE.search(text)):
                print(f"[barge] he said stop: {text!r}", flush=True)
                self._hushed_at = time.monotonic()
                if self.two_brains:
                    self._front_cancel()   # he stopped my VOICE; Claude's job runs on
                elif self.busy:
                    await self._cut_turn(None)
                    self.emit("work_fail", "Stopped.")
                return
            self._cut_note = (time.monotonic(), cut_in[1])
        if (self.two_brains and BARE_STOP_RE.match(text.strip())
                and BARE_STOP_CORE.search(text)
                and (self.talk_busy or self.tts.speaking()
                     or time.monotonic() - getattr(self, "_barge_t", 0.0) < 4.0)):
            # Two brains: a bare "stop"/"wait" while the voice is talking (or
            # just got cut off by his voice) is for the voice — it goes quiet,
            # with no reply. Claude's job is NOT touched: stopping a job is
            # said in words ("stop the job", "cancel that") and the voice
            # handles it with stop_worker.
            print(f"[barge] voice hushed: {text!r}", flush=True)
            self._hushed_at = time.monotonic()
            self._front_cancel()
            return
        # Spoken brake on a running work turn.
        if self.busy and STOP_RE.search(text) and len(text.split()) <= 5:
            if self.two_brains:
                # Voice silent, Claude working, he says "stop": that's the job.
                if self._job_silent:
                    await self._stop_worker()
                else:
                    await self._cut_turn(None)
                    self.emit("work_fail", "Stopped.")
                self._front_cancel()
                self.tts.mark_reply()
                self.emit("voice_note", self._voice_label())
                self.emit("delta", "")
                self.tts.say(ACK_STOP.get(self.turn_lang) or ACK_STOP["en"])
                self.talker.remember(text, ACK_STOP.get(self.turn_lang) or ACK_STOP["en"])
                return
            # Through _cut_turn so the interrupted turn's error result is
            # swallowed — it used to follow "Stopped." with "That one failed."
            await self._cut_turn(None)
            self.emit("work_fail", "Stopped.")
            self.tts.mark_reply()
            self.emit("delta", "")
            self.tts.say("Stopped." if self.turn_lang != "ka" else "შევჩერდი.")
            return
        # His answer to "close that?" (close_guard). Yes runs the reflex's
        # held close at once, or opens the grant for the brain's retry; no
        # drops it; anything else drops the question and is a normal turn.
        asked = close_guard.pending()
        if asked is not None:
            if close_guard.YES_RE.match(text):
                if asked["run"] is not None:
                    close_guard.clear()
                    await self._reflex(text, reflex.Reflex(
                        "window", f"{asked['what']} (he said yes)", asked["run"]))
                    return
                close_guard.grant()
            elif close_guard.NO_RE.match(text):
                close_guard.clear()
                self.tts.cancel()
                self.tts.mark_reply()
                self.emit("delta", "")
                self.tts.say(asked.get("no_line") or (
                    "Sir, leaving it open." if self.turn_lang != "ka"
                    else "კარგი, არ დავხურავ."))
                return
            else:
                close_guard.clear()
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
        if self.two_brains:
            # TWO BRAINS (2026-10-24): the voice (Gemini) takes the turn. It
            # answers, or hands the job to Claude, or tells him how Claude's
            # job is going. As its own task, so his next words can cut it off.
            if (not self.talk_busy and self.tts.speaking()
                    and not (STOP_RE.search(text) and len(text.split()) <= 5)):
                self._stale_voice = True    # see the one-brain note below
            elif echo:
                self.tts.cancel()
            self._merge_prev = (text, time.monotonic())
            self._front_start(text)
            self._last_exchange = time.monotonic()
            return
        # ONE BRAIN (2026-09-23, his order): the talking lane is gone. It was
        # a second, blind mind — asked about his screen, it answered "I can't
        # see the screen, that's the working side's job". Everything that is
        # not a reflex now reaches the one brain that has eyes (screenshots,
        # the live desktop note), hands (shell, files, screen, browser), and
        # speaks its own answer as it writes it.
        if (not self.busy and self.tts.speaking()
                and not (STOP_RE.search(text) and len(text.split()) <= 5)):
            # My last answer is still queued or playing. Don't kill it before
            # we know this line is for me: side talk the brain answers with
            # [[quiet]] used to silence the answer he asked for (bug
            # 2026-10-18 13:50, "have we updated it?" never spoken on either
            # device). _speak_delta cuts it once the new reply proves real.
            self._stale_voice = True
        elif echo:
            self.tts.cancel()
        self._merge_prev = (text, time.monotonic())   # a fragment may follow
        await self._work(text, hard=bool(WORK_HARD_RE.search(text[:80])),
                         echo_you=False)
        self._last_exchange = time.monotonic()

    def _murmur(self, text: str, heard: str) -> bool:
        """One or two stray words cut in over my voice and nothing to act on:
        not a stop, not my name, not an order, not a yes/no to a question I
        just asked (`heard` = what he'd heard of my answer)."""
        import phone_server
        words = re.findall(r"[\w']+", text)
        if not words or len(words) > 2:
            return False
        if (STOP_RE.search(text) or WAKE_RE.search(text) or "?" in text
                or phone_server._STOP_LEFT.search(text)):
            return False
        if "?" in (heard or "") and re.match(
                r"(?i)\s*(yes|yeah|yep|no|nope|sure|ok(ay)?|do it|go ahead|"
                r"კი|ხო|არა|ჰო)\b", text):
            return False
        try:
            return reflex.match(text, self.turn_lang) is None
        except Exception:  # noqa: BLE001
            return True

    async def _echo_stop(self, his: str):
        """He said stop over my voice and the phone clip was mostly my echo:
        stop like the bare-stop cut-in does — voice off on every device (the
        cancel reaches the page), a running turn cut, no reply."""
        self.tts.cancel()
        self._say_buf = ""
        self._cut_in = None
        self._hushed_at = time.monotonic()
        game_pilot.PILOT.stop("he said stop")
        self.emit("you", his)
        self._last_exchange = time.monotonic()
        if self.two_brains:
            self._front_cancel()     # the voice stops; Claude's job runs on
        elif self.busy:
            await self._cut_turn(None)
            self.emit("work_fail", "Stopped.")

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
        if self.two_brains:
            self._front_cancel()   # a voice turn still composing is superseded
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
        if isinstance(result, str) and result.startswith(getattr(reflex, "DEFER", "DEFER")):
            # A fast hand that couldn't finish on its own ("click Subscribe"
            # with nothing by that name in UI Automation or OCR) hands the
            # turn to the brain, which can look — the ack already went out,
            # so to him it is one continuous "on it".
            self._say_buf = ""
            if self.two_brains:
                self.talker.remember(text, spoken)
                await self._give_worker(text, "quick", his=text)
            else:
                await self._work(text, hard=False, echo_you=False)
            self._last_exchange = time.monotonic()
            return
        if isinstance(result, str) and result.startswith("ERROR"):
            line = self._reflex_fail_line(result, rx.kind)
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
        if self.two_brains:
            self.talker.remember(text, reply)   # the voice owns the conversation
        else:
            self._local_unseen.append((text[:200], reply[:200]))
        self._log_exchange(text, reply)
        self.emit("turn_done", "")

    def _reflex_fail_line(self, result: str, kind: str) -> str:
        """The spoken correction when a reflex's action failed."""
        ka = self.turn_lang == "ka"
        if "phone offline" in result or "phone link" in result:
            return ("ბატონო, ტელეფონი არ არის დაკავშირებული." if ka
                    else "Sir, your phone isn't connected.")
        if "phone app too old" in result:
            return ("ბატონო, ამას ტელეფონის ახალი აპი სჭირდება." if ka
                    else "Sir, that needs the new phone app.")
        if result.startswith("ERROR: phone"):
            return ("ბატონო, ტელეფონმა ვერ შეასრულა." if ka
                    else "Sir, the phone didn't take it.")
        if not kind.startswith("open") and not ka:
            return "Sir, that didn't work."     # not everything "opens"
        return REFLEX_FAIL.get(self.turn_lang) or REFLEX_FAIL["en"]

    # ==== TWO BRAINS: the voice (Gemini) and its partner (Claude) ==========
    # His order 2026-10-24 — see talk_brain.py for the why and the numbers.
    # The voice turn below reuses the one-brain speech path (_speak_delta:
    # "Sir," lead, clause-first TTS, [[quiet]] side talk, stale-voice hand
    # over); Claude's jobs run through _work_job and never touch that path.

    def _worker_target(self) -> str:
        """Claude's highest model this account can run right now."""
        if self.work_model == "fable 5.1" and self.fable.ok:
            return MODEL_FABLE
        return MODEL_FULL

    def _worker_label(self) -> str:
        return _friendly_model_name(self._worker_target()).title()

    async def _voice_tool(self, name: str, args: dict):
        """Live's own hands: run it, hand the result back to Google to say.
        None = not one of them (GOAT runs that action itself)."""
        if name not in voice_hands.NAMES:
            return None
        t0 = time.monotonic()
        r = await asyncio.to_thread(voice_hands.execute, name, args)
        print(f"[voice-hands] {name} {str(args)[:80]} -> {r[:120]!r} "
              f"({time.monotonic() - t0:.1f}s)", flush=True)
        return r

    def _voice_label(self) -> str:
        """Who is talking: Gemini Live, or the REST voice standing in."""
        if self.live is not None and self.live.usable():
            return self.live.label()
        return self.talker.label()

    def _emit_brains(self):
        """The interface's picture of both brains: 'voice|work|note'."""
        note = ""
        if self.work_model == "fable 5.1" and not self.fable.ok:
            note = f"Fable 5.1 {self.fable.reason} — Opus 5.5 works instead"
        if self.claude_out:
            note = "Claude is out of usage" + (
                f" — back at {self.claude_reset}" if self.claude_reset else "")
        if not self.two_brains:
            self.emit("brains", f"|{self._worker_label()}|{note}")
            return
        if self.live is not None and self.live.usable():
            self.emit("brains", f"{self.live.label()}|{self._worker_label()}|{note}|")
            return
        voice = self.talker.label()
        vnote = self.talker.quota_note()
        if not self.talker.usable():
            vnote = (f"free quota used up — back at {self.talker.out_until()}; "
                     "Claude covers when free")
        elif self.talker.fail_streak >= 2:
            vnote = "Gemini not answering right now"
        self.emit("brains", f"{voice}|{self._worker_label()}|{note}|{vnote}")

    def _front_cancel(self):
        """Silence the voice turn in flight (his words cut it off)."""
        self._front_gen += 1
        t = self._front_task
        if t is not None and not t.done():
            t.cancel()
        self.talk_busy = False

    def _front_start(self, text: str, kind: str = "user", event: dict | None = None):
        old = self._front_task
        if old is not None and not old.done():
            old.cancel()
        self._front_task = asyncio.create_task(self._front_turn(text, kind, event))
        return self._front_task

    async def _front_parts(self, text: str, kind: str, event: dict | None,
                           probe: bool) -> list:
        """The message the voice gets: what it can see right now (clock,
        screen, body, Claude's job), then his words or the event."""
        blocks = [talk_brain.clock_note()]
        try:
            blocks.append(await asyncio.to_thread(live_view.note))
        except Exception as e:  # noqa: BLE001
            blocks.append(f"[live desktop unavailable: {e}]")
        try:
            full = (not self._talk_body_seen
                    or (kind == "user" and bool(body_sense.ASKED_RE.search(text))))
            body = self._body.note(full=full)
            self._talk_body_seen = True
            if body:
                blocks.append(body)
        except Exception as e:  # noqa: BLE001
            print(f"[body] {type(e).__name__}: {e}")
        if kind == "user":
            try:
                inner = self._inner.turn_note(text)
                if inner:
                    blocks.append(inner)
            except Exception as e:  # noqa: BLE001
                print(f"[inner] {type(e).__name__}: {e}")
        if self._local_unseen:
            blocks.append("[said or done by GOAT since your last turn — context "
                          "only, don't reply to it]\n" + "\n".join(
                              f"him: {u}\nGOAT: {a}" for u, a in self._local_unseen[-6:]))
            self._local_unseen.clear()
        news = self_update.take_notice()
        if news:
            blocks.append("[self-update since he last spoke — tell him in one "
                          "short line, then answer him]\n" + news)
        extra = ""
        if self.work_model == "fable 5.1" and not self.fable.ok:
            extra = (f"(Fable 5.1, Claude's top model, {self.fable.reason}; "
                     "Opus 5.5 does the work meanwhile)")
        plan = getattr(self, "_plan_now", None) or {}
        try:
            five = float(plan.get("five_hour") or 0)
        except (TypeError, ValueError):
            five = 0.0
        if five >= 70:
            # 2026-10-24: one redesign ran the 5-hour window dry with no
            # warning. He watches his usage — let the voice watch it too.
            extra = ((extra + "\n") if extra else "") + (
                f"Claude's 5-hour usage is at {five:.0f}%"
                + (f" (resets {plan.get('five_hour_resets')})"
                   if plan.get("five_hour_resets") else "")
                + ". This is ONLY about jobs you hand to Claude: before a big "
                "(deep) one, tell him in one line and ask; quick jobs are fine. "
                "Talking and explaining yourself costs Claude nothing — never "
                "ask before answering him.")
        if self.claude_out:
            extra = ("Claude is OUT OF USAGE" + (f" until about {self.claude_reset}"
                     if self.claude_reset else "") + " — jobs can't run until then; "
                     "only instant commands (open, close, volume, media) work.")
        eff = getattr(self, "_live_effort", None) or self.effort
        blocks.append(self._watch.status_note(self._worker_label(), eff, extra))
        if kind == "user":
            final = self._watch.take_final()
            if final is not None:
                blocks.append("[news to fold in — answer him first, then tell him "
                              "this in one short sentence]\n"
                              + self._watch.event_prompt(final))
        if MODEL_Q_RE.search(text or ""):
            blocks.append(f"[models: you, the voice, are "
                          f"{self._voice_label()}; "
                          f"Claude, the working brain, is {self._worker_label()}]")
        if self.turn_lang == "ka":
            blocks.append(KA_TALK_NOTE.strip())
        if probe:
            blocks.append(side_talk.PROBE_NOTE.strip())
        cut = getattr(self, "_cut_note", None)
        self._cut_note = None
        if cut is not None and time.monotonic() - cut[0] < CUT_IN_S:
            blocks.append("[he talked over you, so you stopped mid-answer"
                          + (f'; the last thing he heard: "{cut[1][:200]}"'
                             if cut[1] else "")
                          + ". Answer what he said now.]")
        head = "\n".join(blocks)
        if kind == "user":
            return [{"text": f"{head}\n\nGiorgi: {text}"}]
        return [{"text": f"{head}\n\n{self._watch.event_prompt(event)}"}]

    async def _front_turn(self, text: str, kind: str = "user", event: dict | None = None):
        """One voice turn: Gemini answers out loud, and may hand Claude a job,
        pass Claude a note, stop Claude, or look at the screen.
        kind: "user" (he spoke) or "event" (news about Claude's job)."""
        self._front_gen += 1
        my = self._front_gen
        barge0 = self._barge_n
        self.talk_busy = True
        spoken: list = []
        acted: list = []
        do_cmds: list = []
        look: list = []
        lang = self.turn_lang if self.turn_lang in ("en", "ka") else "en"
        started_speaking = False

        def live() -> bool:
            return my == self._front_gen and self._barge_n == barge0

        def on_text(t: str):
            nonlocal started_speaking
            if not live():
                return
            started_speaking = True
            spoken.append(t)
            self._speak_delta(t)

        live_txt: list = []

        def on_text_live(t: str):
            # Live speaks for itself: its transcript is shown, never voiced
            # again. It rides on the next piece of its own audio.
            nonlocal started_speaking
            if not live():
                return
            started_speaking = True
            spoken.append(t)
            live_txt.append(t)
            self.emit("delta", t)

        def on_audio_live(pcm):
            nonlocal started_speaking
            if not live():
                return
            started_speaking = True
            self.tts.say_audio("".join(live_txt), pcm)
            live_txt.clear()

        hands: list = []      # the voice's own hands, for the REST voice's 2nd pass

        def on_call(name: str, args: dict):
            if not live():
                return
            acted.append(name)
            his = text if kind == "user" else ""
            if name in voice_hands.NAMES:
                # Live runs these through _voice_tool and speaks the result;
                # the REST voice gets them in a second pass below.
                self.emit("tool", f"{name} {json.dumps(args, ensure_ascii=False)[:90]}")
                hands.append((name, args))
            elif name == "hand_to_worker":
                task = str(args.get("task") or text).strip()
                depth = "quick" if args.get("depth") == "quick" else "deep"
                print(f"[voice→claude] {depth}: {task[:100]!r}", flush=True)
                asyncio.create_task(self._give_worker(task, depth, his=his))
            elif name == "message_worker":
                asyncio.create_task(self._message_worker(
                    str(args.get("message") or text), his=his))
            elif name == "stop_worker":
                asyncio.create_task(self._stop_worker())
            elif name == "do_now":
                do_cmds.append(str(args.get("command") or ""))
                asyncio.create_task(self._voice_do(
                    str(args.get("command") or text), his=his))
            elif name == "email":
                asyncio.create_task(self._voice_email(args, his=his))
            elif name == "send_draft":
                asyncio.create_task(self._voice_send_draft())
            elif name == "look_at_screen":
                look.append(str(args.get("question") or ""))

        try:
            if kind == "event":
                # News arrives on its own line in the chat, not inside his
                # last answer's label.
                self.tts.mark_reply()
                self.tts.new_turn()
                self.emit("voice_note", self._voice_label())
                self.emit("delta", "")
            elif not getattr(self, "_stale_voice", False):
                self.tts.new_turn()
                self.emit("delta", "")
            self._first_said = False
            self._say_buf = ""
            self._quiet_state = None if kind == "user" else "speak"
            self._quiet_probe = ""
            probe = kind == "user" and bool(getattr(self, "_side_probe", False))
            self._side_probe = False
            if (kind == "user" and not probe
                    and time.monotonic() >= getattr(self, "_side_until", 0.0)
                    and not side_talk.looks_aside(text, WAKE_RE)):
                asyncio.create_task(self._front_filler(my))
            parts = await self._front_parts(text, kind, event, probe)
            res = {"ok": False, "model": "", "ttft": None, "error": ""}
            # Live first. Side-talk probes stay on the REST voice: their
            # [[quiet]] token would be SPOKEN by a voice that speaks itself.
            via_live = (self.live is not None and self.live.usable() and not probe)
            if via_live:
                self._quiet_state = "speak"
                res = await self.live.reply(parts, on_text_live, on_call, on_audio_live)
                if live_txt and live():
                    self.tts.say_audio("".join(live_txt), None)
                    live_txt.clear()
                if not res["ok"]:
                    print(f"[live] {res['error']} — REST voice takes the turn"
                          if not started_speaking else
                          f"[live] {res['error']} (after speaking)", flush=True)
                if res["ok"] or started_speaking or acted:
                    res["ok"] = True
                else:
                    via_live = False
            if not via_live:
                if self.talker.usable():
                    res = await self.talker.reply(parts, on_text, on_call)
                else:   # every Gemini model is resting: don't wait on a sure "no"
                    res = {"ok": False, "model": "", "ttft": None,
                           "error": f"Gemini out of quota until {self.talker.out_until()}"}
            rounds = 0
            while (not via_live and res["ok"] and hands and live() and rounds < 3):
                # The REST voice can't take results mid-stream: run its hands,
                # then ask again with what they returned.
                rounds += 1
                batch, hands[:] = list(hands), []
                got = []
                for n, a in batch:
                    r = await asyncio.to_thread(voice_hands.execute, n, a)
                    print(f"[voice-hands] {n} {str(a)[:80]} -> {r[:120]!r}", flush=True)
                    got.append(f"{n}({json.dumps(a, ensure_ascii=False)[:200]}) "
                               f"returned:\n{r[:4000]}")
                said = "".join(spoken).strip()
                parts = parts + [{"text": "[what your own hands returned — answer him from "
                                          "it" + (f'; you already said "{said}", go on '
                                                  "without repeating it" if said else "")
                                          + "]\n" + "\n\n".join(got)}]
                res = await self.talker.reply(parts, on_text, on_call)
            if res["ok"] and look and live():
                shot = await asyncio.to_thread(talk_brain.screenshot_part)
                said = "".join(spoken).strip()
                note = ("[the screenshot of his screen you asked for is attached — "
                        "answer from it" + (f'; you already said "{said}", so '
                        "continue without repeating it" if said else "") + "]"
                        if shot else "[the screenshot failed — answer from the "
                        "desktop note, or hand it to Claude to look]")
                parts2 = parts + ([shot] if shot else []) + [{"text": note}]
                no_look = (lambda n, a: None if n == "look_at_screen" else on_call(n, a))
                if via_live:
                    res = await self.live.reply(parts2, on_text_live, no_look, on_audio_live)
                    if live_txt and live():
                        self.tts.say_audio("".join(live_txt), None)
                        live_txt.clear()
                    res["ok"] = True
                else:
                    res = await self.talker.reply(parts2, on_text, no_look)
            self._emit_brains()          # winner model / quota state for the card
            if res["ok"]:
                self.emit("talkbrain", self.live.label() if via_live
                          else self.talker.label())
                if res.get("ttft") is not None:
                    print(f"[talk] {res['model']} first token {res['ttft']:.2f}s"
                          + (f" (ended: {res['error']})" if res["error"] else ""),
                          flush=True)
            if not res["ok"] and live():
                print(f"[talk] Gemini failed: {res['error']}", flush=True)
                self._emit_brains()
                if not started_speaking:
                    if kind == "event":
                        line = self._watch.fallback_line(event, lang)
                        if line:
                            self.tts.say(line)
                            spoken.append(line)
                    elif talk_brain.STATUS_Q_RE.search(text) and (
                            self.busy or self._watch.job):
                        # "what's it doing?" needs no model: the watch knows.
                        line = self._watch.local_status(lang)
                        self.tts.say(line)
                        spoken.append(line)
                    elif (not self.busy and not self.claude_out
                          and brain_route.classify(text, False) != brain_route.TALK):
                        # An order with the voice down: Claude does it as a
                        # normal silent job and the watch's own words report
                        # it — not a talking turn that works for 40 s first.
                        line = VOICE_OUT_JOB[lang]
                        self.tts.say(line)
                        spoken.append(line)
                        acted.append("hand_to_worker")
                        asyncio.create_task(self._give_worker(text, "quick", his=text))
                    elif not self.busy and not self.claude_out:
                        # Claude covers this one turn and speaks it itself (the
                        # one-brain path), so the voice never just goes silent.
                        print("[talk] Claude covers this turn", flush=True)
                        self.talk_busy = False
                        self._quiet_state = "speak"
                        self._cover_turn = True
                        await self._work(text, hard=False, echo_you=False)
                        return
                    else:
                        back = self.talker.out_until()
                        if back and lang == "en":
                            line = (f"Sir, Gemini's out of free requests until {back}. "
                                    "Claude's still on your job — I'll tell you when "
                                    "it's done.")
                        elif back:
                            line = (f"ბატონო, Gemini-ს ლიმიტი {back}-მდე ამოიწურა. "
                                    "Claude ისევ მუშაობს, დასრულებისას გეტყვი.")
                        else:
                            line = VOICE_DOWN[lang]
                        self.tts.say(line)
                        spoken.append(line)
            if not live():
                return
            said = "".join(spoken)
            if (kind == "user" and res["ok"] and not acted
                    and talk_brain.CLAIM_RE.search(said)
                    and (not self.busy or PASSED_RE.search(said))):
                # It SAID Claude has it but called nothing: make it true.
                # (Claude already busy: "Claude's on it" is simply true —
                # only "I've passed/told it" owes Claude a note.)
                # His bare words can be just "Yes." (2026-10-24: a deep job
                # titled "Yes."), so the job carries what he said yes TO.
                # (this turn isn't in the history yet: the last model line is
                # what the voice said before he spoke)
                asked = next((h["text"] for h in reversed(self.talker.history)
                              if h["role"] == "model"), "")
                task = (f'Giorgi said "{text}" in reply to GOAT\'s voice, which had just '
                        f'said: "{asked[:400]}". Do what he agreed to or asked for.'
                        if asked and len(text.split()) <= 6 else text)
                print(f"[talk] claim without action — handing over: {text[:60]!r}",
                      flush=True)
                acted.append("hand_to_worker")
                asyncio.create_task(self._give_worker(task, "deep", his=text))
            # End of the turn: same tail rules as the one-brain path.
            qstate = self._quiet_state
            if qstate is None and self._quiet_probe:
                self._quiet_state = "speak"
                held, self._quiet_probe = self._quiet_probe, ""
                self._speak_delta(held)
                qstate = "speak"
            if qstate == "quiet":
                self._quiet_state = "speak"
                self._say_buf = ""
                self._side_until = time.monotonic() + side_talk.SIDE_QUIET_S
                print(f"[side-talk] voice stayed quiet: {text!r}", flush=True)
                self.emit("reply_drop", "")
                self.emit("status", "on a call? say \"GOAT\" to talk to me")
                self.talker.remember(text, side_talk.QUIET_TOKEN)
                return
            self._quiet_state = "speak"
            self._stale_voice = False
            self._flush_sentences(force=True)
            reply = "".join(spoken).strip()
            if not reply:
                ack = ""
                if "hand_to_worker" in acted:
                    pool = ACK_HANDOFF[lang]
                    self._ho_i = (getattr(self, "_ho_i", -1) + 1) % len(pool)
                    ack = pool[self._ho_i]
                elif "message_worker" in acted:
                    ack = ACK_NOTE[lang]
                elif "stop_worker" in acted:
                    ack = ACK_STOP[lang]
                elif "do_now" in acted:
                    ack = self._do_ack(do_cmds[0] if do_cmds else "", lang)
                elif "email" in acted or "send_draft" in acted:
                    ack = ACK_MAIL[lang]
                if ack:
                    self.tts.say(ack)
                    reply = ack
            if kind == "event":
                self.talker.remember(self._event_line(event), reply)
                if reply:
                    self.emit("work_voice", reply)
                    self._log_exchange(self._event_line(event), reply)
            else:
                self.talker.remember(text, reply)
                self._exchanges.append((text[:300], reply[:300]))
                if not text.startswith("[boot-briefing]"):
                    self._log_exchange(text, reply)
            self._last_exchange = time.monotonic()
            self.emit("turn_done", "")
        except asyncio.CancelledError:
            # His next words cut this turn off. News that never got said goes
            # back to the front of the queue; what he heard stays in history.
            if kind == "event" and not started_speaking and event is not None:
                self._watch.pending.insert(0, event)
            elif kind == "user" and text:
                self.talker.remember(text, "".join(spoken).strip() + " …(cut off)")
            raise
        except Exception as e:  # noqa: BLE001 — a voice bug never kills GOAT
            print(f"[talk] turn crashed: {type(e).__name__}: {e}", flush=True)
            if not started_speaking and live():
                self.tts.say(VOICE_DOWN[lang])
        finally:
            if my == self._front_gen:
                self.talk_busy = False

    @staticmethod
    def _event_line(ev: dict | None) -> str:
        if not ev:
            return "[event]"
        return {"done": "[event: Claude finished its job]",
                "failed": "[event: Claude's job failed]",
                "stuck": "[event: Claude looked stuck]",
                "checkin": "[event: progress check on Claude's job]",
                "note": "[event: a note from Claude]"}.get(ev.get("kind"), "[event]")

    async def _front_filler(self, my: int):
        """The voice's "Sir, one moment." if Gemini is slow to start (its
        tail latency is real — see talk_brain.py)."""
        if BACKCHANNEL_MODE in ("off", "0", "false", "no"):
            return
        gen = self.tts.gen
        await asyncio.sleep(BACKCHANNEL_AFTER_S)
        if (my != self._front_gen or gen != self.tts.gen or self.tts._sounded
                or not self.tts.enabled or self._say_buf
                or self._quiet_state == "quiet" or not self.talk_busy):
            return
        pool = BACKCHANNEL.get(self.turn_lang) or BACKCHANNEL["en"]
        self._bc_i = (getattr(self, "_bc_i", -1) + 1) % len(pool)
        self.tts.say(pool[self._bc_i], filler=True)

    async def _give_worker(self, task: str, depth: str = "deep", his: str = ""):
        """The voice hands Claude a job (or Claude is busy: it's a note)."""
        try:
            if self.busy and self._job_silent:
                await self._message_worker(task, his=his)
                return
            for _ in range(50):           # a warm-up/compact/cut finishing
                if not self.busy:
                    break
                await asyncio.sleep(0.1)
            if self.busy:
                await self._message_worker(task, his=his)
                return
            await self._work_job(task, his, depth)
        except Exception as e:  # noqa: BLE001
            print(f"[voice→claude] dispatch failed: {type(e).__name__}: {e}", flush=True)
            self._watch.start(task, his, self._worker_label(), depth)
            self._watch.finish("failed", f"GOAT couldn't start the job: {e}")

    async def _message_worker(self, message: str, his: str = ""):
        """A note from the voice into Claude's running job."""
        message = (message or "").strip()
        if not message:
            return
        if not (self.busy and self._job_silent):
            await self._give_worker(message, "deep", his=his)
            return
        self._watch.added(message)
        self.emit("work_add", f"voice → Claude: {message[:140]}")
        note = f"[note from Giorgi via the voice, while you work: {message}"
        if his and his.strip() != message:
            note += f' (his words: "{his.strip()[:300]}")'
        try:
            await self.client.query(note + "]")
        except Exception as e:  # noqa: BLE001
            print(f"[voice→claude] note failed: {e}", flush=True)

    async def _stop_worker(self):
        if not self.busy:
            return
        print("[voice→claude] stop", flush=True)
        if self._job_silent:
            self._watch.finish("stopped", "")
        await self._cut_turn(None, voice=False)
        self.emit("work_fail", "Stopped — Giorgi asked")

    # ---- the voice's own hands (his ask 2026-10-24: "small tasks like open,
    # close and send me an email — if Gemini can handle it"). Gemini words the
    # order as a plain command and the reflex lane runs it: the same tested
    # matcher, close guard and phone channel as a spoken reflex, in
    # milliseconds instead of a 10-second Claude job. ----

    async def _say_after_voice(self, line: str, gen: int, must: bool = False):
        """A follow-up line (a failure, "sent") once the voice's own sentence
        is out — never spliced into the middle of it. Dropped if he has
        started another turn since, unless he must hear it (an email's fate)."""
        for _ in range(300):
            if not self.talk_busy or (self._front_gen != gen and not must):
                break
            await asyncio.sleep(0.1)
        if self._front_gen == gen or must:
            self.tts.say(line)
            self.talker.note(f"then said: {line}")

    @staticmethod
    def _do_variants(command: str) -> list:
        """Gemini's wording of a quick order, plus the plain forms the reflex
        lane knows ("media next" -> "next song"; "... in chrome" dropped)."""
        c = " ".join((command or "").split()).strip(" .!")
        out = [c]
        bare = re.sub(r"\s+(?:in|on|with|using)\s+(?:the\s+|my\s+)?(?:chrome|brave|edge|"
                      r"firefox|browser|default browser)$", "", c, flags=re.IGNORECASE)
        bare = re.sub(r"^media\s+(?:next|skip)\b.*", "next song", bare, flags=re.IGNORECASE)
        bare = re.sub(r"^media\s+(?:prev|previous|back)\b.*", "previous song", bare,
                      flags=re.IGNORECASE)
        bare = re.sub(r"^(?:media\s+)?(?:play_pause|play|pause|resume|stop)\b.*",
                      "pause", bare, flags=re.IGNORECASE)
        bare = re.sub(r"^skip\b.*", "next song", bare, flags=re.IGNORECASE)
        if bare != c:
            out.append(bare)
        return out

    @staticmethod
    def _do_ack(command: str, lang: str) -> str:
        """His ack when the voice called do_now without saying a word."""
        c = " ".join((command or "").split())
        m = re.match(r"(?i)(open|close|quit)\s+(.+)", c)
        if m and lang != "ka":
            what = re.sub(r"(?i)^https?://(?:www\.)?|\.com$|/$|\s+on my phone$", "",
                          m.group(2)).strip()
            verb = "opening" if m.group(1).lower() == "open" else "closing"
            return f"Sir, {verb} {what}." if len(what) <= 30 else f"Sir, {verb} it."
        if m:
            return "ბატონო, ვხსნი." if m.group(1).lower() == "open" else "ბატონო, ვხურავ."
        return "ბატონო, გაკეთდა." if lang == "ka" else "Sir, done."

    async def _voice_do(self, command: str, his: str = ""):
        gen = self._front_gen
        lang = self.turn_lang if self.turn_lang in ("en", "ka") else "en"
        try:
            rx = None
            for c in self._do_variants(command) + ([his] if his else []):
                rx = reflex.match(c, lang)
                if rx is not None:
                    break
        except Exception as e:  # noqa: BLE001 — a matcher bug costs a hand-off, not the order
            print(f"[voice-do] matcher failed: {e}", flush=True)
            rx = None
        if rx is not None and rx.kind == "answer":
            await self._say_after_voice(rx.speak, gen)    # time, battery, usage
            return
        if rx is None:
            # Not a fixed action after all: Claude does it.
            print(f"[voice-do] no quick action for {command!r} -> Claude", flush=True)
            self.talker.note("not a quick action: passed to Claude")
            await self._give_worker(command, "quick", his=his)
            return
        self.emit("status", f"{rx.kind} · {rx.detail}")
        if rx.speak:
            # The close guard's "That's a terminal — close it anyway?"
            await self._say_after_voice(rx.speak, gen)
        t0 = time.monotonic()
        try:
            result = await asyncio.to_thread(rx.run)
        except Exception as e:  # noqa: BLE001
            result = f"ERROR: {e}"
        print(f"[voice-do] {command!r} -> {rx.kind} {rx.detail!r} -> {result} "
              f"({(time.monotonic() - t0) * 1000:.0f}ms)", flush=True)
        result = str(result or "")
        if result.startswith(getattr(reflex, "DEFER", "DEFER")):
            self.talker.note("couldn't do it directly: passed to Claude")
            await self._give_worker(command, "quick", his=his)
        elif result.startswith("ERROR"):
            await self._say_after_voice(self._reflex_fail_line(result, rx.kind), gen)
        else:
            self.talker.note(f"done: {rx.kind} {rx.detail}".strip())

    async def _voice_email(self, args: dict, his: str = ""):
        import mail_hands
        gen = self._front_gen
        lang = self.turn_lang if self.turn_lang in ("en", "ka") else "en"
        to, bad = mail_hands.resolve_to(str(args.get("to") or ""))
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "").strip()
        send = bool(args.get("send"))
        if bad or not to:
            line = ("Sir, I need the full email address for "
                    f"{', '.join(bad) or 'that'}." if lang == "en" else
                    f"ბატონო, {', '.join(bad) or 'ამის'} სრული ელფოსტის მისამართი მჭირდება.")
            print(f"[voice-mail] no address for {bad!r}", flush=True)
            await self._say_after_voice(line, gen)
            return
        print(f"[voice-mail] {'send' if send else 'draft'} to {len(to)} "
              f"recipient(s), subject {subject[:60]!r}", flush=True)
        self.emit("status", "email · " + ("sending" if send else "drafting"))
        fn = mail_hands.send if send else mail_hands.open_draft
        result = await asyncio.to_thread(fn, to, subject, body)
        print(f"[voice-mail] -> {result}", flush=True)
        if result.startswith("sent"):
            self.emit("status", "email · sent")
            await self._say_after_voice(MAIL_SENT[lang], gen, must=True)
        elif result.startswith("draft open"):
            self.talker.note(f"done: {result} — waiting for his yes")
            who = ", ".join(a.split("@")[0] for a in to)
            await self._say_after_voice(
                f"Sir, the draft to {who} is on your screen. Send it?" if lang == "en"
                else f"ბატონო, {who}-სთვის წერილი ეკრანზეა. გავაგზავნო?", gen)
        elif "didn't open" in result or "browser" in result:
            await self._say_after_voice(MAIL_NO_DRAFT[lang], gen, must=True)
        else:
            await self._say_after_voice(MAIL_FAIL[lang], gen, must=True)

    async def _voice_send_draft(self):
        import mail_hands
        gen = self._front_gen
        lang = self.turn_lang if self.turn_lang in ("en", "ka") else "en"
        self.emit("status", "email · sending")
        result = await asyncio.to_thread(mail_hands.send_open_draft)
        print(f"[voice-mail] send draft -> {result}", flush=True)
        if result.startswith("sent"):
            self.emit("status", "email · sent")
            await self._say_after_voice(MAIL_SENT[lang], gen, must=True)
        elif "no Gmail draft" in result:
            await self._say_after_voice(
                "Sir, I don't see the draft open anymore." if lang == "en"
                else "ბატონო, დრაფტი აღარ ჩანს.", gen)
        else:
            await self._say_after_voice(MAIL_FAIL[lang], gen, must=True)

    async def _direct_job(self, task: str, his: str):
        """His work button / dropped files: straight to Claude, and the voice
        says so (and tells him the result when the watch reports it)."""
        lang = self.turn_lang if self.turn_lang in ("en", "ka") else "en"
        pool = ACK_HANDOFF[lang]
        self._ho_i = (getattr(self, "_ho_i", -1) + 1) % len(pool)
        self._front_cancel()
        self.tts.cancel()
        self.tts.new_turn()
        self.emit("delta", "")
        self.tts.say(pool[self._ho_i])
        self.talker.remember(his, pool[self._ho_i])
        await self._give_worker(task, "deep", his=his)

    async def _work_job(self, task: str, his: str = "", depth: str = "deep"):
        """Claude does one job for the voice: silently, on the Activity panel,
        on Claude's highest model, at full thinking for deep work. Its final
        text is the report the watch hands back to the voice."""
        task = (task or "").strip()
        if not task:
            return
        if self.claude_out and self._limit_expired():
            self.claude_out = False
            self.emit("claude", "ok")
        target = self._worker_target()
        name = _friendly_model_name(target)
        self._watch.start(task, his, name.title(), depth)
        if self.claude_out:
            self._watch.finish("limit", "Claude is out of usage" + (
                f" until about {self.claude_reset}" if self.claude_reset else ""))
            self.emit("work_fail", "Claude is out of usage")
            return
        self.busy = True
        self._cover_turn = False
        self._job_silent = True
        self._job_args = (task, his, depth)
        self.last_user_text = task
        self._current_task = task
        self._work_started = time.monotonic()
        self._tl_reset()
        self._turn_has_tools = False
        self._last_tool = ""
        self._reply_acc = ""
        if self.model != target:
            try:
                await self.client.set_model(target)
                self.model = target
            except Exception as e:  # noqa: BLE001
                self.emit("status", f"model switch failed: {e}")
        deep = depth != "quick"
        tier = brain_route.DEEP if deep else brain_route.TASK
        effort = self._turn_effort(task, deep, tier)
        print(f"[claude] job ({depth}) on {name} @ {effort}: {task[:70]!r}", flush=True)
        self.emit("model", name)
        self.emit("brain", f"{name}|{tier}|{effort}")
        self._emit_brains()
        self.emit("work_start", f"{name}|{task}")
        try:
            view = await asyncio.to_thread(live_view.note)
        except Exception as e:  # noqa: BLE001
            view = f"[live desktop unavailable: {e}]"
        recent = list(self.talker.history)[-8:]
        convo = "\n".join(f"{'Giorgi' if m['role'] == 'user' else 'voice'}: "
                          f"{m['text'][:300]}" for m in recent)
        send = view
        if convo:
            send += ("\n\n[recent conversation between Giorgi and the voice — "
                     "context only]\n" + convo)
        send += (f"\n\n[models: the voice is {self._voice_label()}; you are "
                 f"{name.title()}]")
        send += "\n\n[job from GOAT's voice"
        if his and his.strip() and his.strip() != task:
            send += f' — Giorgi said: "{his.strip()[:500]}"'
        send += f"]\n{task}"
        if self._pending_handoff:
            send = self._pending_handoff + "\n\n" + send
            self._pending_handoff = ""
        await self._route_effort(task, deep, tier)
        self._tl_prep = time.monotonic() - self._tl_t0
        await self.client.query(send)

    def _worker_finished(self, state: str, report: str):
        """Claude's job ended: hand the outcome to the watch (the voice says it)."""
        self._job_silent = False
        self._watch.finish(state, report)
        print(f"[claude] job {state}: {(report or '')[:120]!r}", flush=True)

    async def _watch_loop(self):
        """The voice keeping an eye on Claude: once a second, is there news
        worth saying, and is this a moment to say it?"""
        while True:
            await asyncio.sleep(1.0)
            if not self.two_brains:
                continue
            try:
                now = time.monotonic()
                quiet = (not self.talk_busy and not self.tts.speaking()
                         and not self.audio.is_tts_playing and not self._utt_live
                         and now - self._t_last_heard > 2.5
                         and getattr(self, "_cut_in", None) is None
                         and getattr(self, "_held", None) is None)
                held = (now < getattr(self, "_side_until", 0.0)
                        or getattr(self, "_asleep", False))
                ev = self._watch.due(quiet, now - self._last_exchange, held)
                if ev is not None:
                    print(f"[watch] voicing {ev['kind']}", flush=True)
                    self._front_start("", "event", ev)
            except Exception as e:  # noqa: BLE001
                print(f"[watch] {type(e).__name__}: {e}", flush=True)

    async def _probe_fable(self):
        """Is Claude's top model usable on this account? (talk_brain.FableGate)"""
        if self.fable.fresh() or self.work_model != "fable 5.1":
            self._emit_brains()
            return
        await asyncio.to_thread(self.fable.probe, MODEL_FABLE, WORKSPACE)
        self._emit_brains()

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
        if target == MODEL_FABLE and not getattr(getattr(self, "fable", None), "ok", False):
            target = MODEL_FULL   # Fable 5.1 needs usage credits (talk_brain.FableGate)
        if self.busy:
            # He spoke while GOAT is mid-turn. JARVIS doesn't announce
            # "adding that" (his goal 2026-09-25: it kept saying "ესეც
            # ჩავამატე" to every word, even "Hmm?") — he stops and listens.
            if self._cut:
                # A cut is already in flight; this rides along with it.
                self._after_cut = (f"{self._after_cut}\n{text}"
                                   if self._after_cut else text)
                return
            if not self._turn_has_tools:
                if (self.tts.speaking() and not STOP_RE.search(text)
                        and side_talk.looks_aside(text, WAKE_RE)):
                    # My answer is still playing and this line has no
                    # question, order or name: likely someone else's talk.
                    # Finish the answer first, then judge the line (bug
                    # 2026-10-18: side talk cut his answer mid-turn).
                    held = getattr(self, "_after_turn", "")
                    self._after_turn = f"{held}\n{text}" if held else text
                    return
                # He talked over a spoken answer: drop it, answer this.
                await self._cut_turn(text)
                return
            # Real work under way (tools running): fold it in silently so the
            # running turn sees it at its next step.
            if self.last_user_text:
                self.last_user_text += "\n" + text
            self.emit("work_add", text[:120])
            await self.client.query(text)
            return
        if self.claude_out and self._limit_expired():
            # The reset time has passed (or a probe is due): just try. If the
            # quota is still gone the turn fails again and sets it back.
            self.claude_out = False
            self.emit("claude", "ok")
        if self.claude_out:
            # Quota's gone. One brain now, so say it plainly — the reflex
            # lane (open, close, volume, media, time) still works without it.
            self.emit("work_fail", "Claude is out of usage"
                      + (f" — resets {self.claude_reset}" if self.claude_reset
                         else ""))
            self._say_now(
                ("Claude-ის ლიმიტი ამოიწურა" + (f", განახლდება {self.claude_reset}-ზე"
                 if self.claude_reset else "") + ". გახსნა, დახურვა და ხმა "
                 "ისევ მუშაობს.") if self.turn_lang == "ka" else
                ("I'm out of Claude usage" + (f" until about {self.claude_reset}"
                 if self.claude_reset else "") + ". Opening, closing, volume "
                 "and media still work."))
            return
        self.busy = True
        # The brain speaks its own answer as it writes it. A listening noise
        # covers the gap only if it is slow to start.
        if not getattr(self, "_stale_voice", False):
            # (With the last answer still playing, its reveal and label stay
            # until this reply proves real — see _drop_stale_voice.)
            self.tts.new_turn()
            self.emit("delta", "")
        self._first_said = False
        self._say_buf = ""
        # [[quiet]] detection: undecided until the first text arrives.
        self._quiet_state = None
        self._quiet_probe = ""
        self._probe_turn = bool(getattr(self, "_side_probe", False))
        self._side_probe = False
        # During a detected call nothing may make a sound unless it's a real
        # answer — not even the "Okay." filler. Normal mode keeps it.
        # A plain statement with no question/order/GOAT cue (the shape of
        # someone else's conversation) gets no filler either, so a [[quiet]]
        # reply to it stays fully silent.
        if not (self._probe_turn
                or time.monotonic() < getattr(self, "_side_until", 0.0)
                or side_talk.looks_aside(text, WAKE_RE)):
            asyncio.create_task(self._backchannel(self.tts.gen))
        self.last_user_text = text
        self._current_task = text
        self._work_started = time.monotonic()
        self._tl_reset()
        self._turn_has_tools = False
        self._last_tool = ""
        self._reply_acc = ""
        self._said_doing = False
        # Which brain (brain_route.py): talk on Sonnet 5.5, every order and
        # every serious question on his working brain — per turn, quietly.
        # 2026-10-16, his order: "when I give a task, Opus does it".
        now = time.monotonic()
        last_at = getattr(self, "_last_tier_at", 0.0)
        tier = brain_route.classify(text, hard, getattr(self, "_last_tier", None),
                                    now - last_at if last_at else None)
        self._last_tier, self._last_tier_at = tier, now
        # Only DEEP (code, planning, "hard") reaches the working brain;
        # talk and tasks stay on Sonnet (his order 2026-10-17). Decided here,
        # once, before anything is sent — a running turn is never switched.
        if BRAIN_ROUTING and tier != brain_route.DEEP:
            target = MODEL_TALK
        if self.model != target:
            try:
                await self.client.set_model(target)
                self.model = target
            except Exception as e:  # noqa: BLE001
                self.emit("status", f"model switch failed: {e}")
        name = _friendly_model_name(target)
        effort = self._turn_effort(text, hard, tier)
        print(f"[brain] {tier} -> {name} @ {effort}: {text[:60]!r}")
        self.emit("model", name)
        self.emit("brain", f"{name}|{tier}|{effort}")
        self.emit("work_start", f"{name}|{text}")
        # Bridge recent middle-lane chat so the working brain isn't blind to
        # what was just said out loud.
        # Sight: what is on his screen and running, captured as he spoke.
        try:
            view = await asyncio.to_thread(live_view.note)
        except Exception as e:  # noqa: BLE001
            view = f"[live desktop unavailable: {e}]"
        # Body (2026-10-20): only what changed since the last turn, or the
        # whole picture once per fresh session / when he asks how I am —
        # never a fixed block per turn (it would pile up in the history).
        try:
            full = (self.client is not self._body_client
                    or bool(body_sense.ASKED_RE.search(text)))
            body = self._body.note(full=full)
            self._body_client = self.client
        except Exception as e:  # noqa: BLE001 — never costs a turn
            body = ""
            print(f"[body] {type(e).__name__}: {e}")
        try:     # mood on change, thoughts from a quiet moment once
            inner = self._inner.turn_note(text)
            if inner:
                body = (body + "\n" + inner) if body else inner
        except Exception as e:  # noqa: BLE001
            print(f"[inner] {type(e).__name__}: {e}")
        send = view + ("\n" + body if body else "") + "\n\n" + text
        # Daily self-update results: he asked to hear only when something
        # actually changed — so it rides along on his next turn, once.
        news = self_update.take_notice()
        if news:
            send = ("[self-update since you last spoke — tell Giorgi in one "
                    "short line, then answer him]\n" + news + "\n\n" + send)
        if self._local_unseen:
            lines = "\n".join(f"him: {u}\nyou: {a}"
                              for u, a in self._local_unseen[-6:])
            send = ("[chat since your last turn — context only, do not reply "
                    "to it]\n" + lines + "\n\n" + send)
            self._local_unseen.clear()
        if self._pending_handoff:
            send = self._pending_handoff + "\n\n" + send
            self._pending_handoff = ""
        if self._cover_turn:
            # Two brains, voice down: the worker persona reports ABOUT him to
            # the voice — 2026-10-24 it answered "I added a sentence saying
            # he's living in Germany…" 41 s late. This turn it IS the voice.
            send = COVER_NOTE + send
        if self.turn_lang == "ka":
            # The work client's persona is pinned at connect and a live
            # language switch only steers the TALK lane — so the left lane
            # is told per dispatch. Without this he gets Georgian in the
            # middle and English on the left in the same breath.
            send = KA_WORK_NOTE + send
        if self._probe_turn:
            send = side_talk.PROBE_NOTE + send
        cut = getattr(self, "_cut_note", None)
        self._cut_note = None
        if cut is not None and time.monotonic() - cut[0] < CUT_IN_S:
            send = ("[he talked over you, so you stopped mid-answer"
                    + (f'; the last thing he heard: "{cut[1][:200]}"'
                       if cut[1] else "")
                    + ". Nothing after that was spoken. Answer what he said "
                    "now; pick the old thread up only if he wants it]\n") + send
        if MODEL_Q_RE.search(text):
            send = (f"[this turn: {name} is answering — chosen automatically "
                    f"for a {tier} turn]\n") + send
        await self._route_effort(text, hard, tier)
        self._tl_prep = time.monotonic() - self._tl_t0
        await self.client.query(send)

    # ---- effort routing (2026-10-01, his order: "make GOAT answer fast …
    # route simple turns low and hard ones high") ----
    # Measured on a fork of his live session (42k context, same persona and
    # tools): conversational replies reached their first spoken text in
    # ~2.8s at low, ~3.8s at medium, ~4.7s at high; short factual and
    # trick questions were ~1-2s at every level, and every checkable answer
    # was right at every level. So talk and questions go LOW; work orders
    # keep his chosen depth. `/effort X` switches the live session in 0.1s
    # with no reconnect, but it re-caches the conversation, so it is sent
    # only when the kind of turn changes, never every turn.
    def _turn_effort(self, text: str, hard: bool, tier: str | None = None) -> str:
        """Talk -> QUICK_EFFORT, simple task -> TASK_EFFORT, deep -> his dial.
        The tier comes from brain_route — the same call that picks the model,
        so brain and depth never disagree (2026-10-01)."""
        ceiling = self.effort
        if not EFFORT_ROUTING or hard:
            return ceiling
        if tier is None:
            tier = brain_route.classify(text, hard)
        want = {brain_route.TALK: QUICK_EFFORT,
                brain_route.TASK: TASK_EFFORT}.get(tier, ceiling)
        # Never ABOVE his dial: if he set "low", quick stays low.
        if EFFORT_LEVELS.index(want) > EFFORT_LEVELS.index(ceiling):
            return ceiling
        return want

    async def _route_effort(self, text: str, hard: bool, tier: str | None = None):
        if self._warming or self._compacting or self._cut:
            return      # another result is due first — it would be mistaken
                        # for the command's echo
        if self.client is not getattr(self, "_effort_client", None):
            # A fresh client (boot, reopen, rotation, new chat) starts at
            # the effort it was built with.
            self._effort_client = self.client
            self._live_effort = (getattr(self._work_options, "effort", None)
                                 or self.effort)
            self._effort_late = 0   # an old session's echo never comes
        want = self._turn_effort(text, hard, tier)
        if want == self._live_effort:
            return
        fut = asyncio.get_running_loop().create_future()
        self._effort_cmd = fut
        try:
            await self.client.query(f"/effort {want}")
            await asyncio.wait_for(fut, 3.0)
            self._live_effort = want
        except Exception as e:  # noqa: BLE001 — a missed switch costs speed,
            print(f"[effort] switch to {want} failed: {e!r}")  # never the turn
            if isinstance(e, asyncio.TimeoutError):
                # The echo is still coming (2026-10-10, Germany, slow line:
                # it landed after 3s and was taken for the boot briefing's
                # own result — the turn ended at "0 steps"). _consume
                # swallows it when it arrives and counts the switch as made.
                self._effort_late = getattr(self, "_effort_late", 0) + 1
                self._effort_late_want = want
        finally:
            self._effort_cmd = None

    # ---- turn timeline (2026-10-01: "is there anything we can do to improve
    # GOAT's overall performance?") ----
    # The [latency] line only measures up to the first spoken word; a turn
    # that then took 40s in screenshots left no trace of where the time
    # went. Each model step is timed from message_start (first token, end),
    # and the gap between one step's end and the next step's start is the
    # tool run, named by the tools that step asked for. One [turn] line at
    # the end: total = prep + model + tools + other.
    def _tl_reset(self):
        self._tl_t0 = time.monotonic()
        self._tl_prep = 0.0
        self._tl_steps = []      # (first-token s, step s)
        self._tl_step_t0 = None
        self._tl_first = None
        self._tl_stop = None
        self._tl_pending = []    # tools the last step asked for
        self._tl_tools = []      # (names, s)
        self._tl_wait = None     # query sent -> first model step started

    def _tl_event(self, et: str, dtype: str = ""):
        now = time.monotonic()
        if et == "message_start" and self._tl_wait is None:
            # Everything before the first step: queueing behind another turn,
            # API retries, the CLI's own work. A live turn on 2026-10-01 had
            # 22.5s of unexplained "other" that this would have named.
            self._tl_wait = now - self._tl_t0 - self._tl_prep
        if et == "message_start":
            if self._tl_stop is not None and self._tl_pending:
                self._tl_tools.append(("+".join(self._tl_pending),
                                       now - self._tl_stop))
            self._tl_pending = []
            self._tl_step_t0, self._tl_first = now, None
        elif et == "content_block_delta":
            t0 = getattr(self, "_t_speech_end", 0.0)
            # Spoken text only: a thinking delta counted here billed the
            # model's silent deliberation (2-4 s) to the voice as
            # "token->audio" and pointed the hunt at TTS (2026-10-19).
            if (t0 and dtype == "text_delta"
                    and getattr(self, "_lat_token_ms", -1.0) < 0):
                self._lat_token_ms = (now - t0) * 1000
            if self._tl_first is None and self._tl_step_t0 is not None:
                self._tl_first = now - self._tl_step_t0
        elif et == "message_stop" and self._tl_step_t0 is not None:
            self._tl_steps.append((self._tl_first or 0.0, now - self._tl_step_t0))
            self._tl_step_t0 = None
            self._tl_stop = now

    def _tl_log(self):
        if getattr(self, "_tl_t0", None) is None:
            return
        total = time.monotonic() - self._tl_t0
        model = sum(d for _, d in self._tl_steps)
        tools = sum(s for _, s in self._tl_tools)
        wait = self._tl_wait or 0.0
        other = max(0.0, total - self._tl_prep - wait - model - tools)
        firsts = "/".join(f"{f:.1f}" for f, _ in self._tl_steps[:8])
        if len(self._tl_steps) > 8:
            firsts += "/…"
        slow = sorted(self._tl_tools, key=lambda x: -x[1])[:4]
        eff = getattr(self, "_live_effort", None) or self.effort
        line = (f"[turn] {total:.1f}s @{eff} = prep {self._tl_prep:.2f}s"
                f" + wait {wait:.1f}s"
                f" + model {model:.1f}s in {len(self._tl_steps)} step(s)"
                f" (first token {firsts or '-'}s) + tools {tools:.1f}s")
        if slow:
            line += " [" + ", ".join(f"{n} {s:.1f}s" for n, s in slow) + "]"
        line += f" + other {other:.1f}s · ctx {self._step_ctx // 1000}k"
        cr, cc = getattr(self, "_step_cache", (0, 0))
        line += f" (cache hit {cr // 1000}k, new {cc // 1000}k)"
        print(line)
        self._tl_t0 = None

    def _speak_delta(self, text: str):
        # [[quiet]] (side_talk.QUIET_TOKEN): the brain judged the line was not
        # for GOAT. Hold the opening text until it either IS the token (swallow
        # the whole reply, silently) or can no longer become it (speak it all).
        state = getattr(self, "_quiet_state", "speak")
        if state == "quiet":
            return
        if state is None:
            self._quiet_probe += text
            head = self._quiet_probe.lstrip()
            tok = side_talk.QUIET_TOKEN
            if head.startswith(tok):
                self._quiet_state = "quiet"
                return
            if not head or tok.startswith(head):
                return                       # still could be the token
            self._quiet_state = "speak"
            text, self._quiet_probe = self._quiet_probe, ""
            self._drop_stale_voice()
        if text.strip() and getattr(self.tts, "_lead", False):
            self.tts.say_lead(LEAD_WORD.get(self.turn_lang, LEAD_WORD["en"]))
        self.emit("delta", text)
        self._say_buf += text
        self._flush_sentences()

    def _drop_stale_voice(self):
        """The new reply is real (not [[quiet]]): only now does the previous
        answer, still playing, give way to it."""
        if getattr(self, "_stale_voice", False):
            self._stale_voice = False
            self.tts.cancel()
            self.tts.new_turn()
            self.emit("delta", "")

    async def _release_aside(self, text: str):
        """A line held while my answer played (see _work): judge it now."""
        for _ in range(300):              # a /compact may be running: wait
            if not self.busy:
                break
            await asyncio.sleep(0.2)
        if self.busy:
            return
        await self._talk(text, echo=False)

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
        # The first boundary AT OR PAST the minimum, not just the first comma:
        # "Eight, Sir, since multiplication goes first: 2 plus 6." used to fail
        # on "Eight," (too short) and wait for the whole sentence, and Edge's
        # time grows with length (0.47 s for "Ottawa." vs 2.3-3.4 s for a
        # full sentence, measured 2026-10-18).
        m = None
        for b in CLAUSE_BREAK_RE.finditer(self._say_buf):
            head = self._say_buf[:b.start() + 1]
            if len(head.strip()) >= FIRST_CLAUSE_MIN:
                m = b
                break
        if m is not None:
            self.tts.say(self._say_buf[:m.start() + 1])
            self._say_buf = self._say_buf[m.end():]
            self._first_said = True
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
            fut = getattr(self, "_effort_cmd", None)
            if fut is not None and not fut.done():
                # The local "/effort X" command's own echo ("Set effort level
                # to low…") — never spoken, never a turn end.
                if isinstance(msg, ResultMessage):
                    fut.set_result(True)
                    continue
                if isinstance(msg, AssistantMessage):
                    continue
            elif getattr(self, "_effort_late", 0) and _is_effort_echo(msg):
                if isinstance(msg, ResultMessage):
                    self._effort_late -= 1
                    if self.client is getattr(self, "_effort_client", None):
                        self._live_effort = self._effort_late_want
                continue
            if isinstance(msg, StreamEvent):
                ev = msg.event
                if getattr(self, "_tl_t0", None) is not None:
                    self._tl_event(ev.get("type", ""),
                                   ev.get("delta", {}).get("type", ""))
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta", {})
                    if (delta.get("type") == "text_delta"
                            and not self.suppressed and not self._compacting):
                        t = delta.get("text", "")
                        if t and self.two_brains and not self._cover_turn:
                            # Two brains: Claude's words are for the panel
                            # and the voice, never the speaker.
                            self.emit("work_text", t)
                        elif t:
                            self._speak_delta(t)   # one brain: it speaks
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
                # Each API step's own prompt size — the real context fill.
                # (ResultMessage.usage is SUMMED over every tool step, so a
                # 5-tool turn read ~5x too big and trimmed far too early.)
                su = getattr(msg, "usage", None) or {}
                step = ((su.get("input_tokens") or 0)
                        + (su.get("cache_read_input_tokens") or 0)
                        + (su.get("cache_creation_input_tokens") or 0))
                if step:
                    self._step_ctx = step
                    self._step_cache = (su.get("cache_read_input_tokens") or 0,
                                        su.get("cache_creation_input_tokens") or 0)
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        t = (block.text or "").strip()
                        if t and not self._compacting:
                            self._reply_acc += t + " "
                    elif isinstance(block, ToolUseBlock):
                        self._turn_has_tools = True  # this is a WORK turn now
                        self._last_tool = block.name
                        if (not self._compacting and not self._reply_acc
                                and not (self.two_brains and not self._cover_turn)
                                and not self._say_buf
                                and not getattr(self, "_first_said", False)
                                and not getattr(self, "_said_doing", False)
                                and getattr(self, "_quiet_state", "speak") != "quiet"
                                and self.turn_lang != "ka"):
                            line = doing_line(block)
                            if line:
                                # first tool, nothing said yet: tell him what
                                # is happening instead of going silent
                                self._said_doing = True
                                self.tts.say(line)
                        if getattr(self, "_tl_t0", None) is not None:
                            self._tl_pending.append(block.name.split("__")[-1])
                        self.emit("work_tool", _describe_tool(block))
            elif isinstance(msg, UserMessage):
                # Tool results: a run of failures is one of the voice's
                # "Claude looks stuck" signals.
                content = msg.content if isinstance(msg.content, list) else []
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        self._watch.on_tool_result(bool(block.is_error))
            elif isinstance(msg, SystemMessage):
                if msg.subtype == "init":
                    sid = (msg.data or {}).get("session_id")
                    if sid:
                        with open(SESSION_FILE, "w", encoding="utf-8") as f:
                            f.write(sid)
                elif not (msg.subtype in ENGINE_ROUTINE or (
                        msg.subtype == "status"
                        and (msg.data or {}).get("status") in ENGINE_ROUTINE)):
                    # Retries, compaction, anything else the CLI reports —
                    # the log is where a slow turn's "wait" gets its name.
                    print(f"[engine] {msg.subtype}: "
                          f"{json.dumps(msg.data, default=str)[:200]}")
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
                if self._cut:
                    # The turn he talked over or stopped — see _cut_turn.
                    self._finish_cut()
                    continue
                if self._warming:
                    # The muted warm-up — see _warm_brain. Nothing to say.
                    self._warming = False
                    self.suppressed = False
                    self.busy = False
                    self._reply_acc = ""
                    if not self.two_brains:
                        self._say_buf = ""   # with two brains that's the voice's
                    continue
                if self.two_brains and not self._cover_turn:
                    # Claude's job (or a follow-up of it) ended: its own
                    # handler, nothing on the speaking path below.
                    if await self._job_result(msg):
                        return True
                    continue
                self._cover_turn = False
                self.suppressed = False
                self.busy = False
                self._tl_log()
                quiet_turn = False
                qstate = getattr(self, "_quiet_state", "speak")
                if qstate is None and self._quiet_probe:
                    # Ended while still looking like the token's start
                    # ("[[qu…") — it wasn't the token; say what it was.
                    self._quiet_state = "speak"
                    held, self._quiet_probe = self._quiet_probe, ""
                    self._speak_delta(held)
                    qstate = "speak"
                if qstate == "quiet" and not msg.is_error:
                    # Not for GOAT: nothing spoken, shown or logged, and the
                    # local side-talk gate takes over (side_talk.py).
                    self._quiet_state = "speak"
                    self._reply_acc = ""
                    self._say_buf = ""
                    self._side_until = time.monotonic() + side_talk.SIDE_QUIET_S
                    print(f"[side-talk] brain stayed quiet: {self.last_user_text!r}")
                    self.emit("reply_drop", "")
                    self.emit("status", "on a call? say \"GOAT\" to talk to me")
                    quiet_turn = True
                self._quiet_state = "speak"   # never leaks into a later turn
                self._stale_voice = False     # quiet/empty: the old answer plays on
                held, self._after_turn = getattr(self, "_after_turn", ""), ""
                if held:
                    asyncio.create_task(self._release_aside(held))
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
                self._plan_task = asyncio.create_task(self._pull_plan_usage())
                if self._track_usage(msg):
                    # Quota gone — mark it, show it on the left, keep Gemini
                    # talking in the middle (his rule 4). No retry.
                    self.claude_out = True
                    self._work_failed = True
                    self._last_work_summary = ("it hit Claude's usage limit "
                                               "mid-job; the job waits for "
                                               "the reset")
                    self._arm_resume()
                    self.emit("work_fail", "Claude ran out of usage"
                              + (f" — resets {self.claude_reset}"
                                 if self.claude_reset else "")
                              + ". I can still talk.")
                    self.emit("work_done", "")
                elif (msg.is_error and not self._reply_acc.strip()
                      and BRAIN_ROUTING and self.model == MODEL_TALK
                      and self.last_user_text
                      and getattr(self, "_handoff_done_for", None)
                      != self.last_user_text):
                    # 2026-10-18, his order ("make the escalation perfect"):
                    # the quick brain failed an order with nothing produced —
                    # retry it ONCE on the working brain before reporting.
                    retry = self.last_user_text
                    self._handoff_done_for = retry
                    self.emit("work_step", "quick brain failed — handing to the working brain")
                    asyncio.create_task(self._handoff_retry(retry))
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
                    self._resume_text = ""
                    self._work_failed = False
                    self._last_work_summary = self._reply_acc.strip()[-250:]
                    # Turn done — log the exchange for future handoffs and
                    # measure how heavy this session has become.
                    if self.last_user_text and not quiet_turn:
                        reply = self._reply_acc.strip()
                        self._exchanges.append(
                            (self.last_user_text[:300], reply[:300]))
                        if not self.last_user_text.startswith("[boot-briefing]"):
                            self._log_exchange(self.last_user_text, reply)
                        # The reply was spoken as it streamed; say the tail.
                        self._flush_sentences(force=True)
                        self._reply_acc = ""
                    self.emit("work_done", "")
                    u = msg.usage or {}
                    self._last_ctx = self._step_ctx or (
                        (u.get("input_tokens") or 0)
                        + (u.get("cache_read_input_tokens") or 0)
                        + (u.get("cache_creation_input_tokens") or 0))
                    self._step_ctx = 0
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
        if self._reopen_only or self._rotate_only:
            # _apply_effort / _new_chat closed the client on purpose.
            return True
        return False

    async def _job_result(self, msg: ResultMessage) -> bool:
        """End of one of Claude's turns in TWO_BRAINS mode. The outcome goes
        to the watch, which has the voice tell him. Returns True when run()
        must reconnect (context full / rotation)."""
        silent = self._job_silent
        self.suppressed = False
        self.busy = False
        self._tl_log()
        err = str(getattr(msg, "result", "") or "")
        low = err.lower()
        report = self._reply_acc.strip() or (err if not msg.is_error else "")
        self._reply_acc = ""
        self._work_done_at = time.monotonic()
        self._plan_task = asyncio.create_task(self._pull_plan_usage())
        if not silent:
            # A turn nobody dispatched: Claude answering a note that arrived
            # just after its job ended. Its words still reach the voice.
            if report and not msg.is_error:
                self._watch.start("(follow-up to the voice's last note)", "",
                                  self._worker_label(), "deep")
                self._worker_finished("done", report)
            return False
        if msg.is_error and "prompt is too long" in low:
            try:
                os.remove(SESSION_FILE)
            except OSError:
                pass
            self._pending_handoff = self._handoff_text()
            self._retry_job = self._job_args
            self._job_silent = False
            self.emit("work_step", "context full — fresh session, retrying")
            return True
        if (msg.is_error and self.model == MODEL_FABLE
                and ("credit" in low or "fable" in low)):
            # Fable refused (no usage credits): Opus 5.5 takes the same job.
            self.fable.refused("needs usage credits")
            self._job_silent = False
            self.emit("work_step", "Fable 5.1 needs usage credits — Opus 5.5 takes the job")
            self._emit_brains()
            if self._job_args:
                asyncio.create_task(self._work_job(*self._job_args))
            return False
        if self._track_usage(msg):
            self.claude_out = True
            self._work_failed = True
            self._arm_resume()
            self.emit("work_fail", "Claude ran out of usage"
                      + (f" — resets {self.claude_reset}" if self.claude_reset else ""))
            self._emit_brains()
            self._worker_finished("limit", report)
            return False
        if msg.is_error and not report:
            self._work_failed = True
            self._last_work_summary = f"it errored: {err[:150]}"
            self.emit("work_fail", f"Claude error: {err[:120]}")
            self._worker_finished("failed", err)
            return False
        if self.claude_out:
            self.claude_out = False
            self.emit("claude", "ok")
            self._emit_brains()
        self._resume_text = ""
        self._work_failed = False
        self._last_work_summary = report[-250:]
        task = (self._job_args or ("", "", ""))[0]
        self._exchanges.append((f"[job for Claude] {task[:250]}", report[:300]))
        self.emit("work_done", "")
        self._worker_finished("done", report)
        u = msg.usage or {}
        self._last_ctx = self._step_ctx or (
            (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
            + (u.get("cache_creation_input_tokens") or 0))
        self._step_ctx = 0
        self.emit("work_ctx", f"{self._last_ctx}|{ROTATE_CTX}")
        if self._last_ctx > ROTATE_CTX:
            # Rotation is free and instant; the next job carries the handoff
            # (recent conversation and job reports from _exchanges).
            self._rotate_only = True
            self._last_ctx = 0
            try:
                os.remove(SESSION_FILE)
            except OSError:
                pass
            self._pending_handoff = self._handoff_text()
            self.emit("status", "context rotated — usage saved")
            return True
        return False

    async def _handoff_retry(self, text: str):
        """Re-run a failed quick-brain order on the working brain (once)."""
        await asyncio.sleep(0.4)       # let the failed turn finish closing
        try:
            await self._work(text, hard=True, echo_you=False)
        except Exception as e:  # noqa: BLE001
            print(f"[handoff] retry failed: {e!r}")

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

    async def _pull_plan_usage(self) -> None:
        """Plan meter (5-hour / weekly) from the CLI's get_usage control
        request: no prompt, no tokens. Throttled to once a minute; never
        raises. Emits "plan" -> UI footer and writes .plan_usage.json."""
        now = time.monotonic()
        if now - getattr(self, "_plan_at", -999) < 60:
            return
        self._plan_at = now
        try:
            r = await asyncio.wait_for(
                self.client._query._send_control_request(
                    {"subtype": "get_usage"}), 8)
            rl = (r or {}).get("rate_limits") or {}
            fh, sd = rl.get("five_hour") or {}, rl.get("seven_day") or {}

            def _local(iso):
                try:
                    d = datetime.datetime.fromisoformat(iso).astimezone()
                    return d.strftime("%H:%M")
                except Exception:  # noqa: BLE001
                    return ""
            a, b = fh.get("utilization"), sd.get("utilization")
            if a is None and b is None:
                return
            out = {"five_hour": a, "five_hour_resets": _local(fh.get("resets_at")),
                   "seven_day": b, "seven_day_resets": _local(sd.get("resets_at")),
                   "plan": (r or {}).get("subscription_type"),
                   "at": datetime.datetime.now().isoformat(timespec="seconds")}
            self._plan_now = out          # the voice sees it (_front_parts)
            with open(os.path.join(os.path.dirname(SESSION_FILE),
                                   ".plan_usage.json"), "w",
                      encoding="utf-8") as f:
                json.dump(out, f)
            self.emit("plan", f"{round(a or 0)}|{round(b or 0)}|"
                              f"{out['five_hour_resets']}")
        except Exception as e:  # noqa: BLE001
            print(f"[plan-usage] {type(e).__name__}: {str(e)[:120]}")

    async def _plan_watch(self) -> None:
        """Keep the plan meter live. It used to refresh only after a work
        job finished, so the phone/footer showed a number minutes old (57
        while Claude itself said 64, 2026-10-17). get_usage costs no tokens,
        so poll it every minute regardless of what I'm doing."""
        await asyncio.sleep(5)
        while True:
            try:
                await self._pull_plan_usage()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(60)

    @staticmethod
    def _reset_epoch(text: str) -> float:
        """When the quota comes back, as a time.time() epoch; 0 if unknown.
        Handles the old "|<unix>" form and "resets 2:30am (Asia/Tbilisi)" —
        the zone in the message wins over the laptop's own clock."""
        m = re.search(r"\|(\d{9,11})", text)
        if m:
            return float(m.group(1))
        m = CLAUDE_RESET_RE.search(text)
        if not m:
            return 0.0
        h = int(m.group(1)) % 24
        mnt = int(m.group(2) or 0)
        ap = (m.group(3) or "").lower()
        if ap == "pm" and h != 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        tz = None
        z = re.search(r"\(([A-Za-z_]+/[A-Za-z_/]+)\)", text)
        if z:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(z.group(1))
            except Exception:  # noqa: BLE001 — no tzdata: fall back to local
                tz = None
        now = datetime.datetime.now(tz)
        t = now.replace(hour=h, minute=mnt, second=0, microsecond=0)
        if t <= now:
            t += datetime.timedelta(days=1)
        return t.timestamp()

    def _limit_expired(self) -> bool:
        """True once the quota is probably back: the reset time passed, or (no
        usable reset time) 20 minutes since the limit hit. A wrong guess costs
        one failed turn, never a stuck state."""
        now = time.time()
        if self.claude_reset_at:
            return now >= self.claude_reset_at
        return now - self._limit_hit_at >= 20 * 60

    def _arm_resume(self):
        """The limit cut a job off: remember it and wake up at the reset to
        carry on from where it stopped — no restart, no re-asking."""
        task = (self._current_task or "").strip()
        if task and not task.startswith("[auto-resume]"):
            self._resume_text = task
        old = self._resume_watch
        if old is not None and not old.done() \
                and old is not asyncio.current_task():
            old.cancel()
        self._resume_watch = asyncio.create_task(self._resume_after_reset())

    async def _resume_after_reset(self):
        try:
            while True:
                if self.claude_reset_at:
                    wait = self.claude_reset_at - time.time() + 45
                else:
                    wait = 20 * 60
                await asyncio.sleep(max(30.0, wait))
                for _ in range(120):          # let a running turn finish
                    if not self.busy:
                        break
                    await asyncio.sleep(5)
                if not self._resume_text:
                    return                    # a newer turn already landed
                self.claude_out = False
                self.emit("claude", "ok")
                task = self._resume_text
                print(f"[limit] quota window over — resuming: {task[:80]!r}")
                resume = ("[auto-resume] Claude's usage limit just reset and cut you "
                          "off mid-job. Continue exactly where you left off on this "
                          "order — check what is already done, don't redo it:\n"
                          + task)
                if self.two_brains:
                    self._say_now("Sir, Claude's usage is back. It's picking up "
                                  "where it left off.")
                    await self._work_job(resume, "", "deep")
                else:
                    self._say_now("Usage is back. Picking up where I left off.")
                    await self._work(resume)
                if not self.claude_out:
                    return                    # it ran; done
                # Still limited: _track_usage re-armed a fresh watcher.
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[limit] resume failed: {e}")

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
            self._limit_hit_at = time.time()
            self.claude_reset_at = self._reset_epoch(text)
            self.emit("claude", "out|" + reset)  # UI meter
            # Spoken heads-up comes through the talk lane's voice (once).
            # NEVER the raw CLI error — GOAT's own words only.
            # One brain since 2026-10-16: with Claude out, only the reflex
            # lane works — the old "questions, web, files, all of it" was a
            # promise GOAT could no longer keep.
            warn = (("ბატონო, Claude-ის ლიმიტი ამოიწურა"
                     + (f", განახლდება {reset}-ზე" if reset else "")
                     + ". მანამდე მხოლოდ სწრაფი ბრძანებები მუშაობს: გახსნა, "
                     "დახურვა, ხმა და მედია.") if self.turn_lang == "ka" else
                    ("Sir, I've hit Claude's usage limit"
                     + (f"; it resets around {reset}" if reset else "")
                     + ". Until then only quick commands work: opening, "
                     "closing, volume and media."))
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

    async def _update_watch(self):
        """Once per 24h: check engine / CLI / models (self_update.py). A
        pending engine update needs a restart (the bundled CLI is locked while
        I run), so it waits until he's been quiet for UPDATE_IDLE_S and then
        restarts through restart-goat.ps1 — preflight, install, watchdog."""
        await asyncio.sleep(60)
        self_update.boot_ok()   # survived a minute: this engine is keeper
        while True:
            try:
                if self_update.due():
                    await asyncio.to_thread(self_update.check)
                if (self_update.pending() and not self.busy
                        and not self.audio.is_tts_playing
                        and time.monotonic() - self._work_started
                        > UPDATE_IDLE_S):
                    self.emit("status", "updating my engine — back in a moment")
                    # WMI-spawned, same as the restart protocol: the helper
                    # must outlive the process it is about to kill.
                    helper = os.path.join(os.path.dirname(
                        os.path.abspath(__file__)), "restart-goat.ps1")
                    await asyncio.to_thread(subprocess.run, [
                        "powershell", "-NoProfile", "-Command",
                        "Invoke-CimMethod -ClassName Win32_Process -MethodName "
                        "Create -Arguments @{ CommandLine = 'powershell "
                        "-NoProfile -ExecutionPolicy Bypass -File "
                        f"{helper}' }}"],
                        capture_output=True, timeout=60,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    return
            except Exception as e:  # noqa: BLE001 — never let this hurt me
                self.emit("status", f"update check failed: {e}"[:80])
            await asyncio.sleep(1800)

    async def _warm_up(self):
        """Cold-start guard (bug #3): the canceller has never seen this room
        at process start — play a scripted line with interrupt decisions
        disabled so it can adapt before anything can false-trigger."""
        self.emit("status", "learning the room — one moment")
        tts_edge.prime()
        samples = await asyncio.to_thread(self.tts.synth,
                                          _greeting(self.turn_lang))
        self.audio.warming_up = True
        self.audio.queue_playback(samples)
        while self.audio.is_tts_playing:
            await asyncio.sleep(0.1)
        self.audio.warming_up = False

    async def _warm_on_first_reply(self):
        """Quiet boot has no scripted greeting for the echo canceller to
        learn the room on, so his first reply does that job: barge-in stays
        off until it has finished playing (see _warm_up)."""
        self.audio.warming_up = True
        while not self.audio.is_tts_playing:
            await asyncio.sleep(0.2)
        while self.audio.is_tts_playing:
            await asyncio.sleep(0.1)
        self.audio.warming_up = False

    async def _start_phone(self):
        """Phone client (home Wi-Fi / tailnet). Failure leaves GOAT intact."""
        try:
            import phone_server
            self.phone = phone_server.PhoneServer(self)
            await self.phone.start()
        except Exception as e:  # noqa: BLE001
            self.phone = None
            print(f"[phone] not started: {e}")

    async def run(self):
        self.loop = asyncio.get_running_loop()
        asyncio.create_task(self._start_phone())
        # Away-time, read BEFORE this boot's init overwrites the session
        # file: its mtime is when the LAST session was live.
        away_h = None
        try:
            away_h = (time.time() - os.path.getmtime(SESSION_FILE)) / 3600
        except OSError:
            pass  # no session file — fresh brain, greeting alone covers it
        try:
            # Two brains: talk with the voice never touches Claude's session
            # file; the transcript is the truer "last time we spoke".
            talked = (time.time() - os.path.getmtime(TRANSCRIPT_FILE)) / 3600
            away_h = talked if away_h is None else min(away_h, talked)
        except OSError:
            pass
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
        base_prompt = VOICE_BASE_PROMPT
        boot_model = (MODEL_TALK if BRAIN_ROUTING
                      else WORK_BRAINS.get(self.work_model, MODEL_FULL))
        servers = {"screen": screen_tools.SERVER}
        if self.two_brains:
            # Claude is the working brain only: its prompt drops the voice
            # rules, it opens on its highest usable model, and it gets a line
            # to the voice (tell_voice). Reports are English; the voice speaks
            # his language.
            persona = WORKER_PERSONA
            base_prompt = WORKER_BASE_PROMPT
            boot_model = self._worker_target()
            if talk_brain.VOICE_SERVER is not None:
                servers["voice"] = talk_brain.VOICE_SERVER
        options = ClaudeAgentOptions(
            cwd=WORKSPACE,
            permission_mode="bypassPermissions",
            # Most turns are talk, so the session opens on the talk brain and
            # the muted warm-up primes ITS prompt cache; the first deep turn
            # pays its own cold prefill, hidden inside a slow answer anyway.
            model=boot_model,
            # Every current model thinks adaptively; effort is the dial, and
            # he asked for the top of it. display="summarized" is what makes
            # the reasoning visible on the left instead of a silent gap.
            effort=self.effort,
            thinking=THINKING_CFG,
            # If the picked brain can't serve (Fable's separate credit
            # bucket, or an overload), the turn lands on Opus 5 instead of
            # dying. Without this a credit-less Fable pick kills work.
            fallback_model=(MODEL_FULL if boot_model == MODEL_FABLE else MODEL_PREV),
            # Custom short prompt instead of the claude_code preset (his order
            # 2026-10-17: the preset is ~10k+ tokens of coding-CLI guidance a
            # voice assistant never uses). Persona rides after it unchanged.
            system_prompt=base_prompt + "\n" + persona,
            include_partial_messages=True,
            # "project" = ONLY workspace/.claude — GOAT's own skill library.
            # Giorgi's global plugins/hooks stay out (the latency win that
            # setting_sources=[] originally bought is preserved).
            setting_sources=["project"],
            # Eyes and hands on the screen itself, in-process: `computer` and
            # `browser` land in the same tool list as Bash and Read, called the
            # same way, with no extra process or port (2026-09-14). Everything
            # that only exists as pixels was unreachable before this.
            mcp_servers=servers,
            # Only the in-process screen server: the claude.ai account
            # connectors (Figma, Gmail, Docs...) add thousands of tokens of
            # deferred-tool listings to every prompt and are never voice work.
            strict_mcp_config=True,
            # Slim tool list (his order 2026-10-17: 44k of context is too
            # much for a voice turn). Every tool schema rides in every
            # prompt; the agent/cron/worktree/notebook/todo families are
            # never used by voice work. MCP `screen` tools are unaffected.
            tools=VOICE_TOOLS,
            # 2026-10-17: only GOAT's own skill library is advertised — the
            # engine's bundled skills (claude-api, code-review, dataviz,
            # schedule, loop...) added ~2-3k tokens of descriptions per turn
            # and are never voice work. Names come from the folder, so a new
            # skill goes live without touching this line.
            skills=[d for d in sorted(os.listdir(
                        os.path.join(WORKSPACE, ".claude", "skills")))
                    if os.path.isfile(os.path.join(
                        WORKSPACE, ".claude", "skills", d, "SKILL.md"))],
            # A screenshot comes back as one JSON line of base64. The SDK's
            # default 1MB reader limit killed the brain on the first one
            # after the one-brain switch (2026-09-23: "Fatal error in message
            # reader ... exceeded maximum buffer size of 1048576 bytes").
            max_buffer_size=64 * 1024 * 1024,
            # 2026-10-01: "close this window" closed his live terminal via
            # CloseMainWindow. The brain's shells now pass the close guard.
            hooks=CLOSE_GUARD_HOOKS,
            resume=saved_session_id(),
        )
        self._work_options = options
        # Every mouse move and keystroke GOAT makes shows up on the left panel
        # as it happens. Screen actions are the one kind of work he can't read
        # back off disk afterwards, so they have to be visible while they run.
        screen_tools.set_emit(self.emit)
        self.model = options.model
        self.client = _new_client(options)
        connect_task = asyncio.create_task(self.client.connect())

        self.audio.start()
        self.emit("status", "calibrating — stay quiet for 2 seconds")
        await asyncio.to_thread(self.audio.calibrate, 2.0)
        if self.quiet_boot or self.reply_to == "phone":
            # phone was his device before the restart: no laptop greeting
            tts_edge.prime()
            asyncio.create_task(self._warm_on_first_reply())
        else:
            await self._warm_up()
        stt_ok = await stt_task
        await connect_task
        self_update.mark_engine_up()
        if not stt_ok:
            # Boot self-check, spoken: without this the window looks alive
            # while every word he says silently goes nowhere.
            self.emit("status", "HEARING OFFLINE — whisper-server did not start")
            self.emit("delta", "")  # creates the reply label for the reveal
            self.tts.say(
                "ყურადღება — სმენა ვერ ჩაირთო. სანამ გადამტვირთავ, "
                "ვერ გაგიგებ." if self.turn_lang == "ka" else
                "Heads up — my hearing did not come up. "
                "I can't transcribe you until you restart me.")
        else:
            self.emit("status", "listening — just talk")
        # Boot footer: the talking brain is the always-on voice, so the footer
        # shows it; the work brain shows on the left. It must name the brain he
        # actually PICKED — this line used to hardcode Gemini, so booting with
        # "sonnet 5" selected put a model name in the footer that was simply
        # not answering him. MODEL TRUTH applies to the chrome too.
        self.emit("talkmodel", self._voice_label() if self.two_brains
                  else _friendly_model_name(self.model))
        self.emit("model", _friendly_model_name(self.model))
        self._emit_brains()
        asyncio.create_task(self._watch_loop())
        asyncio.create_task(self._probe_fable())
        if self.two_brains:
            # ASCII only: GOAT's log is cp1252 and an arrow kills the engine.
            print(f"[brains] voice: {' > '.join(self.talker._models()) or 'none (all resting)'}; "
                  f"work: {self._worker_label()} — Fable 5.1 {self.fable.reason}",
                  flush=True)
            if self.live is not None:
                print(f"[brains] live voice: {live_talk.MODEL} first, REST voice "
                      "as its fallback", flush=True)
                asyncio.create_task(self.live.warm())
            voice_hands.warm()      # the voice's file index, built once at boot
        # This code just booted end to end — it IS the last-good version.
        # Snapshot it so a future bad self-edit always has a way back.
        threading.Thread(target=self_check.snapshot, daemon=True).start()

        # Pre-warm the Claude talk client if that is his talking brain: a
        # cold session measured 5.35s to first word vs 2.17s warm, and that
        # cold turn is always the first thing he says after a restart.
        self._prewarm_voice()
        threading.Thread(target=live_view.prime, daemon=True).start()

        if POWER_WATCH:
            asyncio.create_task(self._power_watch())
        asyncio.create_task(self._update_watch())
        asyncio.create_task(self._plan_watch())

        # Boot briefing (Phase 3, ported from the Node app 2026-07-10):
        # back after 6+ hours away → GOAT speaks first, JARVIS-style.
        if self.quiet_boot:
            # No open conversation window at login: only his name wakes me.
            self._last_exchange = time.monotonic() - WAKE_WINDOW_S - 1
            self._asleep = True
        elif away_h is not None and away_h >= BRIEFING_AFTER_H:
            now = datetime.datetime.now()
            await self._talk(
                "[boot-briefing] Greet him as Sir. Giorgi just started you after about "
                f"{away_h:.0f} hours away. It is {now:%A}, {now:%H:%M}.",
                echo=False)
        else:
            await self._warm_brain()   # the briefing already warms it

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
                    self.client = _new_client(options)
                    await self.client.connect()
                    self.model = options.model
                    self.busy = False
                    self.suppressed = False
                    self._hold_deltas = False
                    self._delta_buf = ""
                    if self._job_silent:
                        # The job died with the engine: the voice says so.
                        self._worker_finished(
                            "lost", f"GOAT's engine hiccuped ({type(e).__name__}) "
                            "and the job was cut off; it reconnected.")
                        self.emit("work_fail", "engine hiccup — job lost")
                    self.emit("talkmodel", self._voice_label() if self.two_brains
                              else _friendly_model_name(self.model))
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
                    self.client = _new_client(options)
                    await self.client.connect()
                    self.model = options.model
                    self.busy = False
                    self.suppressed = False
                    self.emit("status", f"thinking at {self.effort} — ready")
                    await self._warm_brain()
                    continue
                # Context full: fresh session, retry the wall-hit message.
                await self.client.disconnect()
                options.resume = None
                self.client = _new_client(options)
                await self.client.connect()
                self.model = options.model
                if self._rotate_only:
                    # Proactive rotation, not a wall hit: nothing to retry —
                    # the next work order carries the handoff.
                    self._rotate_only = False
                    retried_text = None
                    self.busy = False
                    self.emit("status", "fresh session — working brain ready")
                    await self._warm_brain()   # carries the handoff
                    continue
                self.emit("status", "fresh session — working brain ready")
                if self._retry_job:
                    # Two brains: Claude's job hit the context wall — run it
                    # again in the fresh session (once).
                    job, self._retry_job = self._retry_job, None
                    self.busy = False
                    if job[0] != retried_text:
                        retried_text = job[0]
                        await self._work_job(*job)
                    else:
                        self._watch.finish("failed", "the job is too big for one "
                                           "session — it filled Claude's context twice")
                    continue
                if self.last_user_text and self.last_user_text != retried_text:
                    retried_text = self.last_user_text
                    # Retry the work order that filled the context.
                    await self._work(self.last_user_text, echo_you=False)
        finally:
            self.shutdown_audio()
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
