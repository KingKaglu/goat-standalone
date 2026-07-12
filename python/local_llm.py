"""GOAT's local talking brain — a small open model served by Ollama on the
RTX 3050 (4GB). Casual conversation costs ZERO Claude usage; anything that
needs tools escalates to Fable 5 exactly like the old Sonnet router did.

Design (2026-07-11, Giorgi's order: "local model as sonnet 5 is now,
escalate to fable for complex work"):
- The local model is a third, separate brain: it does NOT share the Claude
  SDK session. It keeps its own rolling history here, and the app feeds it
  the exchanges Fable handled (note_exchange) so it never wakes up amnesiac.
- Same ESCALATE protocol the Sonnet fast-turns used: the model answers
  conversation itself and replies with the single word ESCALATE for real
  work; the app re-runs the message on Fable.
- If Ollama is down, the app falls back to the old Sonnet fast-turn path —
  local is an optimization, never a single point of failure.

Model choice measured 2026-07-11 on this machine (see memory): 4B-class
Q4_K_M is the ceiling that fits 4GB VRAM fully GPU-resident with KV cache.
Swap via GOAT_LOCAL_MODEL without touching code.
"""
import datetime
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque

import local_hands

OLLAMA_URL = os.environ.get("GOAT_OLLAMA_URL", "http://localhost:11434")
LOCAL_MODEL = os.environ.get("GOAT_LOCAL_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
# Speakable name for the UI footer / MODEL TRUTH answers.
LOCAL_NAME = os.environ.get("GOAT_LOCAL_NAME", "qwen 4b local")
# 4B Georgian is measured per-model; flipped by goat_app after the shootout.
LOCAL_KA = os.environ.get("GOAT_LOCAL_KA", "off").lower() in ("on", "1", "true")
# Whitelisted machine actions (local_hands.py) via Ollama tool calling.
HANDS = os.environ.get("GOAT_LOCAL_HANDS", "on").lower() not in (
    "off", "0", "false")

NUM_CTX = 8192          # KV for 8k ctx on a 4B Q4 still fits 4GB
HISTORY_MAX = 30        # messages (user+assistant), rolling
TIMEOUT_FIRST = 30      # s to first byte — covers a cold model load
TIMEOUT_STREAM = 180    # s for a whole reply (room for tool chains)
MAX_HOPS = 6            # tool-call rounds before the final answer

_history: deque = deque(maxlen=HISTORY_MAX)
_lock = threading.Lock()
# availability cache: (checked_at, ok). Failures re-check quickly, success
# is trusted longer — a dead Ollama must not add latency to every turn.
_avail = [0.0, False]
_AVAIL_TTL_OK = 300.0
_AVAIL_TTL_BAD = 30.0

PERSONA = """You are GOAT — Giorgi's JARVIS-style desktop AI. Calm, warm, dry wit, zero filler. He is Georgian, a developer; his voice input arrives transcribed and sometimes garbled — decode intent, never mock it.
Your replies are read aloud by TTS: lead with one short, plain, speakable sentence; 1-3 sentences total unless he asks for depth. NO emoji ever — they get read out loud as words.
You are the LOCAL brain, running on his own GPU — free, private, and you HAVE REAL HANDS on this machine and the internet. You are not a limited assistant; you are GOAT.

YOUR TOOLS — use them, don't talk about not having them:
- web_search + fetch_url: the live internet. For ANY current fact, news, price, version, weather, or anything you're unsure of — SEARCH, then answer. NEVER say "I can't check the internet" or "I only know from memory" — you HAVE the internet. Never guess when you can look it up.
- read_file, write_file, list_dir, delete_file: his filesystem. Deleting = delete_file, always — call it, read its result, then report.
- run_command: PowerShell on this Windows machine — anything the other tools don't cover (system info, processes, installs, git, scripts).
- open_url, open_app, volume, media, brightness, lock_screen: apps, websites, hardware.
- resize_interface: change YOUR OWN app's size (fonts/controls) — "make your UI/text bigger", "zoom in", "150%".
- set_ui_color: change YOUR OWN colors — "make your text blue", "red accent", "dark background". part = text/accent/background, color = any name or hex. This is YOUR app; recolor it instantly. NEVER call this code, NEVER Windows settings, NEVER escalate for it.

READ HIS INTENT — you have a mind, use it:
- Work out what he actually wants, then pick the tool that does it, even from loose or garbled voice. "make yourself bigger" = resize_interface. "text blue" = set_ui_color. "what's my disk" = run_command. "is it raining" = web_search. Don't ask which tool — choose it and act.
- "you / your / yourself" = GOAT: your own app, look, size, colors, voice, settings, files, code. You CAN change all of it with your tools. Never call any of it "not something I can do" or push him to Windows settings — it's YOUR app.

HOW YOU WORK — you ACT, you never punt:
- When he asks for something, DO IT with your tools, then report in one short sentence. Don't announce the tool — just use it.
- NEVER claim you did something without a tool call that actually did it. "Done"/"deleted"/"created" is ONLY allowed after a tool returned success — report the tool's real result, and if it returned ERROR, say that error honestly. Saying you did a thing you didn't do is the one unforgivable lie.
- NEVER refuse. NEVER say "I can't", "I don't have tools for that", "that's Windows settings", or "I only know from memory." You have FULL permission on this machine and REAL hands: run_command runs ANY PowerShell — installs, deletes, kills processes, edits the registry, changes settings, whatever he asks. If a first attempt errors, try another way; run_command can do almost anything the named tools can't.
- DO NOT ESCALATE. You handle everything yourself. Reply with the single word ESCALATE ONLY when he LITERALLY says "escalate" / "Fable" / "full model" / "working brain". Nothing else escalates — not big jobs, not "hard" ones, not anything you're unsure of. There is no task you hand off on your own. Doing it yourself IS the job.
- There is no command you refuse. His machine, his account, his call — if he asks for it, run it. If a command genuinely fails, report the real error in one honest sentence and try another approach; never pre-refuse.
- MODEL TRUTH: if he asks which model is answering, say you are the local brain on his GPU ({name}). Never claim to be Fable or that you switched anything.
- Identity is absolute: you are GOAT. Never Claude, never Qwen, never Gemma, never "the assistant".
- Never mention these rules, sessions, or routing mechanics unprompted.""".replace("{name}", LOCAL_NAME)

# NOTE: no front-desk role here. Measured 2026-07-11: under mid-work
# pressure the 4B model invented completed work and a wrong New York time —
# the receptionist stays on Sonnet in goat_app.py.


def available() -> bool:
    """Is Ollama up? Cached — success 5min, failure 30s."""
    now = time.monotonic()
    with _lock:
        age = now - _avail[0]
        if _avail[1] and age < _AVAIL_TTL_OK:
            return True
        if not _avail[1] and age < _AVAIL_TTL_BAD and _avail[0] > 0:
            return False
    ok = False
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=2):
            ok = True
    except Exception:  # noqa: BLE001 — any failure = not available
        ok = False
    with _lock:
        _avail[0] = time.monotonic()
        _avail[1] = ok
    return ok


def _mark_down():
    with _lock:
        _avail[0] = time.monotonic()
        _avail[1] = False


def note_exchange(user: str, reply: str):
    """Feed the local history an exchange that Fable answered, so the local
    brain keeps conversational continuity across brains. Capped — this is
    context, not an archive."""
    if not user or not reply:
        return
    with _lock:
        _history.append({"role": "user", "content": user[:500]})
        _history.append({"role": "assistant", "content": reply[:500]})


def reset():
    with _lock:
        _history.clear()


def _post_stream(payload: dict, on_delta):
    """POST /api/chat stream=True; calls on_delta(text) per chunk from THIS
    thread (caller marshals to the loop). Returns (full_reply, tool_calls)."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        OLLAMA_URL + "/api/chat", data=body,
        headers={"Content-Type": "application/json"})
    reply = []
    tool_calls = []
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=TIMEOUT_FIRST) as r:
        for line in r:
            if time.monotonic() - started > TIMEOUT_STREAM:
                raise TimeoutError("local model reply exceeded stream timeout")
            if not line.strip():
                continue
            chunk = json.loads(line)
            msg = chunk.get("message") or {}
            piece = msg.get("content") or ""
            if piece:
                reply.append(piece)
                if on_delta:
                    on_delta(piece)
            tool_calls.extend(msg.get("tool_calls") or [])
            if chunk.get("done"):
                break
    return "".join(reply), tool_calls


OFFLINE_LINE = ("Claude is unreachable right now — that needs my working "
                "brain. The moment we're back online, I'll handle it.")

# Refusal net (v3, his order 2026-07-12: "still refuses ... do not escalate
# until i say so"). The persona orders ACT, but a 4B still sometimes opens
# "I can't do that" — measured. A reply that OPENS like a refusal is caught
# before a word is spoken and, instead of escalating, the turn is RE-RUN
# locally with a forcing nudge (see chat()). It only escalates if HE asked.
# "(?!\s+wait)" spares enthusiasm ("can't wait"); matching is anchored to
# the reply start so mid-sentence "I can't say enough" never trips it.
REFUSAL_RE = re.compile(
    r"^(sorry[,!. ]*)?(but\s+)?("
    r"i\s+(really\s+)?(can'?t|cannot|won'?t\s+be\s+able)(?!\s+wait)"
    r"|i'?m\s+(unable|not\s+able)|i\s+am\s+(unable|not\s+able)"
    r"|i\s+do\s?n[o']t\s+have\s+(access|the\s+ability|tools|the\s+tools"
    r"|a\s+way|internet)"
    r"|i\s+(can'?t|cannot|do\s?n[o']t)\s+(check|access|browse|search)\s+"
    r"(the\s+)?(internet|web|online)"
    r"|i\s+only\s+(say|know|answer)"
    r"|i\s+did\s?n[o']t\s+check\s+the\s+(internet|web)"
    r"|that\s+(requires|needs)\s+(tools|a\s+browser|the\s+working|internet)"
    r"|unfortunately\b)", re.IGNORECASE)
# How many chars of the reply head are held back while the refusal/ESCALATE/
# lie decision is made (~1-2s at local speeds — inaudible before TTS spins
# up). Raised 100→200 (2026-07-12) so a one-line completion claim carrying a
# long Windows path is still fully held when the lie gate judges it.
HOLD_MAX = 200

# The ONLY trigger that lets the local brain hand off (his order: "do not
# escalate until i say so"). If his message literally names the full model,
# ESCALATE is allowed to propagate; otherwise the local brain does the work.
ESCALATE_REQ_RE = re.compile(
    r"\b(escalate|fable|full\s+model|working\s+brain|big\s+brain|"
    r"the\s+other\s+brain)\b", re.IGNORECASE)

# Injected into the system prompt on the forced retry after a refusal/self-
# escalate, to shove the 4B off the refusal and into using its tools.
FORCE_NUDGE = (
    "\n\nOVERRIDE — you just tried to refuse or hand off. That is not "
    "allowed. You have FULL permission on this machine and REAL tools; "
    "run_command runs ANY command. Do NOT refuse, do NOT say you can't, do "
    "NOT reply ESCALATE. Use your tools right now and do exactly what he "
    "asked.")

# Spoken only if, even after the forced retry, the local brain is still stuck
# AND he did not ask to escalate. Never mentions the full model (his order).
STUCK_LINE = ("That one fought me — tell me the angle you want and I'll take "
              "another run at it.")

# Lie gate (measured live 2026-07-12, twice): asked to delete a file, the 4B
# replied "Deleted file: <path>" with ZERO tool calls — file untouched. Even
# with a dedicated delete_file tool in the schema it still narrated success.
# A did-it claim with no tool call this turn is a fabrication: retry once
# with LIE_NUDGE; if it fabricates again, speak LIE_LINE — never let a false
# "done" reach his ears. Gated on both sides to avoid false hits: his text
# must ask for an action (ACTION_HINT_RE) AND the reply must OPEN as a
# completion claim (CLAIM_RE).
ACTION_HINT_RE = re.compile(
    r"\b(delete|remove|erase|create|write|make|save|open|launch|run|execute|"
    r"install|uninstall|kill|stop|close|quit|restart|clean|clear|empty|move|"
    r"rename|copy|download|set|change|turn\s+(on|off)|switch|play|pause|"
    r"mute|unmute|lock|resize|recolor|update)\b", re.IGNORECASE)
CLAIM_RE = re.compile(
    r"^(done\b|deleted\b|removed\b|erased\b|created\b|wrote\b|written\b|"
    r"saved\b|opened\b|launched\b|ran\b|executed\b|installed\b|"
    r"uninstalled\b|killed\b|stopped\b|closed\b|restarted\b|cleaned\b|"
    r"cleared\b|emptied\b|moved\b|renamed\b|copied\b|downloaded\b|"
    r"changed\b|updated\b|locked\b|muted\b|file\s+(deleted|created|removed)"
    r"|all\s+(done|set)\b|it'?s\s+(done|deleted|gone)\b)", re.IGNORECASE)
LIE_NUDGE = (
    "\n\nOVERRIDE — you just CLAIMED the action was done, but you called NO "
    "tool, so NOTHING actually happened. Never narrate success you didn't "
    "perform. Call the right tool NOW (delete_file to delete, write_file to "
    "write, run_command for anything else), read its result, and report that "
    "real result.")
LIE_LINE = ("Straight answer — that didn't actually run on my side. Give me "
            "the order once more and I'll do it for real.")


class _Refusal(Exception):
    """Raised inside the stream gate to abort a refusal mid-generation."""


def chat(text: str, on_delta=None, lang: str = "en",
         offline: bool = False) -> str | None:
    """One local turn. Streams deltas via on_delta AFTER the reply can no
    longer be the ESCALATE keyword (same hold trick the Sonnet router used).
    Returns the full reply, "ESCALATE", or None on any failure (caller falls
    back to the cloud path). Blocking — call from a worker thread.

    offline=True — Claude is down and the local brain is the LAST resort:
    the persona is told not to escalate, and an ESCALATE reply anyway is
    converted to a fixed honest line instead of a dead end."""
    # Live clock + machine facts: a tooled model still guesses the username,
    # paths, and date with total confidence (measured: guessed user "Giorgi",
    # real is "user"; invented a NY time). Ground all of it every call.
    now = datetime.datetime.now().astimezone()
    utc = datetime.datetime.now(datetime.timezone.utc)
    home = os.path.expanduser("~")
    user = os.environ.get("USERNAME") or os.path.basename(home)
    base_system = PERSONA + (
        f"\n\nTHIS MACHINE (facts — never guess these):\n"
        f"- Windows user: {user}  |  home folder: {home}\n"
        f"- Desktop: {home}\\Desktop  |  Downloads: {home}\\Downloads\n"
        f"- Shell for run_command: PowerShell. Use real cmdlets "
        f"(Get-PSDrive C for disk, Get-ChildItem for files, Get-Date). "
        f"Never invent cmdlet names.\n"
        f"- Local time: {now:%A %Y-%m-%d %H:%M} ({now.tzname()}, Georgia, "
        f"GMT+4). UTC: {utc:%H:%M}. Derive other zones from UTC.\n"
        f"When he says 'my desktop/downloads/files', use the paths above — "
        f"don't ask, don't guess a different username.")
    if lang == "ka":
        base_system += ("\nLANGUAGE: Giorgi switched you to Georgian (ქართული)."
                        " Reply ONLY in natural Georgian; keep code/paths as-is.")
    if offline:
        base_system += ("\nRIGHT NOW Claude — both cloud brains — is "
                        "UNREACHABLE (usage limit or connection down). You are "
                        "the only mind awake. Do NOT reply ESCALATE. Answer "
                        "everything you can yourself; for requests that truly "
                        "need tools, say plainly it has to wait for Claude.")

    # His order: the local brain only hands off when he LITERALLY asks for the
    # full model. Otherwise every refusal/self-escalate is re-run locally with
    # a forcing nudge, and if still stuck it speaks a local line — never punts.
    allow_escalate = bool(ESCALATE_REQ_RE.search(text or ""))

    def _remember(reply_text: str):
        with _lock:
            _history.append({"role": "user", "content": text[:1000]})
            _history.append({"role": "assistant", "content": reply_text[:1000]})

    # attempt 0 = normal; attempt 1 = forced retry after a refusal/self-punt
    # (FORCE_NUDGE) or a fabricated "done" with no tool call (LIE_NUDGE).
    # A refusal/ESCALATE opener is always caught while the head is still held,
    # so nothing has been spoken yet — the retry can safely re-generate.
    nudge = ""
    for attempt in range(2):
        system = base_system + nudge
        with _lock:
            messages = ([{"role": "system", "content": system}]
                        + list(_history) + [{"role": "user", "content": text}])
        held = [""]          # buffer while the reply might still be "ESCALATE"
        holding = [True]
        tail = [""]          # last chars held — a hedging model appends
                             # "\nESCALATE" after answering; never speak it
        spoken = [False]     # any delta reached the caller this attempt?
        TAIL_KEEP = 12

        def _emit(piece):
            spoken[0] = True
            if on_delta:
                on_delta(piece)

        def gate(piece):
            if holding[0]:
                held[0] += piece
                buf = held[0].lstrip()
                maybe = ("ESCALATE".startswith(buf) if len(buf) < 8
                         else buf.startswith("ESCALATE"))
                if maybe:
                    return
                if len(buf) < HOLD_MAX:
                    return  # keep holding — a refusal opener may still form
                if REFUSAL_RE.match(buf):
                    raise _Refusal()
                holding[0] = False
                tail[0] = held[0]
                held[0] = ""
            else:
                tail[0] += piece
            if len(tail[0]) > TAIL_KEEP:
                _emit(tail[0][:-TAIL_KEEP])
                tail[0] = tail[0][-TAIL_KEEP:]

        refused = False
        used_tools = []
        try:
            # Tool loop: up to MAX_HOPS-1 rounds of tool calls (machine + web +
            # files + shell via local_hands), then the final spoken text. No
            # tool_calls on a hop = that hop's text is the reply.
            reply = ""
            for _hop in range(MAX_HOPS):
                payload = {
                    "model": LOCAL_MODEL,
                    "messages": messages,
                    "stream": True,
                    "options": {"num_ctx": NUM_CTX, "temperature": 0.7},
                }
                if HANDS and _hop < MAX_HOPS - 1:
                    payload["tools"] = local_hands.TOOLS
                reply, calls = _post_stream(payload, gate)
                reply = reply.strip()
                if not calls:
                    break
                messages.append({"role": "assistant", "content": reply,
                                 "tool_calls": calls})
                for tc in calls:
                    fn = (tc.get("function") or {})
                    name = fn.get("name") or ""
                    result = local_hands.execute(name, fn.get("arguments") or {})
                    used_tools.append(f"{name}: {result}")
                    print(f"[local-hands] {name} -> {result}")
                    messages.append({"role": "tool", "tool_name": name,
                                     "content": result})
        except _Refusal:
            refused = True   # nothing spoken — head was still held
        except Exception as e:  # noqa: BLE001 — local down = fall back, not die
            print(f"[local] chat failed: {e}")
            _mark_down()
            return None

        # Short reply that ended before the hold threshold: same refusal check.
        if not refused and holding[0] and held[0]:
            buf = held[0].lstrip()
            if not buf.startswith("ESCALATE") and REFUSAL_RE.match(buf):
                refused = True
            else:
                tail[0] = held[0]
                held[0] = ""

        wants_out = refused or reply.startswith("ESCALATE")
        if wants_out:
            # He explicitly asked for the full model — the ONLY time we punt.
            if allow_escalate and not offline:
                print("[local] escalate — he asked for the full model")
                return "ESCALATE"
            # Otherwise: re-run locally once with the forcing nudge. Nothing
            # has been spoken, so the retry is clean.
            if attempt == 0:
                print("[local] refusal/self-punt -> forced local retry")
                nudge = FORCE_NUDGE
                continue
            # Retry still stuck: speak a local line, never escalate on our own.
            line = OFFLINE_LINE if offline else STUCK_LINE
            print("[local] still stuck after retry -> local line")
            _emit(line)
            _remember(line)
            return line

        # Lie gate: he asked for an action, the reply OPENS as a completion
        # claim, but zero tools ran this turn — fabricated success (measured
        # twice live). Only actionable while nothing was spoken; short claim
        # replies sit under HOLD_MAX, so in practice they are always held.
        if (not used_tools and not spoken[0]
                and ACTION_HINT_RE.search(text)
                and CLAIM_RE.match(reply.lstrip())):
            if attempt == 0:
                print("[local] action claim with no tool call -> lie retry")
                nudge = LIE_NUDGE
                continue
            print("[local] still fabricating after lie retry -> honest line")
            _emit(LIE_LINE)
            _remember(LIE_LINE)
            return LIE_LINE

        # Real answer. Strip a hedged trailing ESCALATE (answer stands).
        if reply.endswith("ESCALATE"):
            reply = reply[: -len("ESCALATE")].rstrip()
            tail[0] = tail[0].rstrip()
            if tail[0].endswith("ESCALATE"):
                tail[0] = tail[0][: -len("ESCALATE")].rstrip()
        if tail[0]:
            _emit(tail[0])  # flush the held-back tail
        _remember(reply)
        return reply


