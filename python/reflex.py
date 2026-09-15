"""GOAT's reflex lane — the commands that must feel INSTANT.

His complaint, 2026-09-15: "it takes long to do my commands, for example to
open a file on my PC or open Google — I want it to feel instant."

He was right, and the numbers said so. Measured on this machine that day:

    gemini-3.8-flash  "say ok"            19.0s   (talking brain default)
    gemini-3.5-flash  "say ok"             1.3s
    gemini-3.5-flash-lite "say ok"         0.7s

Gemini 3 models THINK and, per Google's own docs, reasoning cannot be turned
off for them — `reasoning_effort: "none"` is accepted and ignored. So every
"open Google" paid 8-19s of silent thinking, then a SECOND round trip to
narrate the tool result, then TTS. And "open the file gg" was worse: it fell
through to the work lane, which hunted the icon with screenshots.

The fix is the one every shipped voice assistant uses, and the research is
unanimous on it: a two-tier router. Rigid, high-frequency device commands are
matched DETERMINISTICALLY on-device and executed with no model in the loop;
the LLM only ever sees the weird phrasing and the open questions. Intent
match here is a regex plus a dict lookup — tens of microseconds — so the
whole perceived latency becomes "how fast can the speaker say a word", which
is a cached TTS clip, not a network.

What it covers: open a site / app / file / folder, volume, media transport,
brightness, lock, window state, screenshot, time, date, battery. Anything
else returns None in a few microseconds and the normal lanes take the turn
exactly as before. A reflex NEVER guesses: an unresolvable target falls
through to the talking brain rather than opening the wrong thing.

Both languages, because half his orders are Georgian and cloud STT garbles
casual speech — the patterns match stems, not exact words.
"""
import ctypes
import datetime
import os
import re
import subprocess
import webbrowser

import local_hands
import reflex_index

# Kill switch. On by default; GOAT_REFLEX=off sends every command back
# through the talking brain exactly as before this lane existed — a one-line
# escape hatch if the matcher ever grabs something it shouldn't. Test suites
# set it off too, so feeding real phrases through the router doesn't launch
# his browser or move his volume.
ENABLED = os.environ.get("GOAT_REFLEX", "on").strip().lower() not in (
    "off", "0", "false", "no")

# ---- sites he actually asks for, by every name he uses --------------------
SITES = {
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com", "mail": "https://mail.google.com",
    "youtube": "https://www.youtube.com", "yt": "https://www.youtube.com",
    "github": "https://github.com",
    "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai",
    "gemini": "https://gemini.google.com",
    "facebook": "https://www.facebook.com", "fb": "https://www.facebook.com",
    "instagram": "https://www.instagram.com", "insta": "https://www.instagram.com",
    "twitter": "https://x.com", "x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "linkedin": "https://www.linkedin.com",
    "upwork": "https://www.upwork.com",
    "netflix": "https://www.netflix.com",
    "twitch": "https://www.twitch.tv",
    "whatsapp": "https://web.whatsapp.com",
    "telegram": "https://web.telegram.org",
    "maps": "https://maps.google.com",
    "drive": "https://drive.google.com",
    "calendar": "https://calendar.google.com",
    "translate": "https://translate.google.com",
    "stackoverflow": "https://stackoverflow.com",
    "vercel": "https://vercel.com/dashboard",
    "supabase": "https://supabase.com/dashboard",
    "fasmetri": "https://fasmetri.vercel.app",
    # Georgian spellings of the same handful.
    "გუგლი": "https://www.google.com", "გუგლ": "https://www.google.com",
    "იუთუბი": "https://www.youtube.com", "იუტუბი": "https://www.youtube.com",
    "ფეისბუქი": "https://www.facebook.com", "ფეისბუქ": "https://www.facebook.com",
    "ინსტაგრამი": "https://www.instagram.com",
    "ფოსტა": "https://mail.google.com", "ჯიმეილი": "https://mail.google.com",
    "ფასმეტრი": "https://fasmetri.vercel.app",
}

# ---- verbs ---------------------------------------------------------------
# English openers, including the ones he says instead of "open": "pull up",
# "bring up", "fire up", "go to". Deliberately NOT here: "run" and "load".
# "run the tests" / "load the data" are jobs for the working brain, and a
# reflex that grabbed them would double-click a folder named `tests` instead.
_OPEN = (r"(?:open|launch|start|show|bring\s+up|pull\s+up|fire\s+up|"
         r"go\s+to|take\s+me\s+to)")
# Georgian: გახსენი / გამიხსენი / ჩართე / გაუშვე / შედი / ამომიგდე.
_OPEN_KA = r"(?:გა?მ?ი?ხსენ\w*|ჩართ\w*|გაუშვ\w*|შედ\w*|ამომიგდ\w*|გახსნ\w*)"

# Politeness and filler that must never change the meaning.
_LEAD = (r"(?:\s*(?:hey|ok(?:ay)?|goat|please|can\s+you|could\s+you|"
         r"would\s+you|i\s+want\s+you\s+to|i\s+need\s+you\s+to|just|now|"
         r"go\s+ahead\s+and|გთხოვ|ახლა|აბა|კაი|ოკეი|ოქეი)\b[,!\s]*)*")

# Trailing chatter: "for me", "please", "ჩემთვის", "რა". Georgian sentences
# also trail a copula — "…დესკტოპზე არის" ("…it's on the desktop") — which is
# him describing where it lives, not part of the name.
_TAIL_RE = re.compile(
    r"\s*(?:for\s+me|please|thanks|thank\s+you|now|ok(?:ay)?|"
    r"ჩემთვის|გთხოვ|რა|ხომ|არის|არი|იყო|მაქვს)\s*[.!?]*$", re.IGNORECASE)
# Leading filler INSIDE the target ("open now the file called…", "გახსენი
# ახლა ფაილი…"). _LEAD only strips what comes before the verb; Georgian word
# order regularly parks the same words after it.
_TLEAD_RE = re.compile(
    r"^\s*(?:now|just|the|a|an|my|that|this|ახლა|მერე|ერთი|ეს|ის)\b[\s,]*",
    re.IGNORECASE)

# Location hints. They tell us WHERE to look and are stripped from the name.
_WHERE = [
    (re.compile(r"\b(?:on|from|in)\s+(?:my\s+|the\s+)?desktop\b|დესკტოპ\w*|"
                r"სამუშაო\s+მაგიდა\w*", re.IGNORECASE), "desktop"),
    (re.compile(r"\b(?:in|from)\s+(?:my\s+)?downloads?\b|ჩამოტვირთ\w*|"
                r"დაუნლოუდ\w*", re.IGNORECASE), "downloads"),
    (re.compile(r"\b(?:in|from)\s+(?:my\s+)?documents?\b|დოკუმენტ\w*",
                re.IGNORECASE), "documents"),
]
# "the file called X" / "a folder named X" / "ფაილი სახელად X" — the noun is
# scaffolding around the real name, and it also tells us file vs folder.
# NOT anchored: his real sentence was "გახსენი ახლა აპლიკაცია, უფრო სწორად
# ფაილი, სახელად GG, დესკტოპზე არის" — the noun that counts is the LAST one,
# after he corrects himself mid-sentence, and everything before it is noise.
_NOUN_RE = re.compile(
    r"(?:the\s+|a\s+|an\s+|my\s+)?"
    r"(?P<kind>files?|folders?|director(?:y|ies)|documents?|apps?|"
    r"applications?|programs?|"
    r"ფაილ\w*|საქაღალდ\w*|აპლიკაცი\w*|პროგრამ\w*|დოკუმენტ\w*)"
    # A comma can sit on EITHER side of "called" — "ფაილი, სახელად GG" is how
    # he actually says it, and the name is whatever follows all of that.
    r"\s*[:,]?\s*(?:called|named|სახელად|სახელით)?\s*[:,]?\s*",
    re.IGNORECASE)
_NOUN_TAIL_RE = re.compile(
    r"\s*(?:,?\s*(?:which\s+is|it\s+is|it's|is)\s*)?$", re.IGNORECASE)

_URLISH_RE = re.compile(r"^[\w\-]+(?:\.[\w\-]+)+(?:/\S*)?$")

# Targets that mean WORK even behind an opening verb: "start the server",
# "open a pull request". The disk index would happily match a folder called
# `tests` or `build` and double-click it, which is not remotely what he asked
# for — so these hand the turn back to the brains.
_WORKISH_RE = re.compile(
    r"^(?:the\s+|a\s+|an\s+|my\s+)?"
    r"(?:tests?|test\s+suite|suite|builds?|servers?|dev\s+server|deploys?|"
    r"deployments?|pipelines?|ci|lint(?:er)?|typecheck|benchmarks?|"
    r"migrations?|scripts?|repos?|repositor(?:y|ies)|branch(?:es)?|"
    r"pull\s+requests?|prs?|commits?|issues?|terminals?\s+and\s+\w+|"
    r"ტესტ\w*|ბილდ\w*|სერვერ\w*|დეპლოი\w*|რეპო\w*|ბრენჩ\w*)\b",
    re.IGNORECASE)


class Reflex:
    """One matched command: what to run, and what GOAT says while it runs."""

    __slots__ = ("kind", "detail", "run", "speak")

    def __init__(self, kind, detail, run, speak=""):
        self.kind = kind        # open_url | open_app | open_path | volume | …
        self.detail = detail    # precise line for the screen
        self.run = run          # callable -> result string, no args
        self.speak = speak      # optional spoken override (else a cached ack)

    def __repr__(self):
        return f"<Reflex {self.kind}: {self.detail}>"


# ---- answers GOAT can give without anyone's help --------------------------
def _clock(lang: str) -> str:
    now = datetime.datetime.now()
    if lang == "ka":
        return f"{now.hour}:{now.minute:02d}-ია."
    h = now.hour % 12 or 12
    return f"It's {h}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}."


def _today(lang: str) -> str:
    now = datetime.datetime.now()
    if lang == "ka":
        return now.strftime("%d.%m.%Y") + "-ია."
    return "It's " + now.strftime("%A, %B %d") + "."


def _battery(lang: str) -> str:
    class _S(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong)]
    s = _S()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return "I can't read the battery." if lang != "ka" else "ბატარეას ვერ ვკითხულობ."
    pct = s.BatteryLifePercent
    plugged = s.ACLineStatus == 1
    if lang == "ka":
        return f"ბატარეა {pct}%-ია" + (", იტენება." if plugged else ".")
    return f"Battery is at {pct} percent" + (", plugged in." if plugged else ".")


# ---- window actions (Win32 only — no screenshots, no vision) --------------
_SW = {"minimize": 6, "maximize": 3, "restore": 9}


def _window(action: str, target: str = "") -> str:
    """Act on the front window, or on one he named by title.

    Closing is WM_CLOSE, never Alt+F4. Live on 2026-09-14 the Alt+F4 route
    closed the window, landed on the desktop, and popped the Windows shutdown
    dialog — one keystroke away from powering his machine off mid-sentence.
    """
    u = ctypes.windll.user32
    hwnd = 0
    if target:
        try:
            import screen_hands
            hwnd = screen_hands._match(target)["hwnd"]
            screen_hands.focus_window(str(hwnd))
        except Exception:  # noqa: BLE001 — unknown title: use what's in front
            hwnd = 0
    if not hwnd:
        hwnd = u.GetForegroundWindow()
    if not hwnd:
        return "no window in front"
    what = f"the {target} window" if target else "the front window"
    if action == "close":
        u.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
        return f"closed {what}"
    u.ShowWindow(hwnd, _SW[action])
    return f"{action}d {what}"


def _screenshot() -> str:
    # Win+Shift+S = Snipping Tool's region capture, which is what he means.
    for vk, up in ((0x5B, 0), (0x10, 0), (0x53, 0),
                   (0x53, 2), (0x10, 2), (0x5B, 2)):
        ctypes.windll.user32.keybd_event(vk, 0, up, 0)
    return "snipping tool open"


# ---- target resolution ----------------------------------------------------
def _clean_target(raw: str) -> tuple:
    """(name, where, want_dir) out of a spoken target phrase.

    Peels the sentence in the order the words actually nest: trailing chatter,
    then the place he named, then leading filler, then the noun scaffolding —
    and repeats the tail/lead trim afterwards, because removing the middle
    exposes new edges ("ფაილი, სახელად GG, დესკტოპზე არის" ends at "GG," only
    once "დესკტოპზე არის" is gone).
    """
    t = (raw or "").strip()
    where = ""
    for rx, place in _WHERE:
        if rx.search(t):
            where = place
            t = rx.sub(" ", t)
    want_dir = None
    for _ in range(3):      # his corrections stack: "app, or rather, file, …"
        t = _TAIL_RE.sub("", t.strip()).strip(" .,!?:;\"'")
        t = _TLEAD_RE.sub("", t)
        m = _NOUN_RE.search(t)
        if not m:
            break
        kind = m.group("kind").lower()
        if kind.startswith(("folder", "director", "საქაღალდ")):
            want_dir = True
        elif kind.startswith(("file", "document", "ფაილ", "დოკუმენტ")):
            want_dir = False
        else:
            want_dir = None     # "app" says nothing about file vs folder
        # Keep only what FOLLOWS the noun — that is where the name is. If
        # nothing follows, he named the thing before it ("the GG folder").
        after = t[m.end():].strip(" .,!?:;\"'")
        t = after if after else t[:m.start()]
    t = _TAIL_RE.sub("", t.strip())
    t = _NOUN_TAIL_RE.sub("", t)
    t = _TLEAD_RE.sub("", t)
    return " ".join(t.split()).strip(" .,!?:;\"'"), where, want_dir


def resolve_target(raw: str, lang: str = "en") -> Reflex | None:
    """Turn "the file called GG on my desktop" into something to double-click.

    Order matters: a known site beats a same-named file (he means google.com,
    not google.txt), an explicit URL beats everything, and the disk index is
    the last resort before giving up. Giving up is a FEATURE — an unresolved
    name goes to the talking brain instead of opening something random.
    """
    name, where, want_dir = _clean_target(raw)
    if not name or _WORKISH_RE.match(name) or _WORKISH_RE.match(raw.strip()):
        return None
    low = name.lower()

    # 1. Explicit URL or bare domain.
    if low.startswith(("http://", "https://")) or _URLISH_RE.match(low):
        url = local_hands.resolve_url(low)
        if url:
            return Reflex("open_url", url,
                          lambda u=url: local_hands.execute("open_url", {"url": u}))

    # 2. A site he names constantly. "google" with no file hint is the web.
    key = re.sub(r"\s+", "", low)
    site = SITES.get(low) or SITES.get(key)
    if site and want_dir is None:
        return Reflex("open_url", site,
                      lambda u=site: local_hands.execute("open_url", {"url": u}))

    # 3. A whitelisted app name (notepad, spotify, vscode…).
    if local_hands.resolve_app(low) is not None or low == "browser":
        return Reflex("open_app", name,
                      lambda a=low: local_hands.execute("open_app", {"app": a}))

    # 4. The disk: his files, folders, and every installed app's shortcut.
    #    lookup_live re-scans Desktop/Downloads on a miss, so something he
    #    saved or downloaded since GOAT booted still opens.
    hit = reflex_index.lookup_live(name, prefer=where, want_dir=want_dir)
    if hit:
        display, path, is_dir, source = hit
        kind = "folder" if is_dir else ("app" if source == "app" else "file")
        return Reflex("open_path", f"{display} — {kind}",
                      lambda p=path: _open_path(p))

    # 5. Still a site name we know, even though he said "file" — better than
    #    handing a plain word to a model that will take ten seconds to guess.
    if site:
        return Reflex("open_url", site,
                      lambda u=site: local_hands.execute("open_url", {"url": u}))
    return None


def _open_path(path: str) -> str:
    try:
        os.startfile(path)  # noqa: S606 — the whole point: shell-open as he would
        return f"opened {path}"
    except OSError:
        # No file association (a .dat, a folder Windows argues about) — show
        # it in Explorer instead of failing at him.
        try:
            subprocess.Popen(["explorer", path])
            return f"opened {path} in explorer"
        except OSError as e:
            return f"ERROR: {e}"


# ---- the matcher ----------------------------------------------------------
_OPEN_RE = re.compile(_LEAD + _OPEN + r"\s+(?P<t>.+)$", re.IGNORECASE)
_OPEN_KA_RE = re.compile(_LEAD + _OPEN_KA + r"\s+(?P<t>.+)$", re.IGNORECASE)
# Georgian puts the verb last as often as first: "გუგლი გახსენი".
_OPEN_KA_END_RE = re.compile(_LEAD + r"(?P<t>.+?)\s+" + _OPEN_KA + r"\s*[.!?]*$",
                             re.IGNORECASE)

_VOL_UP_RE = re.compile(
    r"^" + _LEAD + r"(?:turn\s+(?:the\s+)?(?:volume|sound|it)\s+up|"
    r"volume\s+up|louder|turn\s+it\s+up|crank\s+it(?:\s+up)?|"
    r"ხმა\s+(?:აუწი\w*|მოუმატ\w*|ადიდ\w*)|(?:აუწი\w*|მოუმატ\w*)\s+ხმა\w*)"
    r"\s*[.!?]*$", re.IGNORECASE)
_VOL_DOWN_RE = re.compile(
    r"^" + _LEAD + r"(?:turn\s+(?:the\s+)?(?:volume|sound|it)\s+down|"
    r"volume\s+down|quieter|lower\s+the\s+volume|turn\s+it\s+down|"
    r"ხმა\s+(?:დაუწი\w*|დააკელ\w*|ჩაწი\w*|დაბლა?\w*)|"
    r"(?:დაუწი\w*|დააკელ\w*)\s+ხმა\w*)\s*[.!?]*$", re.IGNORECASE)
_MUTE_RE = re.compile(
    r"^" + _LEAD + r"(?:mute|unmute|silence|shut\s+up\s+the\s+sound|"
    r"დადუმ\w*|ხმა\s+გათიშ\w*|გაჩუმ\w*|ხმა\s+ჩაკეტ\w*)\s*[.!?]*$",
    re.IGNORECASE)
_VOL_SET_RE = re.compile(
    r"^" + _LEAD + r"(?:set\s+)?(?:the\s+)?(?:volume|sound|ხმა\w*)\s*"
    r"(?:to|at|=|-?ზე)?\s*(?P<p>\d{1,3})\s*%?\s*[.!?]*$", re.IGNORECASE)

_PLAY_RE = re.compile(
    r"^" + _LEAD + r"(?:play|pause|resume|play\s+it|pause\s+it|"
    r"(?:გა)?აჩერ\w*|დაპაუზ\w*|ჩართე\s+მუსიკ\w*|დაუკრ\w*|გააგრძელ\w*)"
    r"\s*[.!?]*$", re.IGNORECASE)
_NEXT_RE = re.compile(
    r"^" + _LEAD + r"(?:next(?:\s+(?:track|song))?|skip(?:\s+(?:this|it))?|"
    r"შემდეგ\w*|გადართ\w*)\s*[.!?]*$", re.IGNORECASE)
_PREV_RE = re.compile(
    r"^" + _LEAD + r"(?:previous(?:\s+(?:track|song))?|go\s+back\s+a\s+song|"
    r"წინა\w*|უკან\s+დააბრუნ\w*)\s*[.!?]*$", re.IGNORECASE)

_BRIGHT_SET_RE = re.compile(
    r"^" + _LEAD + r"(?:set\s+)?(?:the\s+)?(?:brightness|screen|"
    r"სიკაშკაშ\w*|ეკრან\w*)\s*(?:to|at|=|-?ზე)?\s*(?P<p>\d{1,3})\s*%?"
    r"\s*[.!?]*$", re.IGNORECASE)
_BRIGHTER_RE = re.compile(
    r"^" + _LEAD + r"(?:brighter|turn\s+(?:the\s+)?brightness\s+up|"
    r"გაანათ\w*|გაანათლ\w*|სიკაშკაშე?\s+(?:აუწი\w*|მოუმატ\w*))\s*[.!?]*$",
    re.IGNORECASE)
_DIMMER_RE = re.compile(
    r"^" + _LEAD + r"(?:dimmer|dim\s+the\s+screen|turn\s+(?:the\s+)?"
    r"brightness\s+down|დააბნელ\w*|სიკაშკაშე?\s+დაუწი\w*)\s*[.!?]*$",
    re.IGNORECASE)

_LOCK_RE = re.compile(
    r"^" + _LEAD + r"(?:lock(?:\s+(?:the\s+)?(?:screen|pc|computer|it))?|"
    # Georgian puts the object after the verb: "დაბლოკე ეკრანი".
    r"(?:დაბლოკ\w*|ჩაკეტ\w*)(?:\s+(?:ეკრან\w*|კომპ\w*))?)"
    r"\s*[.!?]*$", re.IGNORECASE)
_SHOT_RE = re.compile(
    r"^" + _LEAD + r"(?:take\s+a\s+)?(?:screenshot|screen\s+shot|snip|"
    r"სქრინ\w*|ეკრანის\s+სურათ\w*)\s*[.!?]*$", re.IGNORECASE)

# Window state, with an OPTIONAL named target: "maximize the Google window",
# "გაადიდე გუგლის ფანჯარა ბოლომდე" (a real one from his 2026-09-15 session,
# STT-garbled to "გაადგიდე" — hence the tolerant stems).
_WIN_RE = re.compile(
    r"^" + _LEAD + r"(?P<a>maximi[sz]e|full\s*screen|minimi[sz]e|restore|"
    r"close(?:\s+(?:this|it|the\s+window))?|"
    r"გაად?[გდ]ი[დთ]\w*|გაზარდ\w*|დაპატარავ\w*|ჩააგდ\w*|დახურ\w*|დახუე?ვ\w*)"
    r"(?:\s+(?P<t>.+?))??"
    r"(?:\s*(?:this|the|it)?\s*(?:window|ფანჯარ\w*))?"
    r"(?:\s+(?:all\s+the\s+way|fully|ბოლომდე|მთლიანად))?\s*[.!?]*$",
    re.IGNORECASE)
# Words that are never a window name — they are the sentence, not the target.
_NOT_A_TARGET = re.compile(
    r"^(?:this|that|it|the|my|current|active|"
    r"ეს|ის|ამ|მიმდინარე)$", re.IGNORECASE)

_TIME_RE = re.compile(
    r"^" + _LEAD + r"(?:what(?:'s|\s+is)\s+the\s+time|what\s+time\s+is\s+it|"
    r"time\s+now|რომელი\s+საათია|რა\s+საათია|რა\s+დროა)\s*[.!?]*$",
    re.IGNORECASE)
_DATE_RE = re.compile(
    r"^" + _LEAD + r"(?:what(?:'s|\s+is)\s+(?:the\s+)?(?:date|day)"
    r"(?:\s+today)?|what\s+day\s+is\s+it|today'?s\s+date|"
    r"რა\s+რიცხვია|რა\s+დღეა|დღეს\s+რა\s+რიცხვია)\s*[.!?]*$", re.IGNORECASE)
_BATT_RE = re.compile(
    r"^" + _LEAD + r"(?:(?:what(?:'s|\s+is)\s+(?:my\s+|the\s+)?)?battery"
    r"(?:\s+(?:level|percent(?:age)?|status))?|how\s+much\s+battery|"
    r"ბატარე\w*|დამუხტ\w*)\s*[.!?]*$", re.IGNORECASE)

# A question is never a reflex. "how do I open a file?" must reach a brain.
_QUESTION_RE = re.compile(
    r"^\s*(?:hey\s+|ok(?:ay)?\s+|goat[,!\s]+)*"
    r"(?:what|why|how|when|where|which|who|should|do\s+you|can\s+you\s+tell|"
    r"რატომ|როგორ|როდის|სად|რომელ\w*|ვინ)\b", re.IGNORECASE)


def _vol(action: str, steps: int = 5):
    return lambda: local_hands.execute(
        "volume", {"action": action, "steps": steps})


def _press_vol(action: str, presses: int) -> None:
    """local_hands caps a single call at 25 presses (~50%). Anything that has
    to cross the whole range needs several calls."""
    while presses > 0:
        n = min(25, presses)
        local_hands.execute("volume", {"action": action, "steps": n})
        presses -= n


def _set_volume(pct: int) -> str:
    """Absolute volume, from key presses only. There is no set-volume API in
    local_hands, so the range is crossed the only way the keyboard allows:
    floor it, then climb. 50 down-presses (~2% each) guarantees 0 from any
    starting point — a single capped call of 25 only moves ~50%, which is why
    "set volume to 40" used to land at 90 from a full volume."""
    _press_vol("down", 50)
    _press_vol("up", round(pct / 2))
    return f"volume set to ~{pct}%"


def _read_brightness() -> int | None:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance -Namespace root/WMI "
             "-ClassName WmiMonitorBrightness).CurrentBrightness"],
            capture_output=True, timeout=6, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return int(r.stdout.strip().splitlines()[0])
    except Exception:  # noqa: BLE001 — desktops and some panels have no WMI
        return None    # brightness control at all


def _brightness_step(delta: int) -> str:
    """"Brighter" has to mean brighter THAN NOW, not "jump to full" — so read
    the panel first and step from there. Falls back to a fixed level when the
    monitor exposes no brightness class."""
    cur = _read_brightness()
    if cur is None:
        return local_hands.execute(
            "brightness", {"percent": 100 if delta > 0 else 30})
    return local_hands.execute(
        "brightness", {"percent": max(0, min(100, cur + delta))})


def _media(action: str):
    return lambda: local_hands.execute("media", {"action": action})


def _bright(pct: int):
    return lambda: local_hands.execute("brightness", {"percent": pct})


def match(text: str, lang: str = "en") -> Reflex | None:
    """The whole router. Returns a Reflex to run right now, or None to let the
    normal lanes have the turn. Pure and fast — safe to call on every input."""
    if not ENABLED:
        return None
    t = " ".join((text or "").split())
    if not t or len(t) > 220:
        return None          # a paragraph is never a device command
    # "open google AND tell me the news" is a conversation, not a reflex.
    if re.search(r"\b(?:and\s+then|then\s+tell|and\s+tell|and\s+explain|"
                 r"შემდეგ\s+კი|და\s+მიპასუხ)\b", t, re.IGNORECASE):
        return None

    if _TIME_RE.match(t):
        return Reflex("answer", "time", lambda: "", speak=_clock(lang))
    if _DATE_RE.match(t):
        return Reflex("answer", "date", lambda: "", speak=_today(lang))
    if _BATT_RE.match(t):
        line = _battery(lang)
        return Reflex("answer", "battery", lambda: "", speak=line)

    if _QUESTION_RE.match(t):
        return None

    if _MUTE_RE.match(t):
        return Reflex("volume", "mute", _vol("mute", 1))
    if _VOL_UP_RE.match(t):
        return Reflex("volume", "up", _vol("up"))
    if _VOL_DOWN_RE.match(t):
        return Reflex("volume", "down", _vol("down"))
    m = _VOL_SET_RE.match(t)
    if m:
        pct = max(0, min(100, int(m.group("p"))))
        return Reflex("volume", f"{pct}%", lambda p=pct: _set_volume(p))

    if _NEXT_RE.match(t):
        return Reflex("media", "next", _media("next"))
    if _PREV_RE.match(t):
        return Reflex("media", "previous", _media("prev"))
    if _PLAY_RE.match(t):
        return Reflex("media", "play/pause", _media("play_pause"))

    m = _BRIGHT_SET_RE.match(t)
    if m:
        pct = max(0, min(100, int(m.group("p"))))
        return Reflex("brightness", f"{pct}%", _bright(pct))
    if _BRIGHTER_RE.match(t):
        return Reflex("brightness", "up", lambda: _brightness_step(+20))
    if _DIMMER_RE.match(t):
        return Reflex("brightness", "down", lambda: _brightness_step(-20))

    if _LOCK_RE.match(t):
        return Reflex("lock", "screen",
                      lambda: local_hands.execute("lock_screen", {}))
    if _SHOT_RE.match(t):
        return Reflex("screenshot", "region capture", _screenshot)

    m = _WIN_RE.match(t)
    if m:
        a = m.group("a").lower()
        if a.startswith(("maximi", "full", "გაზარდ")) or a.startswith("გაად"):
            act = "maximize"
        elif a.startswith(("minimi", "დაპატარავ", "ჩააგდ")):
            act = "minimize"
        elif a.startswith(("close", "დახურ", "დახუე")):
            act = "close"
        else:
            act = "restore"
        target = (m.group("t") or "").strip(" .,!?:;\"'")
        # Georgian marks the possessor: "გუგლის ფანჯარა" = "Google's window".
        target = re.sub(r"(\w)ის$", r"\1", target).strip()
        if target and not _NOT_A_TARGET.match(target):
            return Reflex("window", f"{act} {target}",
                          lambda x=act, n=target: _window(x, n))
        return Reflex("window", act, lambda x=act: _window(x))

    for rx in (_OPEN_RE, _OPEN_KA_RE, _OPEN_KA_END_RE):
        m = rx.match(t)
        if m:
            got = resolve_target(m.group("t"), lang)
            if got:
                return got
            break   # it WAS an open order, we just don't know the thing —
                    # let a brain figure it out rather than trying the next
                    # pattern on the same words.
    return None


def warm():
    """Called once at boot: build the disk index behind the UI."""
    reflex_index.refresh(background=True)
