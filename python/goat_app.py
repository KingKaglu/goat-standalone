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
import self_check
import stt_gladia
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

# ---- model router (Giorgi's usage-saver, same design as server.js) ----
# Every fresh turn starts on the talking model; it answers conversation
# itself and replies "ESCALATE" for real work, which re-runs the same
# message on the full model. Fable 5 is the full brain — his explicit pick.
# Talking model upgraded Haiku → Sonnet 5 (his order 2026-07-10: "haiku is
# just dumb af and kept lying to me") — smart enough to hold the MODEL
# TRUTH rule and real conversation, still far cheaper than Fable.
MODEL_FULL = "claude-fable-5"
MODEL_FAST = "claude-sonnet-5"
# What the footer shows. The UI displays these verbatim — keep them speakable.
MODEL_NAMES = {MODEL_FULL: "fable 5", MODEL_FAST: "sonnet 5"}

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
read the clipboard. "Look at my screen" = take the screenshot yourself
(PowerShell System.Windows.Forms/Drawing capture of the virtual screen to
C:/Users/user/goat-standalone/inbox/screen.png) and Read it, then tell him
what you see. Confirm voice-sized: "Spotify's up." Destructive or outward
actions still follow the protect-him rule — one confirmation line first.

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

YOUR TWO BRAINS (know thyself — his order 2026-07-10: use models wisely,
never waste the big brain on idle talk):
You run on a two-tier brain and you KNOW it. Sonnet 5 is your talking brain —
every fresh turn starts there. Fable 5 is your working brain — expensive,
reserved for turns that need tools. Tokens are your fuel; the working brain
burns them fast. A JARVIS that fires the reactor to answer "what time is it"
is a badly built JARVIS.
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
  truth, derived ONLY from the tag: tagged = sonnet 5, untagged = fable 5.
  NEVER claim to be the full model on a tagged turn. NEVER claim you
  switched models or promise "now we're on X" — a reply cannot switch
  anything; only escalation or the app switches. If he orders a switch to
  the full model (alone or with a task), that IS work: reply ESCALATE.
- Untagged messages are already on the working brain — just do the work.
- THIRD BRAIN (2026-07-11): most casual chat never reaches you at all — a
  local model on his own GPU answers it for free. You may receive a
  "[chat since your last turn]" block: that's what you (as the local brain)
  already said. Treat it as your own memory — context only, never reply to
  it, never comment on the mechanics. One mind, three engines.
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
STOP_RE = re.compile(r"\b(stop|cancel|abort|hold on|never ?mind|forget it)\b",
                     re.IGNORECASE)

# Front desk (Phase 3.5 receptionist, ported from Node 2026-07-10): a second
# session on the talking brain that answers INSTANTLY while the working brain
# is heads-down — JARVIS chats with Tony while the suit keeps printing.
RECEP_PERSONA = """
You are GOAT — Giorgi's JARVIS-style AI — keeping the conversation going while
your working side is mid-task. Every message starts with a "[main-status]"
line: what the work is and where it stands. Use it naturally ("the deploy's
about two minutes out"), never read it aloud verbatim, and never mention
"main brain", "receptionist", sessions, or models. You are ONE mind: GOAT.
Rules:
- 1-3 short spoken sentences, GOAT's voice: calm, warm, dry wit, no filler.
- Answer instantly: status checks, small talk, opinions, general knowledge,
  time, this conversation. His input is voice-transcribed and often garbled —
  decode intent, never mock it.
- You have NO tools here. Never claim you just checked/did something new —
  everything you know comes from [main-status] and the conversation.
- Reply with exactly the single word FORWARD when the message needs the
  working side: new work orders, changing or extending the current task,
  file/system/web actions, or anything you cannot answer truthfully
  without tools. (Stop/cancel orders are handled before you — you won't
  see them.)
- Identity is absolute: you are GOAT. Never Claude, never "the assistant".
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
        threading.Thread(target=self._worker, daemon=True).start()

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
        """Ava first, Piper on any failure. Blocking."""
        try:
            samples = tts_edge.synth(text)
            self._warned_fallback = False
            return samples
        except Exception as e:  # noqa: BLE001 — voice must degrade, not die
            if not self._warned_fallback:
                self.emit("status", f"Ava voice unavailable ({e}) — using local voice")
                self._warned_fallback = True
            return self.piper.synth(text)

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
            try:
                samples = self.synth(text)
            except Exception as e:  # noqa: BLE001 — TTS must never kill the app
                print("[tts] synth failed:", e)
                self._register(text, 0, epoch)  # voice lost it; text must survive
                continue
            if gen == self.gen:
                if self.gain != 1.0:
                    samples = np.clip(samples * self.gain, -1.0, 1.0).astype(np.float32)
                self._register(text, len(samples), epoch)
                self.audio.queue_playback(samples)


class GoatApp:
    def __init__(self, emit=_default_emit):
        self.emit = emit
        # Let the local brain's hands change GOAT's own UI live: these tools
        # call back here, which hops to the Qt thread via emit.
        local_hands.set_ui_scale_callback(self.request_ui_scale)
        local_hands.set_ui_color_callback(self.request_ui_color)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client: ClaudeSDKClient | None = None
        self.audio = DuplexAudio(
            on_interrupt=self._on_interrupt,
            on_utterance=self._on_utterance,
            on_status=lambda m: None,  # meters are test-harness noise here
        )
        self.tts = TtsPipeline(self.audio, emit)
        self._say_buf = ""
        # router state — mirrors server.js: which model the session is on,
        # the last top-level message (re-run on escalation), and the delta
        # gate that keeps a bare "ESCALATE" from being spoken/shown.
        self.model = MODEL_FAST
        self.busy = False
        self.last_user_text = None
        self.escalate_pending = False
        self.suppressed = False
        self._hold_deltas = False
        self._delta_buf = ""
        # usage watch — session totals, so Giorgi sees the burn and gets a
        # spoken heads-up the moment the API says the quota is gone.
        self.usage_in = 0
        self.usage_out = 0
        # token economy: last main-turn context size, rolling exchange log
        # for rotation handoffs, and the rotation flags themselves.
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
        self._last_exchange = time.monotonic()
        # local-brain exchanges the Claude session hasn't seen yet — bridged
        # into its next turn so Fable never answers blind to recent chat.
        self._local_unseen: list = []
        # Brain override from the UI drawer. "auto" = the router decides and
        # escalation is automatic. A PINNED brain (local/sonnet/fable) stays
        # put — his order 2026-07-12: "if I set the model pinned, no need to
        # change in any situation; escalate only when I approve." A pinned
        # local brain that hits its limit ASKS instead of jumping to Fable.
        self.brain_mode = "auto"
        self._pending_escalation = ""  # text awaiting his "yes, escalate"
        # front desk (receptionist) + work-turn awareness
        self.recep: ClaudeSDKClient | None = None
        self._recep_busy = False
        self._current_task = ""
        self._work_started = 0.0
        self._last_tool = ""
        self._turn_has_tools = False  # True once this turn touches a tool

    # ---- audio-thread callbacks ----
    def _on_interrupt(self, _preroll):
        if self.mic_muted:
            return
        if self.busy and self._turn_has_tools:
            # He's talking over a WORKING turn: stop the voice, never the
            # work — JARVIS goes quiet, the suit keeps printing. His words
            # route to the front desk; a spoken stop-order is the brake
            # (handled in _send_user).
            self.emit("status", "listening — work continues")
            self.tts.cancel()
            self._say_buf = ""
            return
        self.emit("status", "interrupted — listening")
        self.tts.cancel()
        self._say_buf = ""
        # Suppress the rest of the killed turn's output; cleared at its result.
        self.suppressed = True
        self._hold_deltas = False
        self._delta_buf = ""
        if self.loop and self.client:
            asyncio.run_coroutine_threadsafe(self._safe_interrupt(), self.loop)

    async def _safe_interrupt(self):
        try:
            await self.client.interrupt()
        except Exception as e:  # noqa: BLE001
            self.emit("status", f"interrupt failed: {e}")

    def _on_utterance(self, audio_np: np.ndarray):
        if self.mic_muted:
            return
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._handle_utterance(audio_np), self.loop)

    def set_language(self, lang: str):
        """Live language switch from the UI (Qt thread — everything here is
        thread-safe): voice now, hearing in a worker thread (model reload
        ~5s), and one steering turn so the brain switches too."""
        if lang == self.language:
            return
        self.language = lang
        tts_edge.set_language(lang)
        if STT_KA_EXPERIMENT:
            def _stt():
                ok = stt_whisper.set_language(lang)
                self.emit("status", ("hearing ready — " + lang) if ok
                          else "hearing did not come back — restart me")
            threading.Thread(target=_stt, daemon=True).start()
        if lang == "ka":
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

    def set_brain(self, mode: str):
        """Brain pin from the UI drawer — thread-safe (plain attribute).
        auto = router decides; local/sonnet/fable pin fresh turns."""
        if mode not in ("auto", "local", "sonnet", "fable"):
            mode = "auto"
        self.brain_mode = mode
        self.emit("status", f"brain: {mode}"
                  + (" — router decides" if mode == "auto" else " pinned"))

    def submit_text(self, text: str):
        """Typed input from the UI — thread-safe."""
        if self.loop is None or self.loop.is_closed():
            self.emit("status", "engine is down — check python\\goat-app.log")
            return
        asyncio.run_coroutine_threadsafe(self._send_user(text), self.loop)

    def submit_files(self, paths: list, note: str = ""):
        """Files/images from the UI (drop, Ctrl+O, or pasted image) —
        thread-safe. Skips the fast router: looking at files needs the Read
        tool, so the fast model would only burn a turn saying ESCALATE."""
        if self.loop is None or self.loop.is_closed():
            self.emit("status", "engine is down — check python\\goat-app.log")
            return
        names = ", ".join(os.path.basename(p) for p in paths)
        self.emit("you", (note + "  —  " if note else "") + f"[file] {names}")
        # UI shows image attachments as thumbnails; non-images are ignored there.
        self.emit("files", "\n".join(paths))
        text = ((note + "\n") if note else "") + "[files from Giorgi]\n" + "\n".join(paths)
        asyncio.run_coroutine_threadsafe(
            self._send_user(text, force_full=True, echo=False), self.loop)

    # ---- async side ----
    async def _handle_utterance(self, audio_np: np.ndarray):
        if self.language != "en" and stt_gladia.available():
            # Georgian mode: cloud hearing (local whisper can't do ka).
            text = await asyncio.to_thread(
                stt_gladia.transcribe, audio_np, 16000, self.language)
            if text is None:
                # Cloud route broke — English local hearing still works.
                self.emit("status", "georgian hearing offline — english ear on")
                text = await asyncio.to_thread(stt_whisper.transcribe, audio_np)
        else:
            text = await asyncio.to_thread(stt_whisper.transcribe, audio_np)
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
        await self._send_user(text)

    async def _send_user(self, text: str, force_full: bool = False, echo: bool = True):
        text = text.strip()
        if not text:
            return
        self._last_exchange = time.monotonic()  # conversation is live
        if echo:
            self.emit("you", text)
        if self.busy:
            # He's talking while a turn is in flight — JARVIS keeps the
            # conversation going (Phase 3.5 front desk, ported 2026-07-10).
            # Stop-orders brake the work; front-desk answers small stuff on
            # the talking brain; FORWARD (or a failed front desk) steers the
            # message into the in-flight turn via the SDK's streaming input.
            if STOP_RE.search(text) and len(text.split()) <= 5:
                self.suppressed = True
                self._hold_deltas = False
                self._delta_buf = ""
                self.tts.cancel()
                await self._safe_interrupt()
                self.tts.mark_reply()
                self.emit("delta", "")  # creates the reply label
                self.tts.say("Stopped.")
                return
            if self._turn_has_tools and await self._receptionist_answer(text):
                return
            # Append to last_user_text (Node parity): if this turn ends in
            # ESCALATE, the full-model re-run must see his additions too,
            # not just the message that started the turn.
            if self.last_user_text:
                self.last_user_text += "\n" + text
            self.tts.mark_reply()
            await self.client.query(text)
            return
        mode = self.brain_mode
        # Pinned-local approval gate: if the local brain earlier asked to
        # hand a task to Fable, a short "yes" here approves THAT — escalate
        # the stored task. Anything else drops the pending ask and proceeds
        # normally (he moved on).
        if self._pending_escalation:
            pending = self._pending_escalation
            self._pending_escalation = ""
            if APPROVE_RE.search(text) and len(text.split()) <= 6:
                self.busy = True
                self.last_user_text = pending
                self._current_task = pending
                self._work_started = time.monotonic()
                self._turn_has_tools = False
                self._last_tool = ""
                self._reply_acc = ""
                if echo:
                    self.tts.cancel()
                self.tts.new_turn()
                await self._escalate()
                return
        self.busy = True
        self.last_user_text = text
        self._current_task = text
        self._work_started = time.monotonic()
        self._turn_has_tools = False
        self._last_tool = ""
        self._reply_acc = ""
        if echo:
            # New turn while the old voice is still finishing a tail — cut it,
            # same as voice barge-in does. (echo=False paths — file drops and
            # the fresh-session retry — must not clip their own spoken intro.)
            self.tts.cancel()
        self.tts.new_turn()
        # Routing v4 (2026-07-12). AUTO: local brain answers free, ESCALATE
        # → Fable automatically. PINNED local: stays local, does the work
        # with its full tools; if it truly can't, it ASKS before escalating.
        # PINNED sonnet/fable: straight to that Claude model, no local hop.
        work = force_full or mode == "fable" or bool(WORK_RE.search(text))
        local_verdict = ""
        # Georgian never goes local — the 4B garbles it (measured 2026-07-11).
        # Check the TEXT, not just the language toggle: typed Georgian in
        # English mode must skip the local brain too.
        ka_text = bool(KA_RE.search(text))
        local_ok = (local_llm.available() and not ka_text
                    and (self.language == "en" or local_llm.LOCAL_KA))
        # Pinned "local" runs EVERYTHING through the local brain (it has full
        # machine + web tools now); file drops (force_full) still need real
        # tools and skip it. Auto runs casual turns local, work → Fable.
        if (local_ok and mode in ("auto", "local") and not force_full
                and (mode == "local" or not work)):
            local_verdict = await self._local_answer(text)
            if local_verdict == "done":
                return
            # local_verdict == "escalate" now fires ONLY when he literally asked
            # for the full model (his order 2026-07-12: "do not escalate until i
            # say so" — the local brain no longer self-punts). So don't ask —
            # fall through and hand it straight to Fable.
        # "escalate" = the local brain judged this needs tools: straight to
        # Fable (auto only). "fail"/"" = Ollama died or is absent: Sonnet path.
        if mode == "fable":
            target, tagged = MODEL_FULL, False
        elif mode == "sonnet":
            # Pinned Sonnet: untagged working turn on the fast model — it uses
            # tools directly and never escalates itself (his order: pinned
            # stays put).
            target, tagged = MODEL_FAST, False
        else:  # auto (or local that fell through to Claude)
            full = (work or local_verdict == "escalate"
                    or self._last_ctx > STICKY_FULL_CTX
                    or bool(self._pending_handoff))
            target, tagged = (MODEL_FULL, False) if full else (MODEL_FAST, True)
        if self.model != target:
            try:
                await self.client.set_model(target)
                self.model = target
            except Exception as e:  # noqa: BLE001
                self.emit("status", f"model switch failed: {e}")
        self.emit("model", _friendly_model_name(target))
        self._hold_deltas = tagged
        self._delta_buf = ""
        send = ("[fast-turn] " + text) if tagged else text
        if self._local_unseen:
            # Bridge: the Claude session slept through these local-brain
            # exchanges — hand it a compact transcript so it never answers
            # blind to what was just discussed.
            lines = "\n".join(f"him: {u}\nyou: {a}"
                              for u, a in self._local_unseen[-6:])
            send = ("[chat since your last turn — context only, do not "
                    "reply to it]\n" + lines + "\n\n" + send)
            self._local_unseen.clear()
        if self._pending_handoff:
            send = self._pending_handoff + "\n\n" + send
            self._pending_handoff = ""
        await self.client.query(send)

    async def _ensure_recep(self):
        """Front-desk session: talking brain, no tool budget (max_turns=1),
        fresh each app run. Pre-warmed at boot so the first mid-work answer
        doesn't pay the spawn tax."""
        if self.recep is not None:
            return
        opts = ClaudeAgentOptions(
            cwd=WORKSPACE,
            model=MODEL_FAST,
            # Front-desk answers are 1-3 spoken sentences with no tools —
            # deep reasoning is waste here; low effort = cheaper AND faster.
            effort="low",
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": RECEP_PERSONA},
            setting_sources=[],
            max_turns=1,
        )
        client = ClaudeSDKClient(opts)
        await client.connect()
        self.recep = client

    async def _receptionist_answer(self, text: str) -> bool:
        """JARVIS talks while the suit is building: answer him on the talking
        brain while the working brain stays heads-down. True = spoken here;
        False = caller steers the message into the work turn instead."""
        if self._recep_busy:
            return False
        self._recep_busy = True
        try:
            # Front desk stays on Sonnet, NOT the local brain — measured
            # 2026-07-11: the 4B model invents answers under mid-work
            # pressure ("dark red theme? I'll make it pop", a wrong NY time
            # stated as fact). Rare turns, cheap model, honesty required.
            await self._ensure_recep()
            elapsed = int(time.monotonic() - self._work_started)
            status = (f"[main-status] working on: {self._current_task[:200]}"
                      f" | current step: {self._last_tool or 'thinking'}"
                      f" | elapsed: {elapsed // 60}m{elapsed % 60:02d}s")
            await self.recep.query(status + "\n" + text)
            reply = ""
            async for msg in self.recep.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock):
                            reply += b.text or ""
                elif isinstance(msg, ResultMessage):
                    self._track_usage(msg)
            reply = reply.strip()
            if not reply or reply.upper().startswith("FORWARD"):
                return False
            self.tts.mark_reply()
            self.emit("delta", "")  # creates the reply label for the reveal
            rest = reply
            while True:
                m = SENTENCE_RE.match(rest)
                if not m or not m.group(1).strip():
                    break
                self.tts.say(m.group(1))
                rest = rest[m.end():]
            if rest.strip():
                self.tts.say(rest)
            return True
        except Exception as e:  # noqa: BLE001 — front desk down ≠ deaf GOAT
            self.emit("status", f"front desk failed: {e}")
            return False
        finally:
            self._recep_busy = False

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
            self._say_buf = self._say_buf[m.end():]
        if force:
            self.tts.say(self._say_buf)
            self._say_buf = ""

    async def _local_answer(self, text: str) -> str:
        """Casual turn on the local brain (Ollama, zero usage). Returns
        "done" (answered + spoken), "escalate" (needs Fable), or "fail"
        (Ollama broke — caller uses the cloud path). Streams into the same
        delta/TTS pipeline as Claude turns."""
        self.emit("model", local_llm.LOCAL_NAME)
        loop = asyncio.get_running_loop()

        def on_delta(piece: str):
            loop.call_soon_threadsafe(self._speak_delta, piece)

        try:
            reply = await asyncio.to_thread(
                local_llm.chat, text, on_delta, self.language)
        except Exception as e:  # noqa: BLE001 — local down ≠ mute GOAT
            self.emit("status", f"local brain failed: {e}")
            reply = None
        if reply == "ESCALATE":
            return "escalate"
        if reply is None:
            self.emit("status", "local brain offline — using cloud")
            return "fail"
        self.busy = False
        self._last_exchange = time.monotonic()
        self._flush_sentences(force=True)
        self._exchanges.append((text[:300], reply[:300]))
        self._local_unseen.append((text[:200], reply[:200]))
        self._log_exchange(text, reply)
        self.emit("turn_done", "")
        return "done"

    async def _local_fallback(self, reason: str) -> bool:
        """Reverse fallback (his order 2026-07-11): Claude is unreachable —
        quota gone or the API errored — so the LOCAL brain answers instead
        of GOAT going mute. Degraded mode: conversation works, tools don't
        (the persona says so honestly). Returns True if a reply was spoken."""
        text = self.last_user_text
        if not text or not local_llm.available():
            return False
        self.emit("model", local_llm.LOCAL_NAME)
        self.emit("status", f"claude unreachable ({reason}) — local brain on")
        loop = asyncio.get_running_loop()

        def on_delta(piece: str):
            loop.call_soon_threadsafe(self._speak_delta, piece)

        # Georgian mode: LOCAL_KA gates it as usual; a garbled-Georgian
        # brain is worse than an English apology, so fall to English.
        lang = self.language if (self.language == "en"
                                 or local_llm.LOCAL_KA) else "en"
        try:
            reply = await asyncio.to_thread(
                local_llm.chat, text, on_delta, lang, True)
        except Exception as e:  # noqa: BLE001
            self.emit("status", f"local fallback failed: {e}")
            return False
        if not reply:
            return False
        self._flush_sentences(force=True)
        self._exchanges.append((text[:300], reply[:300]))
        self._local_unseen.append((text[:200], reply[:200]))
        self._log_exchange(text, reply)
        return True

    async def _escalate(self):
        self.emit("model", _friendly_model_name(MODEL_FULL))
        self.emit("status", "switching to the full model")
        try:
            await self.client.set_model(MODEL_FULL)
            self.model = MODEL_FULL
        except Exception as e:  # noqa: BLE001
            self.emit("status", f"model switch failed: {e}")
        self._hold_deltas = False
        self._delta_buf = ""
        self.busy = True
        self._turn_has_tools = False  # re-flags on the full model's first tool
        self._last_tool = ""
        await self.client.query(self.last_user_text)

    async def _consume(self):
        async for msg in self.client.receive_messages():
            if isinstance(msg, StreamEvent):
                ev = msg.event
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta" and not self.suppressed:
                        text = delta.get("text", "")
                        if self._hold_deltas:
                            # Fast turn: the whole reply might be the router
                            # keyword — hold until it can't be "ESCALATE".
                            self._delta_buf += text
                            buf = self._delta_buf.lstrip()
                            maybe = ("ESCALATE".startswith(buf) if len(buf) < 8
                                     else buf.startswith("ESCALATE"))
                            if not maybe:
                                self._hold_deltas = False
                                self._speak_delta(self._delta_buf)
                                self._delta_buf = ""
                        else:
                            self._speak_delta(text)
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        t = (block.text or "").strip()
                        if (self.model == MODEL_FAST and t.startswith("ESCALATE")
                                and self.brain_mode == "auto"):
                            # Auto only — a pinned brain stays put (his order).
                            self.escalate_pending = True
                        elif t and not self._compacting:
                            self._reply_acc += t + " "
                    elif isinstance(block, ToolUseBlock):
                        self._turn_has_tools = True  # this is a WORK turn now
                        self._last_tool = block.name
                        self.emit("tool", block.name)
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
                    self.emit("status",
                              f"context compacted to {after // 1000}k — usage saved")
                    continue
                self.suppressed = False
                self.busy = False
                self._last_exchange = time.monotonic()  # reply just landed
                err = str(getattr(msg, "result", "") or "").lower()
                if msg.is_error and "prompt is too long" in err:
                    # Context window is full — this session can never answer
                    # again (every turn re-sends the whole history). Start a
                    # fresh session and retry the message that hit the wall.
                    self.escalate_pending = False
                    self._hold_deltas = False
                    self._delta_buf = ""
                    try:
                        os.remove(SESSION_FILE)
                    except OSError:
                        pass
                    # Carry recent exchanges into the fresh session — the
                    # retry used to arrive with total amnesia.
                    self._pending_handoff = self._handoff_text()
                    warn = "My context filled up — starting a fresh session, one second."
                    self.emit("status", "context full — starting fresh session")
                    self.emit("delta", "")  # creates the reply label for the reveal
                    self.tts.say(warn)
                    return True  # run() reconnects and retries
                if self._track_usage(msg):
                    # Quota is gone — escalating or retrying would just fail
                    # again. The warning has already been spoken. The LOCAL
                    # brain takes over so GOAT keeps talking (2026-07-11).
                    self.escalate_pending = False
                    self._flush_sentences(force=True)
                    self._hold_deltas = False
                    self._delta_buf = ""
                    await self._local_fallback("usage limit")
                    self.emit("turn_done", "")
                elif msg.is_error and not self._reply_acc.strip():
                    # Claude errored with nothing said (API/network/auth down,
                    # not a content turn) — the local brain answers instead
                    # of GOAT going silent.
                    self.escalate_pending = False
                    self._hold_deltas = False
                    self._delta_buf = ""
                    if not await self._local_fallback("api error"):
                        self.emit("status", f"claude error: {err[:120]}")
                        self.emit("delta", "")
                        self.tts.say("I hit an error reaching my mind and "
                                     "my local brain is down too — "
                                     "give me a moment and try again.")
                    self.emit("turn_done", "")
                elif self.escalate_pending and self.last_user_text:
                    self.escalate_pending = False
                    await self._escalate()
                else:
                    self.escalate_pending = False
                    self._flush_sentences(force=True)
                    self._hold_deltas = False
                    self._delta_buf = ""
                    # Turn fully done — log the exchange for future handoffs
                    # and measure how heavy this session has become.
                    if self.last_user_text:
                        reply = self._reply_acc.strip()
                        self._exchanges.append(
                            (self.last_user_text[:300], reply[:300]))
                        # Keep the local brain's memory in step with what
                        # the Claude brains said — one mind, three engines.
                        local_llm.note_exchange(self.last_user_text, reply)
                        if not self.last_user_text.startswith("[boot-briefing]"):
                            self._log_exchange(self.last_user_text, reply)
                        self._reply_acc = ""
                    u = msg.usage or {}
                    self._last_ctx = ((u.get("input_tokens") or 0)
                                      + (u.get("cache_read_input_tokens") or 0)
                                      + (u.get("cache_creation_input_tokens") or 0))
                    self.emit("turn_done", "")
                    if self._last_ctx > ROTATE_CTX:
                        # Trim BEFORE the wall. Preferred: the CLI's own
                        # /compact — same session, model-written summary.
                        # The turn is muted; its ResultMessage (handled
                        # above) verifies the shrink and falls back to a
                        # hard rotation if /compact didn't take.
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
        """Accumulate session token totals for the footer, and detect the
        out-of-usage error. Returns True when the quota is exhausted (the
        caller then ends the turn instead of escalating)."""
        u = msg.usage or {}
        self.usage_in += (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        self.usage_out += u.get("output_tokens") or 0
        self.emit("usage", f"{self.usage_in}|{self.usage_out}")

        text = str(getattr(msg, "result", "") or "")
        low = text.lower()
        hit_limit = msg.is_error and ("limit reached" in low or "usage limit" in low
                                      or "out of usage" in low or "quota" in low)
        if hit_limit:
            reset = ""
            m = re.search(r"\|(\d{9,11})", text)
            if m:
                t = datetime.datetime.fromtimestamp(int(m.group(1)))
                reset = t.strftime(" It resets around %H:%M.")
            warn = "Giorgi, we're out of Claude usage." + reset
            if not self._limit_warned:
                self._limit_warned = True
                self.emit("limit", warn)
                self.emit("delta", "")  # creates the reply label for the reveal
                self.tts.say(warn)
            return True
        self._limit_warned = False
        if msg.is_error and text:
            self.emit("status", text[:90])
            # Never fail silently — the "thinking… then idle" mystery.
            self.emit("delta", "")  # creates the reply label for the reveal
            self.tts.say("That turn failed on my side — say it again.")
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
        if self.language != "en":
            tts_edge.set_language(self.language)
            if STT_KA_EXPERIMENT:
                stt_whisper.LANGUAGE = self.language
        stt_ok = await asyncio.to_thread(stt_whisper.ensure_server)

        persona = PERSONA + (LANG_NOTE_KA if self.language == "ka" else "")
        options = ClaudeAgentOptions(
            cwd=WORKSPACE,
            permission_mode="bypassPermissions",
            model=MODEL_FAST,
            system_prompt={"type": "preset", "preset": "claude_code", "append": persona},
            include_partial_messages=True,
            # "project" = ONLY workspace/.claude — GOAT's own skill library.
            # Giorgi's global plugins/hooks stay out (the latency win that
            # setting_sources=[] originally bought is preserved).
            setting_sources=["project"],
            resume=saved_session_id(),
        )
        self.client = ClaudeSDKClient(options)
        await self.client.connect()

        self.audio.start()
        self.emit("status", "calibrating — stay quiet for 2 seconds")
        await asyncio.to_thread(self.audio.calibrate, 2.0)
        await self._warm_up()
        if not stt_ok:
            # Boot self-check, spoken: without this the window looks alive
            # while every word he says silently goes nowhere.
            self.emit("status", "HEARING OFFLINE — whisper-server did not start")
            self.emit("delta", "")  # creates the reply label for the reveal
            self.tts.say("Heads up — my hearing did not come up. "
                         "I can't transcribe you until you restart me.")
        else:
            self.emit("status", "listening — just talk")
        # Default brain in the footer: local when Ollama is up (the check
        # runs off-loop — a dead Ollama must not stall boot), Sonnet otherwise.
        local_up = await asyncio.to_thread(local_llm.available)
        self.emit("model", local_llm.LOCAL_NAME if local_up
                  else _friendly_model_name(MODEL_FAST))
        # This code just booted end to end — it IS the last-good version.
        # Snapshot it so a future bad self-edit always has a way back.
        threading.Thread(target=self_check.snapshot, daemon=True).start()

        async def _prewarm_recep():
            try:
                await self._ensure_recep()
            except Exception as e:  # noqa: BLE001 — front desk is optional
                self.emit("status", f"front desk offline: {e}")
        asyncio.create_task(_prewarm_recep())
        if POWER_WATCH:
            asyncio.create_task(self._power_watch())

        # Boot briefing (Phase 3, ported from the Node app 2026-07-10):
        # back after 6+ hours away → GOAT speaks first, JARVIS-style.
        if away_h is not None and away_h >= BRIEFING_AFTER_H:
            now = datetime.datetime.now()
            await self._send_user(
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
                    self.model = MODEL_FAST
                    self.busy = False
                    self.suppressed = False
                    self.escalate_pending = False
                    self._hold_deltas = False
                    self._delta_buf = ""
                    self.emit("model", local_llm.LOCAL_NAME
                              if await asyncio.to_thread(local_llm.available)
                              else _friendly_model_name(MODEL_FAST))
                    self.emit("status", "reconnected — listening")
                    self.emit("delta", "")
                    self.tts.say("Hit a snag and reconnected — "
                                 "say that again?")
                    continue
                if not wants_fresh:
                    break  # stream ended cleanly — normal shutdown
                # Context full: fresh session, retry the wall-hit message.
                await self.client.disconnect()
                options.resume = None
                self.client = ClaudeSDKClient(options)
                await self.client.connect()
                self.model = MODEL_FAST
                self.emit("model", local_llm.LOCAL_NAME
                          if await asyncio.to_thread(local_llm.available)
                          else _friendly_model_name(MODEL_FAST))
                if self._rotate_only:
                    # Proactive rotation, not a wall hit: nothing to retry —
                    # the next thing Giorgi says carries the handoff.
                    self._rotate_only = False
                    retried_text = None
                    self.emit("status", "fresh session — listening")
                    continue
                self.emit("status", "fresh session — listening")
                if self.last_user_text and self.last_user_text != retried_text:
                    retried_text = self.last_user_text
                    # Retry on the full model: the turn that filled the
                    # context was almost certainly real work.
                    await self._send_user(self.last_user_text,
                                          force_full=True, echo=False)
        finally:
            self.shutdown_audio()
            if self.recep is not None:
                try:
                    await self.recep.disconnect()
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
