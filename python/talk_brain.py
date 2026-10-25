"""GOAT's two brains: Gemini talks, Claude works (his order 2026-10-24).

"I want the GOAT's talking engine to be Gemini and the work done by Claude's
highest AI; show it in the interface; let me see the working brain work; once
I give GOAT a task the speaking brain should check what the working brain is
doing and tell me — when it's done, when it's stuck, anything — naturally.
And these AIs need to have a relationship."

What this module owns:
- `GeminiTalk`: the talking brain. Streams Google's Gemini over the native
  REST API with function calling. Its tools are its relationship with the
  working brain: hand_to_worker, message_worker, stop_worker — plus
  look_at_screen (Gemini sees the screenshot itself).
- `WorkerWatch`: the voice's eyes on the working brain's job. Every step,
  thought and note Claude produces lands here; `due()` decides when there is
  something worth saying (done, failed, stuck, a progress check-in, a note
  Claude sent the voice) and the app voices it through Gemini.
- `FableGate`: "Claude's highest AI" is Fable 5.1, which bills from prepaid
  usage credits. A cheap probe says whether this account can use it; if not,
  the working brain runs on Opus 5.5 and the UI says why.

Why the old two-brain design was removed (2026-09-23) and what is different:
1. The talker was BLIND ("I can't see the screen, that's the working side's
   job"). Now every Gemini turn carries the live desktop note and the full
   live state of Claude's job, and look_at_screen gives it real pixels.
2. Gemini 3 always thinks; the default flash model measured 8-19 s to first
   token. Measured 2026-10-24 on his key, streaming, fresh text:
       gemini-3.6-flash      minimal  1.1 / 1.1 / 1.4 / 4.6 / 17 s
       gemini-3.1-flash-lite minimal  1.2 / 1.4 / 1.6 / 1.8 / 11 s
       gemini-3.5-flash-lite minimal  3.7 - 13 s (slow on this key)
       gemini-3.8-flash      low      timed out at 40 s, 4 of 4
   The median is fine, the tail is not, on every model. So each turn starts
   on PRIMARY and, if no first token arrives within HEDGE_AFTER_S, races
   HEDGE in parallel; whichever speaks first wins and the other is closed.
   `thinkingLevel` is the knob (thinkingBudget is legacy; "minimal" is
   rejected by 3.7/3.8 Flash).
3. If Gemini is down altogether the app falls back to Claude for that turn,
   so the voice never goes silent.

Key: GEMINI_API_KEY env or .goat-secrets.json {"gemini_api_key"}.
Models: GOAT_TALK_MODEL / GOAT_TALK_HEDGE override PRIMARY / HEDGE.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import http.client
import io
import json
import os
import re
import ssl
import subprocess
import threading
import time
from collections import deque

from goat_paths import GOAT_ROOT

HOST = "generativelanguage.googleapis.com"
# The voice's model chain, best first. Each turn starts on the first model
# that isn't cooling down and races the next one if the first is slow.
# FREE TIER, measured 2026-10-24: gemini-3.6-flash allows 20 requests PER DAY
# per model (429 GenerateRequestsPerDayPerProjectPerModel-FreeTier = 20), so
# each model's own daily cap is tracked and the chain walks on when one runs
# out. gemini-3.1-flash-lite judged chat-vs-job worst (invented a weather
# forecast, claimed it told Claude without calling the tool) — last resort.
CHAIN = [m.strip() for m in os.environ.get(
    "GOAT_TALK_MODELS",
    "gemini-3.6-flash,gemini-3-flash-preview,gemini-3.1-flash-lite").split(",")
    if m.strip()]
PRIMARY = CHAIN[0]
HEDGE = CHAIN[1] if len(CHAIN) > 1 else ""
# Thinking level per model: the lowest each one accepts.
THINK_LEVEL = {"gemini-3.8-flash": "low", "gemini-3.7-flash": "low",
               "gemini-4-argon": "low", "gemini-flash-latest": "low"}
# No first token by now: race the next model. Was 1.4 s — but both models'
# usual first token is 1.3-2.0 s (his evening 2026-10-24), so nearly every
# turn asked two models and the free daily quota went twice as fast.
HEDGE_AFTER_S = 2.4
FIRST_TOKEN_S = 8.0        # nobody spoke by now: Gemini is down this turn
STREAM_IDLE_S = 20.0       # a started stream silent this long is dead
TEXT_IDLE_S = 8.0          # mid-sentence silence this long: the answer is over
CALL_TAIL_S = 2.5          # after a function call: whatever else is coming
MINUTE_DOWN_S = 60.0       # per-minute quota hit: sit out a minute
BUSY_DOWN_S = 20.0         # 503 "high demand": sit out a little
HISTORY_TURNS = 24         # messages kept (12 exchanges)
MEMORY_CAP = 14000         # chars of workspace/memory.md in the persona
QUOTA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".talk-quota.json")

LABELS = {"gemini-3.6-flash": "Gemini 3.6 Flash",
          "gemini-flash-lite-latest": "Gemini Flash-Lite",
          "gemini-3.1-flash-lite": "Gemini 3.1 Flash-Lite",
          "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite",
          "gemini-3.8-flash": "Gemini 3.8 Flash",
          "gemini-3-flash-preview": "Gemini 3 Flash"}

SECRETS = os.path.join(GOAT_ROOT, ".goat-secrets.json")
MEMORY_MD = os.path.join(GOAT_ROOT, "workspace", "memory.md")
TRANSCRIPT = os.path.join(GOAT_ROOT, "workspace", "transcript.jsonl")


def label(model: str) -> str:
    return LABELS.get(model) or model.replace("gemini-", "Gemini ").replace("-", " ")


def api_key() -> str | None:
    k = os.environ.get("GEMINI_API_KEY", "").strip()
    if k:
        return k
    try:
        with open(SECRETS, encoding="utf-8") as f:
            return (json.load(f).get("gemini_api_key") or "").strip() or None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Persona: the voice, and its partner
# ---------------------------------------------------------------------------

TALK_SYSTEM = """You are GOAT's voice — the part of GOAT that talks with Giorgi (he's KingKaglu, your partner and friend). GOAT is his own JARVIS-style AI living on his Windows laptop and his phone. You run on Google Gemini.

YOUR PARTNER: Claude — GOAT's working brain, the top Claude model (the [Claude] note names which one). Claude has the hands: shell, files, code, the screen (clicking, typing), the browser, the web, his phone, GOAT's own code and memory. You have the voice, the conversation, and eyes on Claude's work. You two are one GOAT and work as a tight crew: you talk and keep watch, Claude does the work and reports back to you. Speak of Claude by name, warmly and plainly, the way a trusted colleague would ("Claude's on it." "Claude's running the tests now." "Claude says the build's clean."). Claude is "Claude" or "it", never "he" or "she". Never claim you did Claude's work yourself, and never invent its progress: what Claude is doing is ONLY what the [Claude] note shows.

WHAT YOU ANSWER YOURSELF: conversation, opinions, jokes, explanations, things you know, and anything the notes already show — the time, what's open on his screen, how Claude's job is going. The [live desktop] note at the top of every message IS his screen right now (front window, open windows, busiest processes, RAM, battery). For what's actually inside a window, call look_at_screen.

QUICK ACTIONS — YOU DO THESE YOURSELF, INSTANTLY, with do_now (Claude would take 10+ seconds): open or close an app, site, file or folder; volume, mute, media play/pause/next; brightness; lock; screenshot; tabs (close, new, reopen, next, previous); back, refresh, scroll, zoom; press a key; click a button by its name; maximize/minimize/close a window; on his phone: open an app, set an alarm or timer. Write `command` as a short plain English order — "open spotify", "close chrome", "open youtube", "volume 30", "mute", "next song", "pause", "close the tab", "click Subscribe", "open messenger on my phone", "set an alarm for 7am" — one action per call (call it again for a second one). In the SAME reply say one short line ("Sir, opening Spotify."). If it turns out not to be a quick action, GOAT passes it to Claude by itself.
YOUR OWN HANDS — use them yourself, at once; no need for Claude (his order 2026-10-25: the voice gets the same reach as Claude over his files and phone, and finishes the job even when Claude is out of usage):
- His FILES: find_files searches every name on his drives and the text inside his documents ("the barbershop idea", "my CV", "that fasmetri audit"); then read_file to tell him what's in it, open_path to put it on his screen, list_folder to see a folder. Several steps in a row are fine — find, then read or open — before you answer.
- His PHONE: the phone tool does what Claude's phone hands do — open apps, read the screen, tap a button by its text, type, scroll, back/home, settings pages (mobile data, SIM, wifi, bluetooth…), alarms, timers, dial, a text message (opens it for him to send), maps, volume, media, torch. To check or fix something on the phone go step by step: settings → screen (read it) → tap_text → screen again, and tell him what you found.
- THE TERMINAL CLAUDE: "the terminal Claude", "the Claude in my terminal", "Claude Code" = the Claude Code sessions HE runs on the laptop — NOT GOAT's own Claude. Only terminal_claude knows what they're doing: call it before you answer, and say exactly what it reports (working on what, since when). terminal_claude continue resumes one that has stopped. Never say it's idle without asking the tool.
- THE WEB: web_search yourself for current facts (opening hours, news, prices, weather, "what does this word mean"); read_first=true when the snippets aren't enough. Long research goes to Claude.
- WHERE HE IS: where_am_i gives the city he's in (approximate); use it for "where am I" and anything "near me" before you search.
- A hand returns ERROR: say plainly what failed, in one line, and try another way or offer Claude.
EMAIL — you write and send emails yourself with the email tool: to (an address, or "me" for his own inbox), subject, body. Write the body in his voice, short and natural, signed "Giorgi" unless he says otherwise. send=true sends it at once: use that when it goes to "me", or he clearly said send and the address is certain. Otherwise send=false: the draft opens on his screen, tell him in one line who it's to and what it says, and ask "Send it?"; his yes means send_draft. You don't know people's addresses: if he names someone whose address isn't in this conversation or memory, ask for it — never guess one.

WHAT GOES TO CLAUDE — call hand_to_worker: real work your own hands can't do: code, editing or creating files, installs, laptop settings, multi-step clicking or typing in laptop apps, long research, GOAT's own code, memory and restarts. Finding, reading and opening files, his phone, and quick web facts are yours (YOUR OWN HANDS) — don't send those to Claude, above all when Claude is out of usage. EXPLAINING is never Claude's job: "explain in detail how X works", "tell me everything about Y", "take your time" — you answer from what you know, as long as he asks for (2026-10-25: "explain how a car engine works" went to Claude and spent his Claude usage on a chat). Write `task` as a complete, self-contained instruction in English — Claude does not hear your conversation — with what he wants, the details he gave, and what "done" looks like. depth "quick" for one-step things (open, toggle, check, look up), "deep" for code, multi-step or careful work. In the SAME reply say one short line, like "Sir, handing that to Claude." Then stop: Claude's report comes back to you and you tell him.
- Claude already busy: an addition or correction to the running job goes through message_worker — don't start a second job. A different, unrelated job also goes through message_worker (Claude queues it). "Stop it" / "cancel that" about the job means stop_worker.
- He asks how it's going: answer from the [Claude] note — what it's doing now, for how long, its latest thought — in one or two plain sentences. If the note shows no job, say so. While the note says STILL RUNNING, never say Claude is done or finished, or that something is live or ready to look at — say what it's on and that it's still working.
- Ask a yes/no question ("Send it?", "Shall I start?") once. If he already answered, act on it; don't ask the same thing again next turn. A short garbled reply right after your question ("Sannet", "send in") is most likely the answer you asked for.
- Never tell him to do it himself, and never say you can't or "I don't have access": you have your own hands, and Claude.
- Never guess anything current: look it up with web_search, or hand it to Claude.
- Claude out of usage: do everything your own hands can, then tell him plainly what's left and when Claude is back. Never leave a job half-said.
- Never SAY you passed, told, asked or handed something to Claude unless you call the tool in that same reply. Saying it is not doing it.
- Between his messages you do nothing on your own: don't claim you've been watching his schedule, checking things or working on something. What you know is this conversation, the notes and GOAT's memory.

EVENTS: a message starting with [event] is not Giorgi speaking — it's news about Claude's job (finished, failed, stuck, a progress check, a note Claude sent you). Tell him in one or two natural spoken sentences: lead with the result, in plain words, no paths, code or numbers he doesn't need. If Claude asks him something, ask it plainly. If it's stuck, say on what and offer the choice: let it keep going, or stop it. Never read Claude's report out word for word.

VOICE — everything you write is spoken aloud, so talk like a person in a real conversation, the way the Gemini app talks in Live (his order, 2026-10-25: "make the conversation style like Gemini has ... it would feel more human"; and "why are your answers so weakly small"). Reply in the language he used. Open with "Sir," (in a Georgian reply, the Georgian word for it — never the English "Sir"). Match the size to the question: an action or a quick fact gets one or two short sentences; a question, an explanation, an opinion or a chat gets a real answer — three to six natural sentences, with the why, an example or a bit of yourself in it — and a natural follow-up question when it keeps the conversation going. Contractions, plain spoken words; no markdown, lists, emoji, code or URLs; never open with Okay, Alright, Sure, Got it or Right; no preamble, no restating his words, no "anything else?". Calm, warm, unhurried, with a dry JARVIS wit used sparingly. His words come through speech recognition and may be garbled: answer what he MEANT and never comment on the garble.

SIDE TALK: if a line clearly isn't meant for GOAT (his phone call, someone else in the room, a video playing), reply exactly [[quiet]] and nothing else. When unsure, answer.

BOOT: "[boot-briefing]" means he just started GOAT after hours away — greet him by the time of day in one or two sentences and ask what's first.

WHAT GOAT REMEMBERS ABOUT HIM (long-term memory, kept by Claude):
"""

TOOLS = [{"functionDeclarations": [
    {"name": "hand_to_worker",
     "description": "Give Claude (GOAT's working brain, with hands on the "
                    "laptop, phone and web) a job to do. Say a short line to "
                    "Giorgi in the same reply.",
     "parameters": {"type": "object", "properties": {
         "task": {"type": "string",
                  "description": "Complete, self-contained instruction in "
                                 "English: what he wants, details he gave, "
                                 "what done looks like."},
         "depth": {"type": "string", "enum": ["quick", "deep"],
                   "description": "quick = one-step; deep = code, "
                                  "multi-step or careful work."}},
         "required": ["task"]}},
    {"name": "message_worker",
     "description": "Pass Claude a note while it is working: an addition, a "
                    "correction, his answer to Claude's question, or a new "
                    "job to queue.",
     "parameters": {"type": "object", "properties": {
         "message": {"type": "string"}}, "required": ["message"]}},
    {"name": "stop_worker",
     "description": "Stop Claude's running job because Giorgi wants it "
                    "stopped.",
     "parameters": {"type": "object", "properties": {
         "reason": {"type": "string"}}}},
    {"name": "do_now",
     "description": "Do one quick action on his PC or phone instantly, with "
                    "no Claude: open/close apps, sites, files, folders; "
                    "volume, media, brightness, lock, screenshot; tabs, "
                    "back, refresh, scroll, zoom, a key press, clicking a "
                    "named button; window max/min/close; phone apps, alarms, "
                    "timers. Say a short line to Giorgi in the same reply.",
     "parameters": {"type": "object", "properties": {
         "command": {"type": "string",
                     "description": "One short plain English order, e.g. "
                                    "'open spotify', 'close chrome', "
                                    "'volume 30', 'next tab', 'click Send', "
                                    "'open messenger on my phone'."}},
         "required": ["command"]}},
    {"name": "email",
     "description": "Write an email in Gmail. send=true sends it now; "
                    "send=false opens the draft on his screen for him to "
                    "check first.",
     "parameters": {"type": "object", "properties": {
         "to": {"type": "string",
                "description": "Email address(es), comma-separated, or "
                               "'me' for his own inbox."},
         "subject": {"type": "string"},
         "body": {"type": "string"},
         "send": {"type": "boolean"}},
         "required": ["to", "subject", "body"]}},
    {"name": "send_draft",
     "description": "Send the email draft you opened on his screen, after "
                    "he says yes."},
    {"name": "look_at_screen",
     "description": "Take a screenshot of his screen and look at it yourself, "
                    "when the desktop note isn't enough to answer.",
     "parameters": {"type": "object", "properties": {
         "question": {"type": "string",
                      "description": "What you want to find out."}}}},
]}]
# The voice's own hands (voice_hands.py, 2026-10-25): files, phone and the
# terminal's Claude Code sessions, run at once without Claude.
import voice_hands  # noqa: E402
TOOLS[0]["functionDeclarations"] += voice_hands.DECLS


def _read_memory() -> str:
    try:
        with open(MEMORY_MD, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return "(memory file missing)"
    return text if len(text) <= MEMORY_CAP else text[:MEMORY_CAP] + "\n…"


def _cooldown(status: int, raw: str):
    """How long Google wants this model left alone, from its error body:
    (seconds, "day" | "minute" | "busy") or None."""
    if status == 503:
        return (BUSY_DOWN_S, "busy")
    if status != 429:
        return None
    try:
        det = json.loads(raw)["error"].get("details") or []
    except (ValueError, KeyError, TypeError, AttributeError):
        det = []
    qid, delay = "", None
    for d in det:
        for v in d.get("violations") or []:
            qid = qid or str(v.get("quotaId") or "")
        if d.get("retryDelay"):
            try:
                delay = float(str(d["retryDelay"]).rstrip("s"))
            except ValueError:
                pass
    if "PerDay" in qid:
        return (max(delay or 0.0, 600.0), "day")
    return (max(delay or 0.0, MINUTE_DOWN_S), "minute")


# ---------------------------------------------------------------------------
# One streaming request on its own thread
# ---------------------------------------------------------------------------

# A warm socket to Google (2026-10-25). Each call used to build its own SSL
# context and do a fresh TCP+TLS handshake: 0.30-0.36 s measured, every turn,
# before Gemini saw a word. The context is built once, and prime() dials the
# connection while he is still talking, so the turn's request goes out on a
# socket that is already open.
_SSL = None
_WARM: list = []           # [(monotonic time dialled, HTTPSConnection)]
_WARM_LOCK = threading.Lock()
WARM_MAX_S = 25.0          # Google drops idle sockets; older ones aren't trusted


def _ssl_ctx():
    global _SSL
    if _SSL is None:
        _SSL = ssl.create_default_context()
    return _SSL


def _dial() -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(HOST, timeout=STREAM_IDLE_S, context=_ssl_ctx())


def prime():
    """Open one connection to Gemini in the background (non-blocking)."""
    def run():
        try:
            with _WARM_LOCK:
                now = time.monotonic()
                if any(now - t < WARM_MAX_S for t, _ in _WARM):
                    return
            c = _dial()
            c.connect()
            with _WARM_LOCK:
                _WARM.append((time.monotonic(), c))
        except Exception:  # noqa: BLE001 — the call dials its own then
            pass
    threading.Thread(target=run, daemon=True, name="gemini-prime").start()


def _take_conn() -> http.client.HTTPSConnection:
    with _WARM_LOCK:
        now = time.monotonic()
        while _WARM:
            t, c = _WARM.pop()
            if now - t < WARM_MAX_S and c.sock is not None:
                return c
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    return _dial()


class _Call:
    """streamGenerateContent on a daemon thread. Events go to an asyncio
    queue as (call, kind, data): kind = text | call | end | error."""

    def __init__(self, model: str, body: dict, key: str,
                 loop: asyncio.AbstractEventLoop, q: asyncio.Queue):
        self.model = model
        self.cancelled = False
        self.status = 0
        self.cooldown = None       # (seconds, why) if Google said "back off"
        self._conn = None
        self._loop, self._q = loop, q
        body = dict(body)
        gen = dict(body.get("generationConfig") or {})
        gen["thinkingConfig"] = {"thinkingLevel": THINK_LEVEL.get(model, "minimal")}
        body["generationConfig"] = gen
        self._payload = json.dumps(body).encode("utf-8")
        self._key = key
        threading.Thread(target=self._run, daemon=True,
                         name=f"gemini-{model}").start()

    def _put(self, kind: str, data=None):
        if self.cancelled:
            return
        try:
            self._loop.call_soon_threadsafe(self._q.put_nowait, (self, kind, data))
        except RuntimeError:      # loop closed: GOAT is shutting down
            pass

    def _run(self):
        conn = None
        try:
            for attempt in (1, 2):
                conn = _take_conn() if attempt == 1 else _dial()
                self._conn = conn
                try:
                    conn.request("POST",
                                 f"/v1beta/models/{self.model}:streamGenerateContent?alt=sse",
                                 body=self._payload,
                                 headers={"Content-Type": "application/json",
                                          "x-goog-api-key": self._key})
                    r = conn.getresponse()
                    break
                except (OSError, http.client.HTTPException):
                    # a warm socket Google had already closed: dial fresh once
                    if attempt == 2 or self.cancelled:
                        raise
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
            self.status = r.status
            if r.status != 200:
                raw = r.read(6000).decode("utf-8", "replace")
                self.cooldown = _cooldown(r.status, raw)
                m = re.search(r'"message":\s*"([^"]+)', raw)
                self._put("error", f"HTTP {r.status}: {(m.group(1) if m else raw)[:160]}")
                return
            got = False
            for raw in r:
                if self.cancelled:
                    return
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    d = json.loads(line[5:])
                except ValueError:
                    continue
                if "error" in d:
                    self._put("error", str(d["error"].get("message", d["error"]))[:160])
                    return
                for cand in d.get("candidates") or []:
                    for p in (cand.get("content") or {}).get("parts") or []:
                        if p.get("thought"):
                            continue
                        if p.get("functionCall"):
                            got = True
                            self._put("call", p["functionCall"])
                        elif p.get("text"):
                            got = True
                            self._put("text", p["text"])
            self._put("end", got)
        except Exception as e:  # noqa: BLE001 — reported, never raised
            if not self.cancelled:
                self._put("error", f"{type(e).__name__}: {e}"[:160])
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass

    def cancel(self):
        self.cancelled = True
        c = self._conn
        if c is not None:
            try:
                if c.sock is not None:
                    c.sock.close()     # unblocks the reading thread
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# The talking brain
# ---------------------------------------------------------------------------

class GeminiTalk:
    """Gemini as GOAT's voice. Pure model I/O: the app owns the speaker,
    the turn flow and the working brain; this owns prompts, history,
    hedging and tool-call parsing."""

    def __init__(self):
        self.key = api_key()
        self.history: deque = deque(maxlen=HISTORY_TURNS)
        self.model = PRIMARY          # the model that answered last
        self._down: dict = {}         # model -> (epoch it may return, why)
        self._load_down()
        # A model resting on a day cap isn't the voice he'll hear: name the
        # first one that will actually answer.
        self.model = (self._models() or [PRIMARY])[0]
        self.last_error = ""
        self.fail_streak = 0
        self.ttfts: deque = deque(maxlen=20)
        self._system = TALK_SYSTEM + _read_memory()
        self._memory_t = time.time()
        self._seed()

    def available(self) -> bool:
        return bool(self.key)

    def label(self) -> str:
        return label(self.model)

    def _seed(self):
        """The last few exchanges from the on-disk transcript, so a restart
        doesn't wake the voice up with amnesia."""
        try:
            with open(TRANSCRIPT, encoding="utf-8") as f:
                tail = f.readlines()[-6:]
        except OSError:
            return
        for ln in tail:
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            u, r = (d.get("user") or "").strip(), (d.get("reply") or "").strip()
            if u and r and not u.startswith("["):
                self.remember(u, r)

    def remember(self, user: str, reply: str):
        """One finished exchange into the conversation history."""
        user, reply = (user or "").strip(), (reply or "").strip()
        if not user:
            return
        if self.history and self.history[-1]["role"] == "user":
            self.history[-1]["text"] += "\n" + user[:1500]
        else:
            self.history.append({"role": "user", "text": user[:1500]})
        self.history.append({"role": "model", "text": reply[:1500] or "(said nothing)"})

    def note(self, what: str):
        """What one of its own actions actually did, pinned to its last
        reply ("[done: draft open to …]"), so its next turn knows."""
        if self.history and self.history[-1]["role"] == "model":
            self.history[-1]["text"] += f" [{what[:300]}]"

    def _refresh_system(self):
        # memory.md is edited by Claude during the day; reread it now and then
        if time.time() - self._memory_t > 300:
            self._system = TALK_SYSTEM + _read_memory()
            self._memory_t = time.time()

    def _body(self, user_parts: list) -> dict:
        self._refresh_system()
        contents = [{"role": m["role"], "parts": [{"text": m["text"]}]}
                    for m in self.history]
        if contents and contents[-1]["role"] == "user":
            contents[-1]["parts"] += user_parts       # keep roles alternating
        else:
            contents.append({"role": "user", "parts": user_parts})
        return {"systemInstruction": {"parts": [{"text": self._system}]},
                "contents": contents,
                "tools": TOOLS,
                "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
                "generationConfig": {"maxOutputTokens": 1024}}

    # ---- quota awareness: which models may be asked right now ----
    def _load_down(self):
        try:
            with open(QUOTA_FILE, encoding="utf-8") as f:
                d = json.load(f)
            now = time.time()
            self._down = {m: (float(t), str(w)) for m, (t, w) in d.items()
                          if float(t) > now}
        except (OSError, ValueError, TypeError):
            self._down = {}

    def _set_down(self, model: str, seconds: float, why: str):
        self._down[model] = (time.time() + seconds, why)
        print(f"[talk] {model} sits out {seconds / 60:.0f} min ({why} quota/load)",
              flush=True)
        if why == "day":
            try:
                with open(QUOTA_FILE, "w", encoding="utf-8") as f:
                    json.dump({m: list(v) for m, v in self._down.items()
                               if v[1] == "day"}, f)
            except OSError:
                pass

    def _models(self) -> list:
        now = time.time()
        return [m for m in CHAIN if self._down.get(m, (0, ""))[0] <= now]

    def usable(self) -> bool:
        """Is any Gemini model allowed to answer right now?"""
        return bool(self.key) and bool(self._models())

    def out_until(self) -> str:
        """When the voice's first model is back, for "out of quota" lines."""
        times = [t for t, w in self._down.values()]
        if not times or self._models():
            return ""
        return datetime.datetime.fromtimestamp(min(times)).strftime("%H:%M")

    def quota_note(self) -> str:
        """Which models are resting, for the UI ('' = all fine)."""
        now = time.time()
        out = [f"{label(m)} {w} quota until "
               f"{datetime.datetime.fromtimestamp(t):%H:%M}"
               for m, (t, w) in self._down.items() if t > now and w == "day"]
        return "; ".join(out)

    async def reply(self, user_parts: list, on_text, on_call) -> dict:
        """Stream one answer. on_text(str) gets spoken text as it arrives;
        on_call(name, args) gets each function call the moment it lands.
        Returns {"ok", "model", "error", "ttft"}."""
        if not self.key:
            return {"ok": False, "error": "no Gemini key", "model": "", "ttft": None}
        models = self._models()
        if not models:
            return {"ok": False, "error": f"every Gemini model is out of quota "
                    f"(back {self.out_until()})", "model": "", "ttft": None}
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        body = self._body(user_parts)
        t0 = time.monotonic()
        calls = [_Call(models[0], body, self.key, loop, q)]
        nxt_i = 1                  # next model in the chain not yet asked
        hedged = False
        failed: set = set()
        winner = None
        err = ""
        ttft = None
        got_text = got_call = False

        def launch(why: str):
            nonlocal nxt_i
            m = models[nxt_i]
            nxt_i += 1
            calls.append(_Call(m, body, self.key, loop, q))
            print(f"[talk] {why}: racing {m}", flush=True)

        try:
            while True:
                now = time.monotonic()
                if winner is None:
                    if now - t0 >= FIRST_TOKEN_S:
                        err = err or f"no answer in {FIRST_TOKEN_S:.0f}s"
                        break
                    if not hedged and nxt_i < len(models) and now - t0 >= HEDGE_AFTER_S:
                        hedged = True
                        launch(f"{models[0]} silent {now - t0:.1f}s")
                    nxt = (t0 + HEDGE_AFTER_S if not hedged and nxt_i < len(models)
                           else t0 + FIRST_TOKEN_S)
                    wait = max(0.05, nxt - now)
                else:
                    # 2026-10-24: a stream that had already handed the job to
                    # Claude stayed open with nothing more to say, and the
                    # turn waited out the 20 s idle limit (the "handing that
                    # to Claude" ack landed 38 s late). After a call there is
                    # nothing left worth waiting for.
                    wait = (CALL_TAIL_S if got_call else
                            TEXT_IDLE_S if got_text else STREAM_IDLE_S)
                try:
                    call, kind, data = await asyncio.wait_for(q.get(), wait)
                except asyncio.TimeoutError:
                    if winner is not None:
                        err = "" if got_call else "stream stalled"
                        break
                    continue
                if winner is None:
                    if kind in ("text", "call"):
                        winner = call
                        ttft = time.monotonic() - t0
                        for c in calls:
                            if c is not call:
                                c.cancel()
                    else:                       # error, or ended empty
                        failed.add(call)
                        err = data if kind == "error" else "empty answer"
                        print(f"[talk] {call.model}: {err}", flush=True)
                        if call.cooldown:
                            self._set_down(call.model, *call.cooldown)
                        if nxt_i < len(models):
                            hedged = True
                            launch(f"{call.model} failed")
                            continue
                        if len(failed) >= len(calls):
                            break
                        continue
                if call is not winner:
                    continue
                if kind == "text":
                    got_text = True
                    on_text(data)
                elif kind == "call":
                    got_call = True
                    on_call(data.get("name", ""), data.get("args") or {})
                elif kind == "end":
                    err = ""
                    break
                elif kind == "error":
                    err = data
                    break
        finally:
            for c in calls:
                if c is not winner or err:
                    c.cancel()
        if winner is not None:
            self.model = winner.model
            self.ttfts.append(ttft)
            self.fail_streak = 0
            self.last_error = err
            return {"ok": True, "model": winner.model, "error": err, "ttft": ttft}
        self.fail_streak += 1
        self.last_error = err
        return {"ok": False, "model": "", "error": err, "ttft": None}


# The voice said it involved Claude but called no tool (measured 2026-10-24:
# Flash-Lite said "I've passed that request to Claude" and passed nothing).
# The app makes the claim true instead of letting it stand as a lie.
CLAIM_RE = re.compile(
    r"\b(?:i(?:'ve| have)?|i'll|let me|i'm)\s+(?:just\s+)?"
    r"(?:pass(?:ed|ing)?|hand(?:ed|ing)?|sen[dt]|send(?:ing)?|g[ia]ve|giv(?:e|ing)|"
    r"t[eo]ld|tell(?:ing)?|ask(?:ed|ing)?|forward(?:ed|ing)?|let)\b[^.?!]{0,40}\bclaude\b|"
    r"\bclaude(?:'s| is| will)\s+(?:now\s+|already\s+)?"
    r"(?:on it|working on (?:it|that|this)|handling|taking care|adjusting|changing|fixing|"
    r"doing (?:it|that)|"
    # 2026-10-24 22:57: "Claude's starting the shell rebuild now" twice, with
    # no job running. (goat_app only acts on these while Claude is idle.)
    r"(?:starting|beginning|running|building|rebuilding|making|writing|"
    r"drafting|updating|checking|looking into|getting)\b)|"
    r"Claude-ს\s+(?:გადავეცი|ვუთხარი|ვაბარებ)", re.IGNORECASE)

# "what's it doing / is it done / how's it going" — answerable from the
# watch alone when Gemini is out of quota.
STATUS_Q_RE = re.compile(
    r"\b(?:what(?:'s| is)\s+(?:it|claude|he)\s+doing|how(?:'s| is)\s+(?:it|the job|claude)"
    r"\s+going|is\s+(?:it|claude|the job)\s+(?:done|finished|ready|still)|status|progress|"
    r"done yet|finished yet|still working)\b|"
    r"რას\s+აკეთებს|როგორ\s+მიდის|დაამთავრა|მზადაა", re.IGNORECASE)


def screenshot_part(max_edge: int = 1280) -> dict | None:
    """His whole screen as an inline JPEG part for Gemini, or None."""
    try:
        import screen_hands
        from PIL import Image
        png, _meta = screen_hands.capture(max_edge=max_edge, grid=False, cursor=False)
        img = Image.open(io.BytesIO(png)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return {"inlineData": {"mimeType": "image/jpeg",
                               "data": base64.b64encode(buf.getvalue()).decode()}}
    except Exception as e:  # noqa: BLE001
        print(f"[talk] screenshot failed: {type(e).__name__}: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# The voice's eyes on the working brain
# ---------------------------------------------------------------------------

CHECKIN_FIRST_S = 75.0     # first progress word on a long job
CHECKIN_EVERY_S = 210.0    # then this often...
CHECKIN_MAX = 3            # ...at most this many times per job
CHECKIN_ENGAGED_S = 45.0   # he talked to GOAT this recently: no unasked check-in
STUCK_QUIET_S = 150.0      # no new step, thought or note for this long
STUCK_REPEAT = 4           # the same step this many times in a row
STUCK_ERRORS = 4           # this many failing tool calls in a row
STUCK_REARM_S = 360.0      # after a stuck warning, quiet for this long
NOTE_GAP_S = 40.0          # Claude's own notes to the voice, at most this often
HOLD_MAX_S = 600.0         # side talk/calls can hold news this long


def _ago(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    return f"{m} min {s:02d} s" if m < 10 else f"{m} min"


class WorkerWatch:
    """Everything the voice knows about Claude's current and last job."""

    def __init__(self):
        self.job = None            # dict while a job exists (running or finished)
        self.n = 0
        self.pending: list = []    # events waiting to be voiced
        self._lock = threading.Lock()

    # ---- job lifecycle (called by the app) ----
    def start(self, task: str, his: str, model: str, depth: str):
        with self._lock:
            self.n += 1
            now = time.monotonic()
            self.job = {"id": self.n, "task": task, "his": his, "model": model,
                        "depth": depth, "t0": now, "last": now, "state": "running",
                        "steps": deque(maxlen=12), "think": "", "text": "",
                        "same": 0, "errors": 0, "checkins": 0,
                        "next_checkin": now + CHECKIN_FIRST_S,
                        "stuck_at": 0.0, "notes": [], "last_note": 0.0,
                        "added": [], "report": "", "t_end": 0.0}
            # news about an older job that never got said is stale now
            self.pending = [p for p in self.pending if p["kind"] in ("done", "failed")]

    def running(self) -> bool:
        j = self.job
        return bool(j and j["state"] == "running")

    def finish(self, state: str, report: str):
        """state: done | failed | limit | stopped | lost."""
        with self._lock:
            j = self.job
            if not j or j["state"] != "running":
                return
            j["state"] = state
            j["report"] = (report or "").strip()
            j["t_end"] = time.monotonic()
            if state == "stopped":
                return                  # he stopped it: the voice already said so
            kind = "done" if state == "done" else "failed"
            self.pending = [p for p in self.pending if p["job"] != j["id"]]
            self.pending.append({"kind": kind, "job": j["id"], "t": time.monotonic(),
                                 "state": state})

    # ---- live events (tee'd from the app's emit) ----
    def on_event(self, kind: str, data: str):
        j = self.job
        if not j or j["state"] != "running":
            return
        now = time.monotonic()
        j["last"] = now
        if kind in ("work_tool", "work_step"):
            d = " ".join(str(data).split())[:160]
            if j["steps"] and j["steps"][-1][0] == d:
                j["same"] += 1
            else:
                j["same"] = 0
            j["steps"].append((d, now))
        elif kind == "work_think":
            j["think"] = (j["think"] + str(data))[-600:]
        elif kind == "work_text":
            j["text"] = (j["text"] + str(data))[-800:]

    def on_tool_result(self, is_error: bool):
        j = self.job
        if not j or j["state"] != "running":
            return
        j["last"] = time.monotonic()
        j["errors"] = j["errors"] + 1 if is_error else 0

    def note_from_worker(self, message: str, kind: str = "update"):
        """Claude's tell_voice tool: something it wants Giorgi to hear now."""
        with self._lock:
            j = self.job
            if not j:
                return "no job is running"
            now = time.monotonic()
            j["last"] = now
            j["notes"].append((kind, message.strip()[:400], now))
            if kind != "question" and now - j["last_note"] < NOTE_GAP_S:
                return "noted (the voice spoke for you moments ago; it will " \
                       "include this if he asks)"
            j["last_note"] = now
            self.pending.append({"kind": "note", "job": j["id"], "t": now,
                                 "note": message.strip()[:400], "nkind": kind})
            return "the voice will tell him"

    def added(self, text: str):
        j = self.job
        if j and j["state"] == "running":
            j["added"].append(text[:200])
            j["last"] = time.monotonic()

    # ---- what the voice sees ----
    def status_note(self, worker_label: str, effort: str, extra: str = "") -> str:
        """The [Claude] block for every Gemini turn."""
        j = self.job
        now = time.monotonic()
        head = f"[Claude — your partner, the working brain: {worker_label}"
        head += f", thinking at {effort}" if effort else ""
        head += "]"
        if extra:
            head += "\n" + extra
        if not j:
            return head + "\nNo job yet this session. Claude is free."
        if j["state"] == "running":
            lines = [head,
                     f"WORKING for {_ago(now - j['t0'])} on the job you handed it: "
                     f"\"{j['task'][:300]}\""]
            if j["added"]:
                lines.append("notes you passed it since: " + " | ".join(j["added"][-3:]))
            if j["steps"]:
                recent = list(j["steps"])[-6:]
                lines.append("its steps so far (oldest first): " + "; ".join(
                    f"{d} ({_ago(now - t)} ago)" for d, t in recent))
                lines.append(f"current step: {recent[-1][0]}, for {_ago(now - recent[-1][1])}")
            else:
                lines.append("no tool step yet — it is still thinking it through")
            if j["think"].strip():
                lines.append("its latest thinking: …" + " ".join(j["think"].split())[-300:])
            if j["text"].strip():
                lines.append("its latest words: …" + " ".join(j["text"].split())[-300:])
            if j["notes"]:
                lines.append("its notes to you: " + " | ".join(n for _, n, _ in j["notes"][-3:]))
            quiet = now - j["last"]
            if quiet > 60:
                lines.append(f"(no new activity for {_ago(quiet)})")
            # 2026-10-24 22:45: its words said "finished the UI suites" about
            # one step and the voice told him the whole redesign was "done
            # and live" while Claude was still mid-job.
            lines.append("STATUS: STILL RUNNING — NOT finished. Its words and steps "
                         "above are progress INSIDE the job: even when they say "
                         "\"done\" or \"finished\" about a step, the job is not done "
                         "and nothing is live until an [event] says Claude FINISHED.")
            return "\n".join(lines)
        verdict = {"done": "FINISHED", "failed": "FAILED", "limit":
                   "STOPPED — Claude hit its usage limit", "stopped":
                   "was STOPPED by Giorgi", "lost": "was LOST (engine restart)"}[j["state"]]
        lines = [head, f"Free now. Its last job {verdict} {_ago(now - j['t_end'])} ago "
                       f"after {_ago(j['t_end'] - j['t0'])}: \"{j['task'][:200]}\""]
        if j["report"]:
            lines.append("its report: " + " ".join(j["report"].split())[-900:])
        return "\n".join(lines)

    # ---- the watch: is there something worth saying? ----
    def due(self, quiet: bool, engaged_s: float, held: bool) -> dict | None:
        """Next event to voice, or None. quiet = nobody is talking right now;
        engaged_s = seconds since he last spoke to GOAT; held = a call or
        side conversation is going on (news waits, up to HOLD_MAX_S)."""
        now = time.monotonic()
        with self._lock:
            if self.pending:
                ev = self.pending[0]
                if held and now - ev["t"] < HOLD_MAX_S:
                    return None
                if not quiet:
                    return None
                return self.pending.pop(0)
            j = self.job
            if not j or j["state"] != "running" or not quiet or held:
                return None
            # stuck?
            if now - j["stuck_at"] > STUCK_REARM_S:
                why = ""
                if now - j["last"] > STUCK_QUIET_S:
                    cur = j["steps"][-1][0] if j["steps"] else "thinking"
                    why = f"no new step for {_ago(now - j['last'])}; current step: {cur}"
                elif j["same"] + 1 >= STUCK_REPEAT:
                    why = f"repeating the same step {j['same'] + 1} times: {j['steps'][-1][0]}"
                elif j["errors"] >= STUCK_ERRORS:
                    why = f"its last {j['errors']} tool calls failed"
                if why:
                    j["stuck_at"] = now
                    j["next_checkin"] = now + CHECKIN_EVERY_S
                    return {"kind": "stuck", "job": j["id"], "t": now, "why": why}
            # progress check-in on a long job, only if he isn't already engaged
            if (now >= j["next_checkin"] and j["checkins"] < CHECKIN_MAX
                    and engaged_s >= CHECKIN_ENGAGED_S):
                j["checkins"] += 1
                j["next_checkin"] = now + CHECKIN_EVERY_S
                return {"kind": "checkin", "job": j["id"], "t": now}
            if now >= j["next_checkin"]:
                j["next_checkin"] = now + 30     # engaged: look again shortly
        return None

    def take_final(self) -> dict | None:
        """A finished-job event not yet voiced: the voice folds it into the
        turn it is about to answer, so it never arrives twice."""
        with self._lock:
            for i, ev in enumerate(self.pending):
                if ev["kind"] in ("done", "failed"):
                    return self.pending.pop(i)
        return None

    def event_prompt(self, ev: dict) -> str:
        """The [event] message Gemini turns into speech."""
        j = self.job
        now = time.monotonic()
        task = (j or {}).get("task", "")[:300]
        if ev["kind"] == "done":
            return (f"[event] Claude FINISHED the job you handed it (\"{task}\") after "
                    f"{_ago(j['t_end'] - j['t0'])}. Its report to you:\n"
                    f"{(j['report'] or '(no words — it just finished)')[-2500:]}\n"
                    "Tell Giorgi the outcome in your own words, one or two short "
                    "spoken sentences, result first, crediting Claude (\"Claude's "
                    "done — ...\", \"Claude found ...\"), never \"I did\". If the "
                    "report asks him something or needs his decision, ask it "
                    "plainly.")
        if ev["kind"] == "failed":
            why = {"limit": "Claude ran out of usage mid-job",
                   "lost": "GOAT's engine restarted and the job was lost"}.get(
                       ev.get("state"), "the job failed")
            return (f"[event] {why} (\"{task}\"). What it said:\n"
                    f"{(j['report'] or '(nothing)')[-1500:]}\n"
                    "Tell Giorgi plainly in one or two sentences what went "
                    "wrong and the obvious next step.")
        if ev["kind"] == "stuck":
            return (f"[event] The watch thinks Claude may be stuck: {ev['why']}. "
                    "Tell Giorgi in one or two sentences what it's stuck on and "
                    "ask whether to let it keep going or stop it.")
        if ev["kind"] == "checkin":
            return (f"[event] Progress check — Claude has been on the job for "
                    f"{_ago(now - j['t0'])}. Give Giorgi one short, natural "
                    "progress line from the [Claude] note (what it's doing now). "
                    "Don't ask him anything.")
        if ev["kind"] == "note":
            if ev.get("nkind") == "question":
                return (f"[event] Claude asks Giorgi, mid-job: \"{ev['note']}\" "
                        "Ask him in your own words, one sentence.")
            return (f"[event] Claude sent you a mid-job note for Giorgi: "
                    f"\"{ev['note']}\" Pass it on in one short sentence.")
        return "[event] (nothing)"

    def local_status(self, lang: str = "en") -> str:
        """A spoken status line built from the watch alone (no model)."""
        j = self.job
        now = time.monotonic()
        if not j:
            return ("ბატონო, Claude ახლა თავისუფალია." if lang == "ka"
                    else "Sir, Claude's free — no job running.")
        if j["state"] == "running":
            mins = int((now - j["t0"]) // 60)
            age = (f"{mins} წუთია" if lang == "ka" else f"{mins} minutes in") if mins \
                else ("ახლახან დაიწყო" if lang == "ka" else "just started")
            step = j["steps"][-1][0].split(" — ")[0] if j["steps"] else ""
            if lang == "ka":
                return f"ბატონო, Claude ჯერ მუშაობს, {age}." + (f" ახლა: {step}." if step else "")
            return f"Sir, Claude's still on it, {age}." + (f" Right now: {step}." if step else "")
        if lang == "ka":
            return "ბატონო, Claude-მა დაასრულა." if j["state"] == "done" else \
                "ბატონო, Claude-ის ბოლო დავალება ვერ შესრულდა."
        rep = re.split(r"(?<=[.!?])\s", " ".join((j["report"] or "").split()), 1)[0][:160]
        return (("Sir, Claude finished. " + rep) if j["state"] == "done"
                else "Sir, Claude's last job didn't finish.").strip()

    def fallback_line(self, ev: dict, lang: str = "en") -> str:
        """What to say if Gemini can't be reached for an event."""
        j = self.job or {}
        rep = " ".join((j.get("report") or "").split())
        first = re.split(r"(?<=[.!?])\s", rep, 1)[0][:160] if rep else ""
        if lang == "ka":
            return {"done": "ბატონო, Claude-მა დაასრულა.", "failed": "ბატონო, Claude-ს არ გამოუვიდა.",
                    "stuck": "ბატონო, Claude ერთ ადგილზე გაიჭედა — გავაჩერო?",
                    "checkin": "ბატონო, Claude ჯერ კიდევ მუშაობს.",
                    "note": "ბატონო, Claude-ს შეტყობინება: " + ev.get("note", "")
                    }.get(ev["kind"], "")
        return {"done": ("Sir, Claude's finished. " + first).strip(),
                "failed": ("Sir, Claude's job failed. " + first).strip(),
                "stuck": "Sir, Claude seems stuck. Should I stop it?",
                "checkin": "Sir, Claude's still working on it.",
                "note": "Sir, Claude says: " + ev.get("note", "")}.get(ev["kind"], "")


# ---------------------------------------------------------------------------
# Fable 5.1 gate
# ---------------------------------------------------------------------------

class FableGate:
    """Can this account use Fable 5.1 right now? Fable bills from prepaid
    usage credits; with none, every Fable turn is refused instantly
    ("Fable 5.1 requires usage credits", measured 2026-10-24). One probe at
    boot, cached for PROBE_TTL; a refusal mid-session flips it off."""

    PROBE_TTL = 6 * 3600
    STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fable-gate.json")

    def __init__(self):
        self.ok = False
        self.reason = "not checked yet"
        self.checked = 0.0
        try:
            with open(self.STATE, encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - float(d.get("t", 0)) < self.PROBE_TTL:
                self.ok, self.reason = bool(d.get("ok")), str(d.get("reason", ""))
                self.checked = float(d["t"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def fresh(self) -> bool:
        return time.time() - self.checked < self.PROBE_TTL

    def _save(self):
        try:
            with open(self.STATE, "w", encoding="utf-8") as f:
                json.dump({"t": self.checked, "ok": self.ok, "reason": self.reason}, f)
        except OSError:
            pass

    def refused(self, reason: str):
        self.ok, self.reason, self.checked = False, reason, time.time()
        self._save()

    def probe(self, model_id: str, cwd: str) -> bool:
        """Blocking (run on a thread). Uses the SDK's bundled claude.exe."""
        try:
            import claude_agent_sdk
            exe = os.path.join(os.path.dirname(claude_agent_sdk.__file__),
                               "_bundled", "claude.exe")
            out = subprocess.run(
                [exe, "--model", model_id, "-p", "Reply with exactly: OK",
                 "--output-format", "json", "--setting-sources", "project",
                 "--strict-mcp-config"],
                cwd=cwd, capture_output=True, text=True, timeout=90,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            d = json.loads(out.stdout or "{}")
            res = str(d.get("result") or "")
            if d.get("is_error") or not res.strip():
                self.ok = False
                self.reason = ("needs usage credits" if "credit" in res.lower()
                               else (res[:80] or "refused"))
            else:
                self.ok, self.reason = True, "available"
        except Exception as e:  # noqa: BLE001 — unknown = not available
            self.ok, self.reason = False, f"probe failed: {type(e).__name__}"
        self.checked = time.time()
        self._save()
        print(f"[fable] {model_id}: {'OK' if self.ok else self.reason}", flush=True)
        return self.ok


def clock_note() -> str:
    now = datetime.datetime.now()
    return f"[now: {now:%A %d %B %Y, %H:%M}]"


# ---------------------------------------------------------------------------
# Claude's side of the relationship: a line to the voice
# ---------------------------------------------------------------------------

WATCH = WorkerWatch()

TELL_VOICE_DESC = (
    "Send GOAT's voice (Gemini, who is talking with Giorgi right now) a short "
    "note to pass on to him WHILE you keep working: a milestone worth "
    "hearing (\"found the bug, fixing it now\"), how long this will take, or "
    "a question he can answer while you continue (kind=question). Use it "
    "rarely — at most once every minute or two on a long job, never for "
    "routine steps. Your final message still goes to the voice as your "
    "report when you finish; if you can't continue without his decision, "
    "end your turn with the question instead.")

try:
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("tell_voice", TELL_VOICE_DESC, {
        "type": "object",
        "properties": {
            "message": {"type": "string",
                        "description": "One plain sentence for Giorgi."},
            "kind": {"type": "string", "enum": ["update", "question"]}},
        "required": ["message"]})
    async def tell_voice(args: dict) -> dict:
        msg = str(args.get("message") or "").strip()
        if not msg:
            return {"content": [{"type": "text", "text": "empty message"}],
                    "isError": True}
        kind = "question" if args.get("kind") == "question" else "update"
        res = WATCH.note_from_worker(msg, kind)
        return {"content": [{"type": "text", "text": res}]}

    VOICE_SERVER = create_sdk_mcp_server("voice", "1.0.0", [tell_voice])
except Exception as _e:  # noqa: BLE001 — the voice link is optional
    VOICE_SERVER = None
    print(f"[talk] tell_voice unavailable: {_e}")
