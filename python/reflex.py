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
    r"applications?|programs?|pictures?|photos?|images?|pics?|videos?|"
    r"ფაილ\w*|საქაღალდ\w*|აპლიკაცი\w*|პროგრამ\w*|დოკუმენტ\w*|სურათ\w*|"
    r"ფოტო\w*|ვიდეო\w*)"
    # A comma can sit on EITHER side of "called" — "ფაილი, სახელად GG" is how
    # he actually says it, and the name is whatever follows all of that.
    r"\s*[:,]?\s*(?:called|named|სახელად|სახელით)?\s*[:,]?\s*",
    re.IGNORECASE)
_NOUN_TAIL_RE = re.compile(
    r"\s*(?:,?\s*(?:which\s+is|it\s+is|it's|is)\s*)?$", re.IGNORECASE)

_URLISH_RE = re.compile(r"^[\w\-]+(?:\.[\w\-]+)+(?:/\S*)?$")

# Spoken sentences run on past the name: "open the PFP picture? For me. That
# I have on my desktop." (2026-09-18 — fell through, and the brain then said
# no such picture existed). The name lives in the first sentence; a relative
# clause about where he keeps it is description, not name.
_SENTENCE_END_RE = re.compile(r"[.?!]+\s+")
_KEEP_CLAUSE_RE = re.compile(
    r"\s*,?\s*\b(?:that|which)\s+i\s+(?:have|had|got|saved|put|keep|"
    r"downloaded)\b.*$", re.IGNORECASE)

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
        # A NAMED window that isn't there is a failure, never "use the front
        # one": "დახურე სტიმი" found no window titled სტიმი, closed whatever
        # was in front (his terminal, or GOAT itself), then said it closed
        # Steam. Seen 2026-09-18.
        try:
            import screen_hands
            hwnd = screen_hands._match(target)["hwnd"]
            screen_hands.focus_window(str(hwnd))
        except Exception as e:  # noqa: BLE001
            return f"ERROR: no window matching {target!r} ({e})"
    if not hwnd:
        # "close this" means what he is looking at — the topmost window that
        # is NOT GOAT. The old GetForegroundWindow() was GOAT itself whenever
        # he had just been talking to it, and WM_CLOSE would have quit GOAT.
        try:
            import fast_hands
            w = fast_hands.target_window()
        except Exception:  # noqa: BLE001
            w = None
        if w is None:
            return "ERROR: no window in front"
        hwnd = w["hwnd"]
        target = w.get("app") or w["title"]
    what = f"the {target} window" if target else "the front window"
    if action == "close":
        u.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
        return f"closed {what}"
    u.ShowWindow(hwnd, _SW[action])
    return f"{action}d {what}"


def _close_question(target: str, lang: str) -> "Reflex | None":
    """A close aimed at a terminal, a Claude session or unsaved work asks
    first (2026-10-01: "close this window" closed his live Claude Code
    terminal). The window is pinned by handle NOW, so his "yes" closes the
    one he was asked about, not whatever is in front by then."""
    try:
        import close_guard
        if target:
            import screen_hands
            w = screen_hands._match(target)
        else:
            import fast_hands
            w = fast_hands.target_window()
        if not w:
            return None
        reason = close_guard.window_reason(w["hwnd"], w.get("title", ""))
    except Exception:  # noqa: BLE001 — unsure means ask the brain, not close
        return None
    if not reason:
        return None
    hwnd = w["hwnd"]

    def close_it(h=hwnd, why=reason):
        ctypes.windll.user32.PostMessageW(h, 0x0010, 0, 0)   # WM_CLOSE
        return f"closed {why.split(' — ')[0]}"

    def hold(why=reason):
        close_guard.ask(why, run=close_it)
        return "asked first"

    q = (f"ეს არის {reason}. მაინც დავხურო?" if lang == "ka"
         else f"That's {reason}. Close it anyway?")
    return Reflex("confirm", f"close? {reason}", hold, speak=q)


def _has_window(target: str) -> bool:
    try:
        import screen_hands
        screen_hands._match(target)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- quitting apps (process level, verified) -------------------------------
# "Turn off the Ubisoft and the Roblox and the Steam" (2026-09-18) went to a
# brain, which killed two, missed Ubisoft (its process is `upc`, not
# "ubisoft"), and said all three were closed. Launchers live in the tray with
# no window to WM_CLOSE, so this works on processes — and checks afterwards
# that they are really gone before anything is reported.
# Name he says -> process names (psutil .name() without .exe, lowercased).
QUIT_APPS = {
    "steam": ("steam", "steamwebhelper", "steamservice"),
    "ubisoft": ("upc", "uplaywebcore", "ubisoftconnect",
                "ubisoftgamelauncher", "ubisoftgamelauncher64"),
    "roblox": ("robloxplayerbeta", "robloxstudiobeta"),
    "discord": ("discord",),
    "spotify": ("spotify",),
    "chrome": ("chrome",),
    "brave": ("brave",),
    "opera": ("opera",),
    "edge": ("msedge",),
    "telegram": ("telegram",),
    "whatsapp": ("whatsapp",),
    "epic": ("epicgameslauncher",),
    "riot": ("riotclientservices", "riotclientux", "riotclientuxrender"),
    "valorant": ("valorant-win64-shipping", "valorant"),
    "ollama": ("ollama", "ollama app"),
    "zoom": ("zoom",),
    "obs": ("obs64",),
    "teams": ("ms-teams", "teams"),
}
# Every way he (or the STT) names them -> QUIT_APPS key. Georgian by stem,
# because the case ending moves: სტიმი / სტიმს / სტიმიც.
_QUIT_ALIASES = {
    "steam": "steam", "uplay": "ubisoft", "ubisoft connect": "ubisoft",
    "ubisoft": "ubisoft", "roblox": "roblox", "discord": "discord",
    "spotify": "spotify", "chrome": "chrome", "google chrome": "chrome",
    "brave": "brave", "opera": "opera", "opera gx": "opera",
    "edge": "edge", "microsoft edge": "edge", "telegram": "telegram",
    "whatsapp": "whatsapp", "epic": "epic", "epic games": "epic",
    "riot": "riot", "riot client": "riot", "valorant": "valorant",
    "ollama": "ollama", "zoom": "zoom", "obs": "obs", "teams": "teams",
}
_QUIT_KA_STEMS = (
    ("სტიმ", "steam"), ("იუბისოფ", "ubisoft"), ("უბისოფ", "ubisoft"),
    ("იუბისოფტ", "ubisoft"), ("რობლოქს", "roblox"), ("რობლოკს", "roblox"),
    ("დისქორდ", "discord"), ("დისკორდ", "discord"), ("სპოტიფა", "spotify"),
    # The ear moves the ს across the word gap: "დახურე სტიმი" is heard as
    # "დახურეს ტიმი" (measured 2026-09-23), and he says "ტიმი" for Steam.
    ("ტიმ", "steam"),
    ("ქრომ", "chrome"), ("ბრეივ", "brave"), ("ოპერ", "opera"),
    ("ტელეგრამ", "telegram"), ("ვოცაპ", "whatsapp"), ("ვაცაპ", "whatsapp"),
    ("ვალორანტ", "valorant"), ("ოლამა", "ollama"), ("ზუმ", "zoom"),
)
_QUIT_NOISE_RE = re.compile(
    r"\b(?:the|my|app|application|program|launcher|client|game|games|too|"
    r"also|as\s+well|for\s+me|please|now|process(?:es)?|აპლიკაცი\w*|"
    r"პროგრამ\w*|თამაშ\w*|ჩემთვის|გთხოვ|ახლა|ასევე|კიდევ)\b",
    re.IGNORECASE)
_QUIT_SPLIT_RE = re.compile(r"\s*(?:,|&|\band\b|\bდა\b|\bplus\b)\s*",
                            re.IGNORECASE)


def _quit_key(word: str) -> str | None:
    w = " ".join(_QUIT_NOISE_RE.sub(" ", word).split()).strip(" .,!?:;\"'")
    if not w:
        return None
    low = w.lower()
    if low in _QUIT_ALIASES:
        return _QUIT_ALIASES[low]
    for stem, key in _QUIT_KA_STEMS:
        if low.startswith(stem):
            return key
    return None


def _quit_targets(raw: str) -> list | None:
    """Every named thing must be a known app, or None (a brain decides)."""
    parts = [p for p in _QUIT_SPLIT_RE.split(raw or "") if p.strip(" .,!?")]
    keys = []
    for p in parts:
        k = _quit_key(p)
        if k is None:
            # "the Ubisoft and the Roblox" leaves "the" pieces — noise-only
            # parts are fine, a real unknown word is not.
            if _QUIT_NOISE_RE.sub("", p).strip(" .,!?:;\"'"):
                return None
            continue
        if k not in keys:
            keys.append(k)
    return keys or None


def _quit_apps(keys: list, grace: float = 2.0) -> str:
    """Ask each app to close, force the stragglers, then CHECK. The result
    names exactly what is still running, so the voice can't say "closed"
    about something that isn't."""
    import time
    import psutil
    want = {n for k in keys for n in QUIT_APPS[k]}

    def alive():
        out = []
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "").lower().removesuffix(".exe")
            if n in want:
                out.append(p)
        return out

    procs = alive()
    running = {k for k in keys
               if any((p.info.get("name") or "").lower().removesuffix(".exe")
                      in QUIT_APPS[k] for p in procs)}
    for p in procs:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _gone, left = psutil.wait_procs(procs, timeout=grace)
    for p in left:
        try:
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(left, timeout=1.5)
    still = sorted({k for k in keys
                    for p in alive()
                    if (p.info.get("name") or "").lower().removesuffix(".exe")
                    in QUIT_APPS[k]})
    if still:
        return "ERROR: still running: " + ", ".join(still)
    not_running = [k for k in keys if k not in running]
    msg = "closed " + (", ".join(sorted(running)) if running else "nothing")
    if not_running:
        msg += " (was not running: " + ", ".join(not_running) + ")"
    return msg


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
    # The ear writes a Latin name with a Georgian case ending: "Google-ი".
    t = re.sub(r"(\w)-[ა-ჿ]+", r"\1", t)
    t = _KEEP_CLAUSE_RE.sub("", t)
    first = next((s for s in _SENTENCE_END_RE.split(t) if s.strip(" .,!?")),
                 t)
    t = first
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
        elif kind.startswith(("file", "document", "ფაილ", "დოკუმენტ", "pic",
                              "photo", "image", "video", "სურათ", "ფოტო",
                              "ვიდეო")):
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
    r"დადუმ\w*|ხმა\s+გათიშ\w*|გაჩუმ\w*|ხმა\s+ჩაკეტ\w*|ხმა\s+გამორთ\w*|"
    r"(?:გათიშ|გამორთ)\w*\s+ხმა\w*)\s*[.!?]*$",
    re.IGNORECASE)

# Quit an app: "close steam", "turn off the Ubisoft and the Roblox",
# "დახურე სტიმი", "სტიმი გათიშე". Only fires when EVERY target is in
# QUIT_APPS; anything else falls to the window rule or a brain.
_QUIT_VERB = (r"(?:close|quit|exit|kill|turn\s+off|shut\s+off|switch\s+off|"
              r"end|stop)")
_QUIT_VERB_KA = r"(?:დახურ\w*|დახუე?ვ\w*|გათიშ\w*|გამორთ\w*|მოკალ\w*|გააჩერ\w*)"
_QUIT_RE = re.compile(r"^" + _LEAD + _QUIT_VERB + r"\s+(?P<t>.+?)\s*[.!?]*$",
                      re.IGNORECASE)
_QUIT_KA_RE = re.compile(r"^" + _LEAD + _QUIT_VERB_KA + r"\s+(?P<t>.+?)\s*[.!?]*$",
                         re.IGNORECASE)
_QUIT_KA_END_RE = re.compile(r"^" + _LEAD + r"(?P<t>.+?)\s+" + _QUIT_VERB_KA
                             + r"\s*[.!?]*$", re.IGNORECASE)
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
    r"(?:\s+(?:all\s+the\s+way|fully|ბოლომდე|მთლიანად))?"
    # "Okay, close it now." (live 2026-10-01) fell through to the brain for
    # 3.8s because "now" was read as a window name.
    r"(?:\s+(?:now|please|for\s+me|ახლა|გთხოვ))*\s*[.!?]*$",
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

# ---- fast hands: tabs, keys, clicks (2026-10-01) ---------------------------
# His complaint: "goat takes about 10 seconds to do the simple tasks, close
# the tab, click this, click that". Those went to the brain — think, press
# ctrl+w, screenshot, press, screenshot — five model round trips around 42ms
# of keystrokes. Here they are one Win32 call aimed at the window he means
# (fast_hands.target_window: the topmost real window that is not GOAT).

_NUM = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2,
        "ერთი": 1, "ორი": 2, "სამი": 3, "ოთხი": 4, "ხუთი": 5, "ექვსი": 6,
        "შვიდი": 7, "რვა": 8, "ცხრა": 9, "ათი": 10}
_NUM_ALT = (r"(?:\d{1,2}|" + "|".join(sorted((k for k in _NUM if len(k) > 2),
                                             key=len, reverse=True)) + r")")
# What may trail a tab order without changing it: "…that I have open",
# "…in Brave", "…please".
_TAB_TAIL = (r"(?:\s+(?:that\s+|which\s+)?i(?:'ve|\s+have)?\s+(?:got\s+)?open"
             r"(?:ed)?)?(?:\s+(?:in|on)\s+(?:the\s+)?(?:browser|brave|chrome|"
             r"edge|firefox))?\s*(?:please|for\s+me|now)?\s*[.!?]*$")
_TAB_W = r"(?:tabs?|ტაბ\w*|ჩანართ\w*)"
_CLOSE_TAB_RE = re.compile(
    r"^" + _LEAD + r"(?:close|shut|kill|get\s+rid\s+of|remove|დახურ\w*|დახუე?ვ\w*)"
    r"\s+(?:(?:the|this|that|my|current|these|those|last|ეს|ბოლო)\s+)*"
    r"(?:(?P<n>" + _NUM_ALT + r"|all(?:\s+the)?|ყველა)\s+)?"
    r"(?:(?:open|current|last|of\s+the|of\s+my)\s+)*"
    + _TAB_W + _TAB_TAIL, re.IGNORECASE)
_CLOSE_TAB_KA_END_RE = re.compile(
    r"^" + _LEAD + r"(?:(?:ეს|ბოლო)\s+)?(?:(?P<n>" + _NUM_ALT + r"|ყველა)\s+)?"
    + _TAB_W + r"\s+(?:დახურ\w*|დახუე?ვ\w*)\s*[.!?]*$", re.IGNORECASE)
_NEW_TAB_RE = re.compile(
    r"^" + _LEAD + r"(?:(?:open|give\s+me)\s+)?(?:a\s+)?new\s+tab\s*[.!?]*$|"
    r"^" + _LEAD + r"ახალი\s+" + _TAB_W + r"(?:\s+" + _OPEN_KA + r")?\s*[.!?]*$",
    re.IGNORECASE)
_REOPEN_TAB_RE = re.compile(
    r"^" + _LEAD + r"(?:re-?open|bring\s+back|restore|undo\s+close)\s+"
    r"(?:the\s+)?(?:last\s+)?(?:closed\s+)?tab(?:\s+i\s+closed)?\s*[.!?]*$",
    re.IGNORECASE)
_NEXT_TAB_RE = re.compile(
    r"^" + _LEAD + r"(?:(?:go|switch|move)\s+to\s+(?:the\s+)?)?next\s+tab\s*[.!?]*$|"
    r"^" + _LEAD + r"შემდეგ\w*\s+" + _TAB_W + r"\s*[.!?]*$", re.IGNORECASE)
_PREV_TAB_RE = re.compile(
    r"^" + _LEAD + r"(?:(?:go|switch|move)\s+(?:back\s+)?to\s+(?:the\s+)?)?"
    r"(?:previous|prev|last)\s+tab\s*[.!?]*$|"
    r"^" + _LEAD + r"წინა\s+" + _TAB_W + r"\s*[.!?]*$", re.IGNORECASE)
_BACK_RE = re.compile(
    r"^" + _LEAD + r"(?:go\s+back(?:\s+a\s+page|\s+one\s+page)?|back\s+a\s+page|"
    r"previous\s+page|წინა\s+გვერდ\w*)\s*[.!?]*$", re.IGNORECASE)
_FWD_RE = re.compile(
    r"^" + _LEAD + r"(?:go\s+forward(?:\s+a\s+page)?|next\s+page|"
    r"შემდეგ\w*\s+გვერდ\w*)\s*[.!?]*$", re.IGNORECASE)
_REFRESH_RE = re.compile(
    r"^" + _LEAD + r"(?:refresh|reload)(?:\s+(?:the|this)\s+(?:page|tab)|"
    r"\s+it|\s+the\s+site)?\s*[.!?]*$|"
    r"^" + _LEAD + r"(?:გვერდი\s+)?(?:დაარეფრეშ\w*|გადატვირთ\w*)(?:\s+გვერდ\w*)?"
    r"\s*[.!?]*$", re.IGNORECASE)
_ZOOM_RE = re.compile(
    r"^" + _LEAD + r"zoom\s+(?P<d>in|out)\s*[.!?]*$", re.IGNORECASE)
_SCROLL_RE = re.compile(
    # No direction ("scroll this", "keep scrolling") means down — live on
    # 2026-10-01 "could you scroll this" went to the brain for 3.2s.
    r"^" + _LEAD + r"(?:(?:keep\s+)?scroll(?:ing)?(?:\s+(?P<d>down|up))?"
    r"(?:\s+(?:this|it|the\s+page|a\s+bit|a\s+little|more|some|please|"
    r"for\s+me|now))*|(?P<ka_d>ჩამო|ა)სქროლ\w*|(?P<ka2>ქვემოთ|ზემოთ)\s+(?:ჩამოდი|ადი|"
    r"ჩასქროლე|ასქროლე))\s*[.!?]*$", re.IGNORECASE)
_KEYNAMES = {"enter": "enter", "return": "enter", "escape": "esc", "esc": "esc",
             "space": "space", "spacebar": "space", "tab": "tab",
             "backspace": "backspace", "delete": "delete", "up": "up",
             "down": "down", "left": "left", "right": "right",
             "page down": "pagedown", "page up": "pageup", "home": "home",
             "end": "end", "f5": "f5", "f11": "f11", "f1": "f1"}
_PRESS_RE = re.compile(
    r"^" + _LEAD + r"(?:press|hit|push)\s+(?:the\s+)?(?P<k>"
    + "|".join(sorted((re.escape(k) for k in _KEYNAMES), key=len, reverse=True))
    + r")(?:\s+key)?(?:\s+(?P<n>" + _NUM_ALT + r")\s+times)?\s*[.!?]*$",
    re.IGNORECASE)
_EDIT_KEYS = [
    (re.compile(r"^" + _LEAD + r"(?:copy(?:\s+(?:it|that|this))?|დააკოპირ\w*)"
                r"\s*[.!?]*$", re.IGNORECASE), "ctrl+c", "copy"),
    (re.compile(r"^" + _LEAD + r"(?:paste(?:\s+(?:it|that|this))?|ჩასვ\w*|"
                r"დაპასტ\w*)\s*[.!?]*$", re.IGNORECASE), "ctrl+v", "paste"),
    (re.compile(r"^" + _LEAD + r"undo(?:\s+(?:it|that|this))?\s*[.!?]*$",
                re.IGNORECASE), "ctrl+z", "undo"),
    (re.compile(r"^" + _LEAD + r"redo\s*[.!?]*$", re.IGNORECASE), "ctrl+y", "redo"),
    (re.compile(r"^" + _LEAD + r"(?:select\s+all|მონიშნე\s+ყველა\w*)\s*[.!?]*$",
                re.IGNORECASE), "ctrl+a", "select all"),
    (re.compile(r"^" + _LEAD + r"(?:save(?:\s+(?:it|this|that|the\s+file))?|"
                r"შეინახ\w*|დაასეივ\w*)\s*[.!?]*$", re.IGNORECASE), "ctrl+s", "save"),
]
# "click Subscribe", "click on the Settings button", "დააჭირე Subscribe-ს",
# "Subscribe-ზე დააჭირე". Pointing words ("click this", "click here") need
# eyes, so they are not a reflex.
_CLICK_RE = re.compile(
    r"^" + _LEAD + r"(?:click|tap|press)\s+(?:on\s+)?(?:the\s+)?"
    r"(?P<t>.+?)(?:\s+(?:button|link|tab|icon|option|menu|item))?\s*[.!?]*$|"
    r"^" + _LEAD + r"(?:დააჭირე|დააკლიკე|დააწექი|დააკლიკ\w*)\s+(?P<t2>.+?)\s*[.!?]*$|"
    r"^" + _LEAD + r"(?P<t3>.+?)(?:-?ზე|-?ს)\s+(?:დააჭირე|დააკლიკე|დააწექი)\s*[.!?]*$",
    re.IGNORECASE)
_DEICTIC = re.compile(
    r"^(?:this|that|it|here|there|this\s+one|that\s+one|the\s+first\s+one|"
    r"on\s+it|me|ეს|ის|აქ|იქ|ამას|იმას|ამ\w*|იმ\w*)$", re.IGNORECASE)


def _count(word: str | None) -> int:
    if not word:
        return 1
    w = word.strip().lower()
    return int(w) if w.isdigit() else _NUM.get(w, 1)


def _fast(kind: str, *args, **kw):
    """Deferred import: fast_hands pulls in screen_hands (ctypes setup), and
    match() must stay importable and cheap in tests that never act."""
    def run():
        import fast_hands
        return getattr(fast_hands, kind)(*args, **kw)
    return run


def _fast_hands_rule(t: str) -> Reflex | None:
    for rx in (_CLOSE_TAB_RE, _CLOSE_TAB_KA_END_RE):
        m = rx.match(t)
        if m:
            n = (m.group("n") or "").lower()
            if n.startswith("all") or n == "ყველა":
                # All tabs = the browser window: ctrl+shift+w closes it.
                return Reflex("keys", "close all tabs",
                              _fast("keys", "ctrl+shift+w", browser=True,
                                    what="closed the window's tabs"))
            count = _count(n)
            plural = bool(re.search(r"(?:tabs|ტაბები|ჩანართები)\b", t, re.IGNORECASE))
            if plural and not m.group("n"):
                return None     # "close the tabs" — which ones? a brain asks.
            return Reflex("keys", f"close {count} tab{'s' if count > 1 else ''}",
                          _fast("keys", "ctrl+w", times=count, browser=True,
                                what="closed tab"))
    if _NEW_TAB_RE.match(t):
        return Reflex("keys", "new tab", _fast("keys", "ctrl+t", browser=True,
                                               what="new tab"))
    if _REOPEN_TAB_RE.match(t):
        return Reflex("keys", "reopen tab",
                      _fast("keys", "ctrl+shift+t", browser=True, what="reopened tab"))
    if _NEXT_TAB_RE.match(t):
        return Reflex("keys", "next tab", _fast("keys", "ctrl+tab", browser=True,
                                                what="next tab"))
    if _PREV_TAB_RE.match(t):
        return Reflex("keys", "previous tab",
                      _fast("keys", "ctrl+shift+tab", browser=True, what="previous tab"))
    if _BACK_RE.match(t):
        return Reflex("keys", "back", _fast("keys", "alt+left", browser=True,
                                            what="back"))
    if _FWD_RE.match(t):
        return Reflex("keys", "forward", _fast("keys", "alt+right", browser=True,
                                               what="forward"))
    if _REFRESH_RE.match(t):
        return Reflex("keys", "refresh", _fast("keys", "f5", what="refreshed"))
    m = _ZOOM_RE.match(t)
    if m:
        d = m.group("d").lower()
        return Reflex("keys", f"zoom {d}",
                      _fast("keys", "ctrl+equals" if d == "in" else "ctrl+minus",
                            what=f"zoom {d}"))
    m = _SCROLL_RE.match(t)
    if m:
        if m.group("d"):
            d = m.group("d").lower()
        elif m.group("ka_d") is not None:
            d = "down" if m.group("ka_d").startswith("ჩამო") else "up"
        elif m.group("ka2"):
            d = "down" if m.group("ka2").startswith("ქვე") else "up"
        else:
            d = "down"      # "scroll this" / "keep scrolling"
        return Reflex("keys", f"scroll {d}", _fast("scroll", d))
    m = _PRESS_RE.match(t)
    if m:
        key = _KEYNAMES[m.group("k").lower()]
        n = _count(m.group("n"))
        return Reflex("keys", f"press {key}" + (f" x{n}" if n > 1 else ""),
                      _fast("keys", key, times=n, what=f"pressed {key}"))
    for rx, combo, what in _EDIT_KEYS:
        if rx.match(t):
            return Reflex("keys", what, _fast("keys", combo, what=what))
    m = _CLICK_RE.match(t)
    if m:
        label = (m.group("t") or m.group("t2") or m.group("t3") or "").strip(" .,!?:;\"'")
        label = re.sub(r"(?:-?ზე|-?ს)$", "", label).strip()
        # "Okay, click stop. First image you see." (live 2026-10-01, the ear
        # mishearing "click the first image") searched for a button named
        # "stop. First image you see" for 491ms, then deferred anyway. A name
        # never spans a sentence break — that is a description, and
        # describing needs eyes.
        if label and not _DEICTIC.match(label) and len(label) <= 60 \
                and len(label.split()) <= 6 and not re.search(r"[.?!]\s", label):
            return Reflex("click", label, _fast("click_named", label))
    return None


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

    # The realtime ear punctuates speech — "გამორთე, ხმა." — and a comma must
    # not break a fixed command. The open patterns below keep the original,
    # where a comma can separate a noun from its name ("ფაილი, სახელად GG").
    full = t
    t = re.sub(r"\s*,\s*", " ", t)

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

    got = _fast_hands_rule(t)
    if got is not None:
        return got

    for rx in (_QUIT_RE, _QUIT_KA_RE, _QUIT_KA_END_RE):
        m = rx.match(t)
        if m:
            keys = _quit_targets(m.group("t"))
            if not keys and rx is _QUIT_KA_RE and t[m.start("t") - 2] == "ს":
                # Word gap misheard: "დახურეს ტიმი" = "დახურე სტიმი".
                keys = _quit_targets("ს" + m.group("t"))
            if keys:
                return Reflex("quit", ", ".join(keys),
                              lambda k=keys: _quit_apps(k))

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
        # "close the Zebra window" names "Zebra", not "the Zebra".
        target = re.sub(r"^(?:the|my|this|that)\s+", "", target,
                        flags=re.IGNORECASE)
        named = target and not _NOT_A_TARGET.match(target)
        # Closing is the one that can hurt: resolve the window NOW, and if
        # nothing by that name is open, a brain looks for it instead.
        if act == "close" and named and not _has_window(target):
            return None
        if act == "close":
            ask = _close_question(target if named else "", lang)
            if ask is not None:
                return ask
        if named:
            return Reflex("window", f"{act} {target}",
                          lambda x=act, n=target: _window(x, n))
        return Reflex("window", act, lambda x=act: _window(x))

    for rx in (_OPEN_RE, _OPEN_KA_RE, _OPEN_KA_END_RE):
        m = rx.match(full)
        if m:
            got = resolve_target(m.group("t"), lang)
            if got:
                return got
            break   # it WAS an open order, we just don't know the thing —
                    # let a brain figure it out rather than trying the next
                    # pattern on the same words.
    return None


# A fast hand returns this prefix when the job needs eyes after all.
DEFER = "DEFER"


def warm():
    """Called once at boot: build the disk index behind the UI, and generate
    the UI Automation bindings so his first "click …" isn't the slow one."""
    reflex_index.refresh(background=True)

    def _hands():
        try:
            import fast_hands
            fast_hands.warm()
        except Exception:  # noqa: BLE001 — clicks then defer to the brain
            pass
    import threading
    threading.Thread(target=_hands, daemon=True).start()
