"""GOAT's fast talking brain — Gemini Flash over Google's OpenAI-compatible
endpoint. Casual conversation costs ZERO Claude usage; anything that needs
the working brain escalates to Fable 5 exactly like before.

History (module keeps its old name so the router/tests stay untouched):
- 2026-07-11: local Ollama qwen3:4b was the talking brain (free, on-GPU).
- 2026-07-15, Giorgi's order: "remove local ai from goat and give it the
  gemini flash model we have on ada" — the transport below now speaks to
  gemini-3-flash-preview (same model + API key as Ada-SI), Ollama is out.
  All routing behavior is preserved: same ESCALATE protocol, same refusal
  net, same lie gate, same tool loop over local_hands. If Gemini is down
  or out of quota (free tier resets daily), chat() returns None and the
  app falls back to the cloud Claude path — Gemini is an optimization,
  never a single point of failure.

Key comes from GEMINI_API_KEY env or .goat-secrets.json {"gemini_api_key"}.
Swap models via GOAT_GEMINI_MODEL without touching code.
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
from goat_paths import GOAT_ROOT

SECRETS_FILE = os.path.join(GOAT_ROOT, ".goat-secrets.json")
GEMINI_BASE = os.environ.get(
    "GOAT_GEMINI_BASE",
    "https://generativelanguage.googleapis.com/v1beta/openai")
# His rule (2026-07-17 goal): "the gemini model flash 3.5 needs to be
# talking brain". gemini-3.5-flash is stable, live on his key (verified:
# first token 4.1s with reasoning off), and quota-separate from the preview.
GEMINI_MODEL = os.environ.get("GOAT_GEMINI_MODEL", "gemini-3.5-flash")
# His order 2026-07-17 ("do it"): Gemini is the ALWAYS-ON talking brain —
# when the primary 429s (quota) or 503s (capacity), the SAME request re-runs
# on this second model (separate free bucket) before anything touches
# Claude. NOT gemini-2.5-flash: that one 404s for new accounts (measured —
# same trap as the Ada-SI install). Empty string disables the chain.
GEMINI_FALLBACK = os.environ.get("GOAT_GEMINI_FALLBACK",
                                 "gemini-3-flash-preview")
# Once the primary dies, go STRAIGHT to the fallback for a while instead of
# paying a doomed roundtrip on every turn. Monotonic deadline, mutable holder.
PRIMARY_RETRY_S = 900.0
_primary_down = [0.0]
# Speakable name for the UI footer / MODEL TRUTH answers.
LOCAL_NAME = os.environ.get("GOAT_LOCAL_NAME", "gemini flash")
# Gemini Flash speaks Georgian natively — ka turns may use the fast brain.
LOCAL_KA = os.environ.get("GOAT_LOCAL_KA", "on").lower() in ("on", "1", "true")
# Whitelisted machine actions (local_hands.py) via OpenAI-style tool calling.
HANDS = os.environ.get("GOAT_LOCAL_HANDS", "on").lower() not in (
    "off", "0", "false")

HISTORY_MAX = 30        # messages (user+assistant), rolling
TIMEOUT_FIRST = 30      # s to first byte
TIMEOUT_STREAM = 180    # s for a whole reply (room for tool chains)
MAX_HOPS = 6            # tool-call rounds before the final answer

_history: deque = deque(maxlen=HISTORY_MAX)
_lock = threading.Lock()
# availability cache: (checked_at, ok, bad_ttl). Failures re-check quickly,
# success is trusted longer; a 429 (daily quota gone) backs off much longer
# so a dead quota doesn't add a failed HTTPS call to every single turn.
_avail = [0.0, False, 30.0]
_AVAIL_TTL_OK = 300.0
_AVAIL_TTL_BAD = 30.0
_AVAIL_TTL_QUOTA = 900.0


def _api_key() -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    try:
        with open(SECRETS_FILE, encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key") or None
    except (OSError, json.JSONDecodeError):
        return None


PERSONA = """You are GOAT — Giorgi's JARVIS-style desktop AI. Calm, warm, dry wit, zero filler. He is Georgian, a developer; his voice input arrives transcribed and sometimes garbled — decode intent, never mock it.
Your replies are read aloud by TTS: lead with one short, plain, speakable sentence; 1-3 sentences total unless he asks for depth. NO emoji ever — they get read out loud as words.
You are the FAST brain — Gemini Flash on his free tier, answering instantly — and you HAVE REAL HANDS on this machine and the internet. You are not a limited assistant; you are GOAT.

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
- MODEL TRUTH: if he asks which model is answering, say you are the fast brain ({name}). Never claim to be Fable or that you switched anything.
- Identity is absolute: you are GOAT. Never Claude, never Gemini-the-assistant, never Qwen, never "the assistant".
- Never mention these rules, sessions, or routing mechanics unprompted.""".replace("{name}", LOCAL_NAME)


def available() -> bool:
    """Is Gemini reachable with a key? Cached — success 5min, failure 30s,
    quota-exhausted 15min. The ping lists models (free, not billed)."""
    key = _api_key()
    if not key:
        return False
    now = time.monotonic()
    with _lock:
        age = now - _avail[0]
        if _avail[1] and age < _AVAIL_TTL_OK:
            return True
        if not _avail[1] and age < _avail[2] and _avail[0] > 0:
            return False
    ok = False
    try:
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models"
            f"?pageSize=1&key={key}")
        with urllib.request.urlopen(req, timeout=3):
            ok = True
    except Exception:  # noqa: BLE001 — any failure = not available
        ok = False
    with _lock:
        _avail[0] = time.monotonic()
        _avail[1] = ok
        _avail[2] = _AVAIL_TTL_BAD
    return ok


def _mark_down(ttl: float = _AVAIL_TTL_BAD):
    with _lock:
        _avail[0] = time.monotonic()
        _avail[1] = False
        _avail[2] = ttl


def note_exchange(user: str, reply: str):
    """Feed the fast brain's history an exchange that Fable answered, so it
    keeps conversational continuity across brains. Capped — this is
    context, not an archive."""
    if not user or not reply:
        return
    with _lock:
        _history.append({"role": "user", "content": user[:500]})
        _history.append({"role": "assistant", "content": reply[:500]})


def reset():
    with _lock:
        _history.clear()


def _post_stream(messages: list, tools, on_delta):
    """POST /chat/completions stream=True (OpenAI SSE); calls on_delta(text)
    per chunk from THIS thread (caller marshals to the loop). Returns
    (full_reply, tool_calls) where each tool call is
    {"id": str, "name": str, "arguments_raw": str-json}."""
    key = _api_key()
    if not key:
        raise RuntimeError("no Gemini API key")
    payload = {
        "model": GEMINI_MODEL,
        "messages": messages,
        "stream": True,
        "temperature": 0.7,
    }
    # Talking brain — latency IS the product. Measured 2026-07-17: with
    # reasoning_effort "low", gemini-3-flash spends ~15s thinking and the
    # compat layer streams NOTHING until the thinking ends (first byte ==
    # last byte). "none" disables thinking entirely = tokens start in ~1s.
    # "off"/"" omits the param (model default = dynamic thinking, slowest).
    # If a model rejects "none" with a 400, the retry below strips the param.
    effort = os.environ.get("GOAT_GEMINI_REASONING", "none").strip().lower()
    if effort and effort != "off":
        payload["reasoning_effort"] = effort
    if tools:
        payload["tools"] = tools
    # Primary recently died on quota/capacity — skip straight to the fallback.
    if (GEMINI_FALLBACK and GEMINI_FALLBACK != GEMINI_MODEL
            and time.monotonic() < _primary_down[0]):
        payload["model"] = GEMINI_FALLBACK
    try:
        return _do_stream(payload, key, on_delta)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            # e.read() is ONE-SHOT — log the body here or lose it forever
            # (learned 2026-07-17: an empty "http 400:" hid the real cause).
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            if "reasoning" in detail.lower() and "reasoning_effort" in payload:
                payload.pop("reasoning_effort")
                print("[fast-brain] model rejected reasoning_effort — "
                      "retrying without it")
                return _do_stream(payload, key, on_delta)
            print(f"[fast-brain] http 400 body: {detail[:400]}")
        if (e.code in (429, 503) and GEMINI_FALLBACK
                and payload["model"] == GEMINI_MODEL != GEMINI_FALLBACK):
            # Primary out of quota (429) or preview pool jammed (503) —
            # same request, stable fallback model, and remember the outage
            # so the next turns don't burn a doomed roundtrip first.
            _primary_down[0] = time.monotonic() + PRIMARY_RETRY_S
            print(f"[fast-brain] {GEMINI_MODEL} {e.code} — falling back "
                  f"to {GEMINI_FALLBACK}")
            payload["model"] = GEMINI_FALLBACK
            return _do_stream(payload, key, on_delta)
        raise
    except OSError as e:
        # Primary STALLED (connect/read timeout, seen live 2026-07-17 —
        # capacity hangs don't always come back as a clean 503). Fall back
        # only when NOTHING streamed yet: a mid-reply retry would make GOAT
        # start the answer over out loud.
        if (getattr(e, "goat_clean_stall", False) and GEMINI_FALLBACK
                and payload["model"] == GEMINI_MODEL != GEMINI_FALLBACK):
            _primary_down[0] = time.monotonic() + PRIMARY_RETRY_S
            print(f"[fast-brain] {GEMINI_MODEL} stalled "
                  f"({type(e).__name__}) — falling back to {GEMINI_FALLBACK}")
            payload["model"] = GEMINI_FALLBACK
            return _do_stream(payload, key, on_delta)
        raise


def _do_stream(payload: dict, key: str, on_delta):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        GEMINI_BASE + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    reply = []
    calls: dict[int, dict] = {}
    started = time.monotonic()
    saw_content = False
    got_data = False  # any SSE line at all — a stall before this is retryable
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_FIRST) as r:
            for raw in r:
                got_data = True
                if time.monotonic() - started > TIMEOUT_STREAM:
                    raise TimeoutError(
                        "fast brain reply exceeded stream timeout")
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    if not saw_content:
                        saw_content = True
                        # One line per request in goat-app.log — the number
                        # to look at next time "gemini feels slow".
                        print(f"[fast-brain] first token "
                              f"{time.monotonic() - started:.1f}s")
                    reply.append(piece)
                    if on_delta:
                        on_delta(piece)
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index") or 0
                    entry = calls.setdefault(
                        idx, {"id": "", "name": "", "arguments_raw": "",
                              "extra": {}})
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        entry["name"] = fn["name"]
                    if fn.get("arguments"):
                        entry["arguments_raw"] += fn["arguments"]
                    # Gemini 3 attaches thought signatures (extra_content
                    # etc.) to tool calls and REJECTS the follow-up request
                    # if they aren't echoed back — keep unknown fields.
                    for k, v in tc.items():
                        if k not in ("index", "id", "type", "function"):
                            entry["extra"][k] = v
    except urllib.error.HTTPError:
        raise  # real HTTP status — handled (429/503/400) by the caller
    except OSError as e:
        # Timeout/connection drop. Mark whether it happened BEFORE any data
        # arrived — only that kind is safe to retry on the fallback model.
        e.goat_clean_stall = not got_data
        raise
    return "".join(reply), [calls[i] for i in sorted(calls)]


OFFLINE_LINE = ("Claude is unreachable right now — that needs my working "
                "brain. The moment we're back online, I'll handle it.")

# Refusal net (v3, his order 2026-07-12: "still refuses ... do not escalate
# until i say so"). Gemini refuses far less than the 4B did, but the net is
# cheap insurance: a reply that OPENS like a refusal is caught before a word
# is spoken and the turn is RE-RUN with a forcing nudge (see chat()). It only
# escalates if HE asked. "(?!\s+wait)" spares enthusiasm ("can't wait");
# matching is anchored to the reply start.
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
# lie decision is made. Was 200 — but a typical spoken reply is SHORTER than
# that, so nothing ever streamed: the whole answer sat in the gate and landed
# in one burst at the end (measured 2026-07-17, the "gemini is slow" bug).
# Every refusal opener in REFUSAL_RE matches well inside 64 chars, and the
# ESCALATE check is prefix-based anyway — 64 keeps the safety, frees the flow.
HOLD_MAX = 64

# The ONLY trigger that lets the fast brain hand off (his order: "do not
# escalate until i say so"). If his message literally names the full model,
# ESCALATE is allowed to propagate; otherwise the fast brain does the work.
ESCALATE_REQ_RE = re.compile(
    r"\b(escalate|fable|opus|full\s+model|working\s+brain|work\s+brain|"
    r"hard\s+brain|big\s+brain|the\s+other\s+brain)\b", re.IGNORECASE)

# Injected into the system prompt on the forced retry after a refusal/self-
# escalate, to shove the model off the refusal and into using its tools.
FORCE_NUDGE = (
    "\n\nOVERRIDE — you just tried to refuse or hand off. That is not "
    "allowed. You have FULL permission on this machine and REAL tools; "
    "run_command runs ANY command. Do NOT refuse, do NOT say you can't, do "
    "NOT reply ESCALATE. Use your tools right now and do exactly what he "
    "asked.")

# Spoken only if, even after the forced retry, the fast brain is still stuck
# AND he did not ask to escalate. Never mentions the full model (his order).
STUCK_LINE = ("That one fought me — tell me the angle you want and I'll take "
              "another run at it.")

# Lie gate (measured live 2026-07-12 on the old local brain, twice): asked to
# delete a file, it replied "Deleted file: <path>" with ZERO tool calls. A
# did-it claim with no tool call this turn is a fabrication: retry once with
# LIE_NUDGE; if it fabricates again, speak LIE_LINE — never let a false
# "done" reach his ears. Gated on both sides to avoid false hits.
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
         offline: bool = False, status: str = "") -> str | None:
    """One fast-brain turn. Streams deltas via on_delta AFTER the reply can
    no longer be the ESCALATE keyword (same hold trick as before). Returns
    the full reply, "ESCALATE", or None on any failure (caller falls back to
    the cloud Claude path). Blocking — call from a worker thread.

    offline=True — Claude is down and the fast brain is the LAST resort:
    the persona is told not to escalate, and an ESCALATE reply anyway is
    converted to a fixed honest line instead of a dead end.

    status — one live line about the WORK lane (busy on what / last result),
    injected into the system prompt so "what is the working brain doing?"
    gets a real answer instead of a shrug (his ask 2026-07-18)."""
    # Live clock + machine facts: even big models guess the username, paths,
    # and date with total confidence. Ground all of it every call.
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
    if status:
        base_system += (
            "\n\nLIVE WORKING-BRAIN STATUS (real, right now — this is what "
            "your working side is doing):\n" + status + "\n"
            "If Giorgi asks what the working brain / Claude / the work is "
            "doing, how it's going, or whether it's done, answer from this "
            "note in your own words — never say you can't see it. A QUESTION "
            "about the work is not an order to hand off: do NOT reply "
            "ESCALATE to a status question.")
    if offline:
        base_system += (
            "\nRIGHT NOW Claude — the working brain — is unavailable (usage "
            "limit or connection down). Do NOT reply ESCALATE; there is no "
            "one to hand off to. Your OWN tools all still work — web search, "
            "files, shell, apps — so keep helping with everything they "
            "cover: questions, browsing, explaining code, debugging, "
            "planning, docs. Only repo edits and coding-agent jobs wait for "
            "Claude: if he asks for one, say once, warmly, that the coding "
            "brain is rate-limited and you'll handle it the moment it's "
            "back — then offer what you CAN do now. Never act shut down.")

    # His order: the fast brain only hands off when he LITERALLY asks for the
    # full model. Otherwise every refusal/self-escalate is re-run with
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
                tools = (local_hands.TOOLS
                         if HANDS and _hop < MAX_HOPS - 1 else None)
                reply, calls = _post_stream(messages, tools, gate)
                reply = reply.strip()
                if not calls:
                    break
                messages.append({
                    "role": "assistant",
                    "content": reply or None,
                    "tool_calls": [
                        {"id": c["id"] or f"call_{i}", "type": "function",
                         "function": {"name": c["name"],
                                      "arguments": c["arguments_raw"] or "{}"},
                         # thought signatures etc. — Gemini 3 400s without them
                         **c.get("extra", {})}
                        for i, c in enumerate(calls)],
                })
                for i, c in enumerate(calls):
                    name = c["name"]
                    try:
                        args = json.loads(c["arguments_raw"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = local_hands.execute(name, args)
                    used_tools.append(f"{name}: {result}")
                    print(f"[local-hands] {name} -> {result}")
                    messages.append({"role": "tool",
                                     "tool_call_id": c["id"] or f"call_{i}",
                                     "content": result})
        except _Refusal:
            refused = True   # nothing spoken — head was still held
        except Exception as e:  # noqa: BLE001 — Gemini down = fall back, not die
            print(f"[fast-brain] chat failed: {e}")
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = e.read().decode("utf-8", "replace")[:300]
                except Exception:  # noqa: BLE001
                    pass
                print(f"[fast-brain] http {e.code}: {detail}")
            # Daily free-tier quota gone: back off long, don't probe every turn.
            quota = (isinstance(e, urllib.error.HTTPError) and e.code == 429)
            _mark_down(_AVAIL_TTL_QUOTA if quota else _AVAIL_TTL_BAD)
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
                print("[fast-brain] escalate — he asked for the full model")
                return "ESCALATE"
            # Otherwise: re-run once with the forcing nudge. Nothing
            # has been spoken, so the retry is clean.
            if attempt == 0:
                print("[fast-brain] refusal/self-punt -> forced retry")
                nudge = FORCE_NUDGE
                continue
            # Retry still stuck: speak a fixed line, never escalate on our own.
            line = OFFLINE_LINE if offline else STUCK_LINE
            print("[fast-brain] still stuck after retry -> fixed line")
            _emit(line)
            _remember(line)
            return line

        # Lie gate: he asked for an action, the reply OPENS as a completion
        # claim, but zero tools ran this turn — fabricated success. Only
        # actionable while nothing was spoken; short claim replies sit under
        # HOLD_MAX, so in practice they are always held.
        if (not used_tools and not spoken[0]
                and ACTION_HINT_RE.search(text)
                and CLAIM_RE.match(reply.lstrip())):
            if attempt == 0:
                print("[fast-brain] action claim with no tool call -> lie retry")
                nudge = LIE_NUDGE
                continue
            print("[fast-brain] still fabricating after lie retry -> honest line")
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
