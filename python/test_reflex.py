"""Reflex-lane tests — the router that makes his device commands instant.

Two jobs, and the second matters more than the first:

  1. Everything he actually says must MATCH, in English and Georgian, and
     resolve to the right thing.
  2. Everything else must NOT match. A reflex that fires on "fix the build"
     or "how do I open a file?" is worse than a slow GOAT — it would act on a
     question. Every miss here has to fall through to a brain.

Free: no model, no network, no audio. Run it with
    py -3.13 test_reflex.py
"""
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import close_guard     # noqa: E402
import reflex          # noqa: E402
import reflex_index    # noqa: E402

# The close guard looks at whatever window is really in front — when these
# tests run from a terminal, that's the terminal, and every "close it" would
# (rightly) turn into a question. Pin it: nothing is protected unless a test
# says so (see the close-guard section at the end).
_protected = {"reason": None}
close_guard.window_reason = lambda hwnd, title="": _protected["reason"]

_passed = 0
_failed = 0


def check(label: str, ok: bool, extra: str = ""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"[PASS] {label}")
    else:
        _failed += 1
        print(f"[FAIL] {label}" + (f"  — {extra}" if extra else ""))


def hits(text: str, kind: str, lang: str = "en", detail: str = ""):
    r = reflex.match(text, lang)
    if r is None:
        check(f"{text!r} -> {kind}", False, "no match at all")
        return
    ok = r.kind == kind and (not detail or detail.lower() in r.detail.lower())
    check(f"{text!r} -> {kind}" + (f" ({detail})" if detail else ""), ok,
          f"got {r.kind}: {r.detail}")


def falls_through(text: str, lang: str = "en"):
    r = reflex.match(text, lang)
    check(f"{text!r} stays with the brains", r is None,
          f"reflex grabbed it as {r.kind}: {r.detail}" if r else "")


# ---------------------------------------------------------------- the index
t0 = time.perf_counter()
reflex_index.refresh(background=False)
build_s = time.perf_counter() - t0
check(f"index builds ({len(reflex_index._entries)} entries in {build_s:.2f}s)",
      reflex_index.ready() and build_s < 10)

dirs = reflex_index.user_dirs()
check("desktop resolved through Windows, not string-joined",
      bool(dirs.get("desktop")) and os.path.isdir(dirs["desktop"]),
      str(dirs))
# The whole point of asking Windows: OneDrive redirection must be followed.
check("redirected desktop is the one with his files in it",
      os.path.isdir(dirs.get("desktop", "")))

# --------------------------------------------------------------- open: web
hits("open google", "open_url", detail="google.com")
hits("open Google", "open_url", detail="google.com")
hits("hey goat, open google please", "open_url", detail="google.com")
hits("go to youtube", "open_url", detail="youtube.com")
hits("pull up gmail", "open_url", detail="mail.google.com")
hits("open github.com", "open_url", detail="github.com")
hits("open https://vercel.com/dashboard", "open_url", detail="vercel.com")
hits("გახსენი გუგლი", "open_url", "ka", "google.com")
hits("გუგლი გახსენი", "open_url", "ka", "google.com")
hits("გამიხსენი იუთუბი", "open_url", "ka", "youtube.com")

# --------------------------------------------------------------- open: apps
hits("open notepad", "open_app", detail="notepad")
hits("open spotify", "open_app", detail="spotify")
hits("open calculator", "open_app", detail="calc")

# ------------------------------------------------------- open: his own disk
# The exact sentence that cost him ~20s and a screenshot hunt on 2026-09-15.
# His real GG folder has since left the desktop, so the test brings its own
# and removes it again (only if it made it).
_gg = os.path.join(dirs["desktop"], "GG")
_made_gg = not os.path.exists(_gg)
if _made_gg:
    os.makedirs(_gg)
try:
    reflex_index.refresh(background=False)
    hits("ოქეი, გახსენი ახლა აპლიკაცია, უფრო სწორად ფაილი, სახელად GG, "
         "დესკტოპზე არის", "open_path", "ka", "gg")
    hits("open the file called GG on my desktop", "open_path", detail="gg")
    hits("open gg", "open_path", detail="gg")
    hits("open the folder gg", "open_path", detail="gg")
finally:
    if _made_gg:
        os.rmdir(_gg)
        reflex_index.refresh(background=False)

# 2026-09-18: the name sits in the first sentence; the rest is chatter.
_pic = os.path.join(dirs["desktop"], "goat-selftest-pfp.jpg")
_made_pic = not os.path.exists(_pic)
if _made_pic:
    open(_pic, "wb").close()
try:
    hits("Hey, can you open the goat-selftest-pfp picture? For me. That I have "
         "on my desktop.", "open_path", detail="goat-selftest-pfp")
    hits("open the goat-selftest-pfp photo that I have on my desktop",
         "open_path", detail="goat-selftest-pfp")
finally:
    if _made_pic:
        os.remove(_pic)

# A file saved AFTER the index was built must still open — "I just downloaded
# it, open it" is one of the most natural things he can say, and a boot-time
# index is blind to it without a rescan on miss.
_live = os.path.join(dirs["desktop"], "goat-reflex-selftest.txt")
try:
    with open(_live, "w", encoding="utf-8") as f:
        f.write("x")
    t0 = time.perf_counter()
    r = reflex.match("open goat-reflex-selftest")
    live_ms = (time.perf_counter() - t0) * 1000
    check(f"a file created after boot still resolves ({live_ms:.1f}ms)",
          r is not None and r.kind == "open_path"
          and "selftest" in r.detail and live_ms < 500,
          f"got {r.kind + ': ' + r.detail if r else None}")
finally:
    try:
        os.remove(_live)
    except OSError:
        pass

# ------------------------------------------------------------------ devices
hits("volume up", "volume", detail="up")
hits("turn it down", "volume", detail="down")
hits("louder", "volume", detail="up")
hits("mute", "volume", detail="mute")
hits("set volume to 40", "volume", detail="40")
hits("ხმა აუწიე", "volume", "ka", "up")
hits("ხმა დაუწიე", "volume", "ka", "down")
hits("დადუმდი", "volume", "ka", "mute")

hits("next track", "media", detail="next")
hits("skip", "media", detail="next")
hits("previous", "media", detail="previous")
hits("pause", "media", detail="play")
hits("შემდეგი", "media", "ka", "next")
hits("წინა", "media", "ka", "previous")

hits("brightness 70", "brightness", detail="70")
hits("dim the screen", "brightness", detail="down")

hits("lock the screen", "lock")
hits("lock", "lock")
hits("დაბლოკე ეკრანი", "lock", "ka")
hits("take a screenshot", "screenshot")

hits("maximize this window", "window", detail="maximize")
hits("minimize it", "window", detail="minimize")
hits("close it", "window", detail="close")
hits("გაადიდე ფანჯარა", "window", "ka", "maximize")
hits("დახურე ფანჯარა", "window", "ka", "close")
# Window named by title, STT-garbled ("გაადგიდე"), with a trailing intensifier.
hits("გაადგიდე გუგლის ფანჯარა ბოლომდე", "window", "ka", "maximize")
hits("გამორთე ხმა", "volume", "ka", "mute")
hits("ხმა გამორთე", "volume", "ka", "mute")

# Quitting apps — process level, every target must be known.
hits("close steam", "quit", detail="steam")
hits("Can you turn off the Ubisoft and the Roblox for me, and the Steam?",
     "quit", detail="ubisoft, roblox, steam")
hits("დახურე სტიმი", "quit", "ka", "steam")
hits("სტიმი და რობლოქსი გათიშე", "quit", "ka", "steam, roblox")
hits("kill discord", "quit", detail="discord")
# Exactly what the realtime ear wrote back on 2026-09-23 (test_voice_e2e).
hits("გამორთე, ხმა.", "volume", "ka", "mute")
hits("დახურეს, ტიმი.", "quit", "ka", "steam")
hits("Google-ი გახსენი.", "open_url", "ka", "google.com")
# Unknown targets never get a process killed or the front window closed.
falls_through("close zzzznotawindow")
falls_through("დახურე ზზზზარაფერი", "ka")
falls_through("turn off the wifi")
falls_through("გამორთე ლეპტოპი", "ka")
check("a named window that is not open reports an error, never closes the "
      "front one",
      reflex._window("close", "zzzznotawindow").startswith("ERROR"))
# A fake app, so the test can never kill anything real of his.
reflex.QUIT_APPS["goat-selftest"] = ("zzzz-goat-selftest",)
check("quitting an app that is not running says so, never 'closed it'",
      "was not running" in reflex._quit_apps(["goat-selftest"]))
del reflex.QUIT_APPS["goat-selftest"]

# ----------------------------------------------- answers GOAT gives itself
hits("what time is it", "answer", detail="time")
hits("what's the date", "answer", detail="date")
hits("battery", "answer", detail="battery")
hits("რომელი საათია", "answer", "ka", "time")
hits("ბატარეა", "answer", "ka", "battery")
check("the clock answer is a real time, not a placeholder",
      any(c.isdigit() for c in (reflex.match("what time is it").speak or "")))

# ------------------------------------------------- MUST fall through ------
# Questions are never orders.
falls_through("how do I open a file")
falls_through("what is google")
falls_through("why is google slow")
falls_through("როგორ გავხსნა ფაილი", "ka")
# Real work belongs to the working brain.
falls_through("fix the build")
falls_through("გაასწორე ბილდი", "ka")
falls_through("write me a poem about goats")
falls_through("deploy fasmetri")
# Opening verbs over work-shaped objects: a folder named `tests` must never
# be double-clicked because he said "run the tests".
falls_through("run the tests")
falls_through("start the server")
falls_through("start the dev server")
falls_through("open a pull request")
falls_through("open the repo")
# Compound requests are conversation, not a reflex.
falls_through("open google and then tell me the news")
falls_through("open youtube and explain how it works")
# Unknown targets go to a brain rather than opening something random.
falls_through("open zzzznotathingonthismachine")
# A paragraph is never a device command.
falls_through("so I was thinking about the way we handle the catalog sync "
              "and whether we should open the possibility of a second store "
              "adapter before the launch, what do you think about that plan")

# ------------------------------------------------------------- fast hands
# 2026-10-01, his complaint: simple tab/click orders took ~10s through the
# brain. They must be reflexes now — and the ambiguous ones must NOT be.
hits("close the tab", "keys", detail="close 1 tab")
hits("close this tab", "keys", detail="close 1 tab")
hits("Could you please close the two tabs I have open?", "keys", detail="close 2 tabs")
hits("close 3 tabs", "keys", detail="close 3 tabs")
hits("close all tabs", "keys", detail="close all tabs")
hits("დახურე ტაბი", "keys", "ka", detail="close 1 tab")
hits("ტაბი დახურე", "keys", "ka", detail="close 1 tab")
hits("დახურე ორი ტაბი", "keys", "ka", detail="close 2 tabs")
hits("open a new tab", "keys", detail="new tab")
hits("new tab", "keys", detail="new tab")
hits("reopen the closed tab", "keys", detail="reopen tab")
hits("next tab", "keys", detail="next tab")
hits("go to the previous tab", "keys", detail="previous tab")
hits("go back", "keys", detail="back")
hits("refresh the page", "keys", detail="refresh")
hits("scroll down", "keys", detail="scroll down")
hits("ჩამოსქროლე", "keys", "ka", detail="scroll down")
hits("zoom in", "keys", detail="zoom in")
hits("press enter", "keys", detail="press enter")
hits("press escape", "keys", detail="press esc")
hits("copy that", "keys", detail="copy")
hits("paste it", "keys", detail="paste")
hits("save it", "keys", detail="save")
hits("click subscribe", "click", detail="subscribe")
hits("click on the Settings button", "click", detail="settings")
hits("please click Sign in", "click", detail="sign in")
hits("Subscribe-ზე დააჭირე", "click", "ka", detail="subscribe")
hits("დააჭირე Subscribe", "click", "ka", detail="subscribe")
# Pointing needs eyes; plural tabs with no count needs a question.
falls_through("click this")
falls_through("click here")
falls_through("click that one")
falls_through("close the tabs")
falls_through("hit me with a joke")
falls_through("how do I close a tab?")
# "next" alone is still the media key, not a tab.
hits("next", "media", detail="next")
# From his live session the same night: each of these cost 3-4s in the brain.
hits("could you scroll this", "keys", detail="scroll down")
hits("keep scrolling", "keys", detail="scroll down")
hits("scroll up a bit", "keys", detail="scroll up")
hits("Okay, close it now.", "window", detail="close")
hits("click Node.js", "click", detail="node.js")
# The ear heard "click stop. First image you see." — a description, not a name.
falls_through("Okay, click stop. First image you see.")

# ------------------------------------------------------------------- speed
# The claim is "instant". Prove the router itself is not the cost.
SAMPLE = ["open google", "open gg", "volume up", "what time is it",
          "გახსენი გუგლი", "fix the build", "open zzzznotathing"]
t0 = time.perf_counter()
for _ in range(200):
    for s in SAMPLE:
        reflex.match(s, "en")
per = (time.perf_counter() - t0) / (200 * len(SAMPLE)) * 1e6
check(f"match costs {per:.0f}us per input (budget 5000us)", per < 5000)

# ------------------------------------------------------------- close guard
# 2026-10-01: "close this window I have opened" closed his live Claude Code
# terminal. A protected window is asked about, never closed outright.
import fast_hands  # noqa: E402
_real_target = fast_hands.target_window
fast_hands.target_window = lambda browser=False: {
    "hwnd": 4242, "pid": 1, "title": "Test it out", "app": "WindowsTerminal"}
_protected["reason"] = "Windows Terminal (Test it out) — a terminal with a running session"
r = reflex.match("close this window", "en")
check("protected window: 'close this window' asks instead of closing",
      r is not None and r.kind == "confirm" and "Close it anyway" in r.speak,
      repr(r))
check("the question names the window", r is not None and "Test it out" in r.speak)
close_guard.clear()
if r is not None:
    check("asking holds the close for his yes", r.run() == "asked first"
          and close_guard.pending() is not None
          and close_guard.pending()["run"] is not None)
hits("minimize it", "window", detail="minimize")    # only CLOSING asks
r = reflex.match("დახურე ფანჯარა", "ka")
check("Georgian close asks in Georgian",
      r is not None and r.kind == "confirm" and "დავხურო" in r.speak)
_protected["reason"] = None
hits("close it", "window", detail="close")          # unprotected: instant
close_guard.clear()
fast_hands.target_window = _real_target

for s in ("yes", "Yeah, close it.", "ok go ahead", "კი", "do it please"):
    check(f"{s!r} is a yes", bool(close_guard.YES_RE.match(s)))
for s in ("yes but wait, which one?", "close the other one", "what?"):
    check(f"{s!r} is NOT a yes", not close_guard.YES_RE.match(s))
for s in ("no", "No, don't.", "wait", "არა"):
    check(f"{s!r} is a no", bool(close_guard.NO_RE.match(s)))

# The brain's shell: the exact command that closed his terminal.
_real_pr = close_guard.process_reason
close_guard.process_reason = (lambda pid, kill=False, title="":
                              "a terminal" if pid == 17688 else None)
check("guard reads (Get-Process -Id N).CloseMainWindow()",
      close_guard.check_command(
          "(Get-Process -Id 17688).CloseMainWindow() | Out-Null") == "a terminal")
check("guard reads taskkill /PID N /F",
      close_guard.check_command("taskkill /PID 17688 /F") == "a terminal")
check("guard ignores commands that close nothing",
      close_guard.check_command("Get-Process -Id 17688 | select CPU") is None)
check("guard ignores closes aimed elsewhere",
      close_guard.check_command("Stop-Process -Id 999") is None)
close_guard.process_reason = _real_pr

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
