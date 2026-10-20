"""GOAT's inner life: mood, thinking in quiet moments, and speaking first.

Layers 4-6 of his 2026-10-20 ask ("give the GOAT the consciousness"; layers
1-3 are self.md, body_sense.py and episodes.py):

  Mood        — tone read from local signals (his frustration, repeats, my
                own failed attempts, the hour, a fast back-and-forth). Zero
                Claude calls. The brain hears it only when it CHANGES.
  Reflection  — after 20 quiet minutes with enough new talk, one Haiku call
                thinks privately about it: patterns, what he's stuck on, my
                mistakes. Thoughts go to journal/thoughts.md and the "On my
                mind" block of self.md, and ride once on his next turn.
  Initiative  — a reflection may leave one line worth saying. GOAT says it
                first, unprompted, at a decent moment only: when he comes
                back to the laptop, or after a long quiet stretch while he's
                there. Never in a call, a fullscreen game/video, while muted,
                asleep, on the phone, or mid-conversation.

Cost (his question, same day; weekly quota was at 86%): reflections are
capped at 6 a day, skipped when the 5-hour quota is above 70% or the week
above 95%; mood and initiative are pure local reads.
"""
import asyncio
import ctypes
import datetime
import difflib
import json
import os
import re
import threading
import time
import winreg
from ctypes import wintypes

import psutil

import episodes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
THOUGHTS = os.path.join(episodes.JOURNAL, "thoughts.md")
STATE = os.path.join(ROOT, ".inner_life.json")
T_START, T_END = "<!-- thoughts:start -->", "<!-- thoughts:end -->"

POLL_S = 30
IDLE_REFLECT_S = 20 * 60      # quiet this long before thinking
MIN_NEW = 6                   # new exchanges worth a reflection
REFLECT_PER_DAY = 6
SAY_GAP_S = 20 * 60           # between two unprompted lines
SAY_PER_DAY = 6
SAY_TTL_S = 3 * 3600          # a thought older than this is stale news
AWAY_S = 15 * 60              # back after this long away = natural moment
QUIET_EXCHANGE_S = 30 * 60    # or: he's here and we haven't talked this long
KEEP_THOUGHTS = 4             # bullets kept in self.md

REFLECT_SYSTEM = (
    "You are GOAT, Giorgi's personal voice assistant, thinking privately in "
    "a quiet moment about your recent conversation with him. First person, "
    "plain English. Write 2 to 4 short, concrete thoughts: a pattern in what "
    "he asks, something he's stuck on and how I could help, a mistake I made "
    "and what I'll do differently, something worth remembering. Only what "
    "the transcript supports: no invented feelings, no flattery, no filler.\n"
    "Then one line starting SAY: with ONE sentence I'd say to him unprompted "
    "when he's back, only if it genuinely helps him (a fix or better idea for "
    "what he's stuck on, a thing he asked for and that got dropped). It must "
    "be true the moment it's spoken: an offer (\"Want me to…?\") or a fact, "
    "never a promise that I did or will do something on my own. Never "
    "small talk, never checking in, never asking how he is. Otherwise "
    "SAY: none.\n\nExact format:\n- <thought>\n- <thought>\nSAY: <sentence or none>")

# His corrections and annoyance, as he actually talks (voice, often terse).
FRUSTRATED_RE = re.compile(
    r"\b(wrong|again|still (?:not|doesn'?t|isn'?t|broken)|i (?:said|told you)|"
    r"why (?:did|didn'?t|isn'?t|is it|are you)|not working|doesn'?t work|"
    r"come on|what the|wtf|fuck\w*|shit|damn|useless|stupid|"
    r"(?:please )?repeat|you didn'?t understand)\b", re.I)
MY_FAIL_RE = re.compile(
    r"\b(failed|couldn'?t|can'?t|error|didn'?t work|unable|sorry|"
    r"my mistake|i was wrong)\b", re.I)


# ---------------------------------------------------------------- senses --

class _LII(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def input_idle_s() -> float:
    """Seconds since his last keyboard/mouse input on the laptop."""
    lii = _LII()
    lii.cbSize = ctypes.sizeof(lii)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - lii.dwTime)
            & 0xFFFFFFFF) / 1000.0


class _MI(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def front_fullscreen() -> bool:
    """A game or a fullscreen video in front (covers the whole monitor,
    taskbar included — a maximized window doesn't)."""
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    if not h:
        return False
    cls = ctypes.create_unicode_buffer(64)
    u.GetClassNameW(h, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False          # the desktop itself
    r = wintypes.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    mi = _MI()
    mi.cbSize = ctypes.sizeof(_MI)
    if not u.GetMonitorInfoW(u.MonitorFromWindow(h, 2), ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    return (r.left <= m.left and r.top <= m.top
            and r.right >= m.right and r.bottom >= m.bottom)


_MIC_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
            r"\CapabilityAccessManager\ConsentStore\microphone")


def other_app_on_mic() -> bool:
    """Another app holds the microphone right now (a call, game voice chat).
    Windows keeps LastUsedTimeStop=0 while an app records — but a crashed
    app leaves a stale 0 (Rainbow Six did, 2026-10-20), so a desktop app
    counts only while its exe is actually running."""
    running = None
    now_ft = (time.time() + 11644473600) * 1e7     # unix -> FILETIME

    def scan(path, packaged):
        nonlocal running
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return False
        with k:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                if name == "NonPackaged":
                    if scan(path + "\\NonPackaged", False):
                        return True
                    continue
                try:
                    with winreg.OpenKey(k, name) as sk:
                        stop = winreg.QueryValueEx(sk, "LastUsedTimeStop")[0]
                        start = winreg.QueryValueEx(sk, "LastUsedTimeStart")[0]
                except OSError:
                    continue
                if stop != 0 or not start or re.search(r"python|goat", name, re.I):
                    continue
                if packaged:
                    if now_ft - start < 6 * 3600 * 1e7:
                        return True
                    continue
                if running is None:
                    running = set()
                    for p in psutil.process_iter(["exe"]):
                        if p.info.get("exe"):
                            running.add(p.info["exe"].lower())
                if name.replace("#", "\\").lower() in running:
                    return True
        return False
    try:
        return scan(_MIC_KEY, True)
    except Exception:  # noqa: BLE001 — unknown = don't block on it
        return False


def _plan() -> dict:
    try:
        with open(episodes.PLAN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def recent_entries(since_t: float, max_bytes: int = 160_000) -> list:
    """Transcript entries newer than since_t (reads only the file's tail)."""
    try:
        with open(episodes.TRANSCRIPT, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for ln in raw.splitlines():
        try:
            e = json.loads(ln)
            if float(e["t"]) > since_t:
                out.append(e)
        except (ValueError, KeyError, TypeError):
            continue
    return out


# ------------------------------------------------------------------ mood --

def read_mood(text: str, entries: list, now: datetime.datetime):
    """(key, hint) from the last 30 minutes of talk plus what he just said."""
    his = [str(e.get("user", "")) for e in entries] + [text]
    mine = [str(e.get("reply", ""))[:400] for e in entries]
    signs = sum(1 for u in his if FRUSTRATED_RE.search(u))
    last = [re.sub(r"\W+", " ", u.lower()).strip() for u in his[-4:]]
    repeats = sum(1 for i in range(len(last)) for j in range(i + 1, len(last))
                  if last[i] and difflib.SequenceMatcher(
                      None, last[i], last[j]).ratio() > 0.85)
    if signs + repeats >= 2:
        return ("frustrated",
                f"he seems frustrated ({signs + repeats} signs in the last "
                "30 min): fewest words, no wit, fix it first, own any mistake "
                "in half a sentence")
    fails = sum(1 for r in mine[-6:] if MY_FAIL_RE.search(r))
    if fails >= 3:
        return ("struggling",
                "your last few attempts went badly: say plainly what you'll "
                "do differently, no confidence you haven't earned")
    if 1 <= now.hour < 6:
        return ("late", f"it's {now:%H:%M}, deep night: quieter and shorter; "
                        "one gentle line about rest only at a natural pause")
    if len(entries) >= 8:
        return ("flow", "he's in flow, quick turns: match the pace, "
                        "zero preamble")
    return ("steady", "")


# ------------------------------------------------------------ inner life --

class InnerLife:
    def __init__(self, app):
        self.app = app
        self.st = self._load()
        self._mood = "steady"
        self._mood_said = False
        self._note = ""               # thoughts waiting for his next turn
        self._prev_idle = 0.0
        self._lock = threading.Lock()

    # ---- state ----
    def _load(self) -> dict:
        try:
            with open(STATE, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        st.setdefault("reflect_t", time.time())   # never digest old history
        st.setdefault("say", [])
        return st

    def _save(self):
        try:
            tmp = STATE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.st, f)
            os.replace(tmp, STATE)
        except OSError:
            pass

    def _count(self, key: str) -> int:
        today = episodes.day_of(time.time()).isoformat()
        if self.st.get("day") != today:
            self.st.update(day=today, reflects=0, says=0)
        return self.st.get(key, 0)

    # ---- per turn ----
    def turn_note(self, text: str) -> str:
        """Mood (on change only) + thoughts from a quiet moment (once)."""
        parts = []
        try:
            ents = recent_entries(time.time() - 30 * 60, 60_000)
            key, hint = read_mood(text, ents, datetime.datetime.now())
            if key != self._mood:
                if key == "steady":
                    parts.append("[mood — back to steady: normal tone]")
                else:
                    parts.append(f"[mood — {hint}]")
                self._mood = key
        except Exception as e:  # noqa: BLE001
            print(f"[mood] {type(e).__name__}: {e}")
        with self._lock:
            if self._note:
                parts.append("[on your mind — your own thoughts from a quiet "
                             "moment; context only, don't recite them]\n"
                             + self._note)
                self._note = ""
        return "\n".join(parts)

    # ---- background ----
    def start(self, delay: float = 120):
        def loop():
            time.sleep(delay)
            while True:
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001 — never hurts GOAT
                    print(f"[inner] {type(e).__name__}: {e}")
                time.sleep(POLL_S)
        threading.Thread(target=loop, daemon=True, name="inner-life").start()

    def tick(self):
        now = time.time()
        if self._quiet_for() >= IDLE_REFLECT_S and self._may_reflect():
            self.reflect()
        self._maybe_speak(now)

    def _quiet_for(self) -> float:
        last = getattr(self.app, "_last_exchange", None)
        return time.monotonic() - last if last is not None else 0.0

    def _asleep_or_out(self) -> bool:
        a = self.app
        if getattr(a, "_asleep", False):
            return True
        hit = getattr(a, "_limit_hit_at", 0.0) or 0.0
        reset_at = getattr(a, "claude_reset_at", 0.0) or 0.0
        return bool(hit and (not reset_at or time.time() < reset_at))

    def _may_reflect(self) -> bool:
        if self._asleep_or_out() or getattr(self.app, "busy", False):
            return False
        if self._count("reflects") >= REFLECT_PER_DAY:
            return False
        p = _plan()
        if float(p.get("five_hour") or 0) > 70 or float(p.get("seven_day") or 0) > 95:
            return False
        return len(recent_entries(self.st["reflect_t"])) >= MIN_NEW

    def reflect(self) -> str:
        ents = recent_entries(self.st["reflect_t"])[-60:]
        if not ents:
            return ""
        try:
            with open(episodes.SELF_MD, encoding="utf-8") as f:
                me = f.read()[:3500]
        except OSError:
            me = ""
        now = datetime.datetime.now()
        prompt = (f"Now: {now:%A %H:%M}.\n\nWho I am:\n{me}\n\n"
                  "Our talk since my last quiet moment:\n"
                  + episodes.render(ents))
        text = asyncio.run(episodes._ask(prompt, REFLECT_SYSTEM))
        thoughts = [ln[2:].strip() for ln in text.splitlines()
                    if ln.startswith("- ")][:4]
        m = re.search(r"^SAY:\s*(.+)$", text, re.M)
        say = (m.group(1).strip() if m else "")
        if say.lower().rstrip(".") in ("none", "", "n/a"):
            say = ""
        self.st["reflect_t"] = float(ents[-1]["t"])
        self.st["reflects"] = self._count("reflects") + 1
        if not thoughts:
            self._save()
            print(f"[inner] odd reflection, skipped: {text[:120]!r}")
            return ""
        stamp = f"{now:%Y-%m-%d %H:%M}"
        os.makedirs(episodes.JOURNAL, exist_ok=True)
        with open(THOUGHTS, "a", encoding="utf-8") as f:
            f.write(f"\n## {stamp}\n" + "".join(f"- {t}\n" for t in thoughts)
                    + (f"- (to say) {say}\n" if say else ""))
        write_thoughts(thoughts, f"{now:%m-%d %H:%M}")
        with self._lock:
            self._note = "\n".join(f"- {t}" for t in thoughts)
        if say:
            self.st["say"].append({"line": say, "t": time.time()})
        self._save()
        print(f"[inner] reflected: {thoughts[0][:90]}" + (" | will say" if say else ""))
        return text

    # ---- initiative ----
    def _moment(self, idle: float) -> str:
        """Why now is a decent moment to speak first, or ''."""
        came_back = self._prev_idle >= AWAY_S and idle < 15
        if came_back:
            return "back"
        if idle < 60 and self._quiet_for() >= QUIET_EXCHANGE_S:
            return "quiet"
        return ""

    def _can_speak(self) -> bool:
        a = self.app
        if self._asleep_or_out() or getattr(a, "mic_muted", False):
            return False
        if getattr(a, "phone_live", False):
            return False                        # he's on the phone, not here
        try:
            if a.busy or a.tts.speaking() or a.audio.is_tts_playing:
                return False
        except Exception:  # noqa: BLE001
            return False
        if self._quiet_for() < 120:
            return False                        # mid-conversation
        if time.time() - self.st.get("last_say", 0) < SAY_GAP_S:
            return False
        if self._count("says") >= SAY_PER_DAY:
            return False
        return not front_fullscreen() and not other_app_on_mic()

    def _maybe_speak(self, now: float):
        idle = input_idle_s()
        why = self._moment(idle)                # compares with the last poll
        self._prev_idle = idle
        self.st["say"] = [x for x in self.st["say"] if now - x["t"] < SAY_TTL_S]
        if not self.st["say"] or not why or not self._can_speak():
            return
        line = self.st["say"].pop(0)["line"]
        if not line.lower().startswith("sir"):
            line = "Sir, " + line[0].lower() + line[1:]
        self.st["last_say"] = now
        self.st["says"] = self._count("says") + 1
        self._save()
        a = self.app
        print(f"[inner] spoke first ({why}): {line}")

        def say():
            a._say_now(line)
            try:      # the brain must know it said this
                a._local_unseen.append(("(nothing — you spoke first, unprompted)",
                                        line[:200]))
            except Exception:  # noqa: BLE001
                pass
        a.loop.call_soon_threadsafe(say)


def write_thoughts(thoughts: list, stamp: str):
    """Newest thoughts into self.md's "On my mind" block (KEEP_THOUGHTS)."""
    try:
        with open(episodes.SELF_MD, encoding="utf-8") as f:
            doc = f.read()
    except OSError:
        return
    if T_START not in doc or T_END not in doc:
        return
    head, rest = doc.split(T_START, 1)
    block, tail = rest.split(T_END, 1)
    old = [ln for ln in block.strip().splitlines() if ln.startswith("- ")]
    new = [f"- {stamp}: {t}" for t in thoughts]
    lines = (new + old)[:KEEP_THOUGHTS]
    doc = head + T_START + "\n" + "\n".join(lines) + "\n" + T_END + tail
    tmp = episodes.SELF_MD + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(doc)
    os.replace(tmp, episodes.SELF_MD)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print("input idle", round(input_idle_s()), "s | fullscreen", front_fullscreen(),
          "| other app on mic", other_app_on_mic())
    ents = recent_entries(time.time() - 30 * 60)
    print("mood:", read_mood("", ents, datetime.datetime.now()))
