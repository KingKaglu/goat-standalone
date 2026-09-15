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

import reflex          # noqa: E402
import reflex_index    # noqa: E402

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
hits("ოქეი, გახსენი ახლა აპლიკაცია, უფრო სწორად ფაილი, სახელად GG, "
     "დესკტოპზე არის", "open_path", "ka", "gg")
hits("open the file called GG on my desktop", "open_path", detail="gg")
hits("open gg", "open_path", detail="gg")
hits("open the folder gg", "open_path", detail="gg")

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

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
