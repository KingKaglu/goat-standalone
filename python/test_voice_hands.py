"""voice_hands.py offline: file search by name and by contents, reading,
safe opening, the phone ops' commands, the terminal-Claude report, and the
ear's German guard. No phone, no network, no real search of his drives.
Run: py -3.13 test_voice_hands.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import zipfile

import talk_brain
import voice_hands as vh
import stt_whisper

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


# ---- a little home of his ---------------------------------------------------
tmp = tempfile.mkdtemp()
desk = os.path.join(tmp, "Desktop")
ws = os.path.join(tmp, "workspace", "side-hustle")
os.makedirs(desk)
os.makedirs(ws)
os.makedirs(os.path.join(tmp, "node_modules", "junk"))
open(os.path.join(ws, "business-plan.md"), "w", encoding="utf-8").write(
    "# Side hustle plan\nAn AI front desk for a barbershop in Essen: bookings, WhatsApp.")
open(os.path.join(desk, "upwork-profile.md"), "w", encoding="utf-8").write("Freelance profile")
open(os.path.join(tmp, "node_modules", "junk", "barbershop.md"), "w").write("x")
open(os.path.join(desk, "setup.exe"), "wb").write(b"MZ")
with zipfile.ZipFile(os.path.join(desk, "cv.docx"), "w") as z:
    z.writestr("word/document.xml", "<w:document><w:p><w:t>Giorgi CV</w:t></w:p>"
                                    "<w:p><w:t>Python, voice AI</w:t></w:p></w:document>")
open(os.path.join(desk, "chunk-SNB7PWCV.js"), "w").write("x")   # a bundle, not his CV
page = os.path.join(desk, "page.html")
open(page, "w", encoding="utf-8").write("<html><script>x=1</script><h1>Hello &amp; welcome</h1></html>")

vh.ROOTS = [tmp]
vh.CONTENT_ROOTS = [os.path.join(tmp, "workspace"), desk]
vh._index, vh._docs = [], {}
vh.warm()
for _ in range(100):
    if vh._index and not vh._building.is_set():
        break
    time.sleep(0.05)

r = vh.find_files("upwork profile")
check("finds a file by its name", "upwork-profile.md" in r, r)
r = vh.find_files("the barbershop idea")
check("finds a document by what is written INSIDE it",
      "business-plan.md" in r and "Mentioned inside" in r, r)
check("dependency folders are never searched", "node_modules" not in r, r)
check("nothing found says so plainly", vh.find_files("zebra quantum").startswith("nothing found"))
r = vh.find_files("my cv")
check("a short word is a whole word: his CV, not a bundle with CV in its name",
      "cv.docx" in r and "PWCV" not in r, r)
check("reads a docx", "Giorgi CV" in vh.read_file(os.path.join(desk, "cv.docx")))
h = vh.read_file(page)
check("reads html as text", "Hello & welcome" in h and "x=1" not in h, h)
check("never opens a program", vh.open_path(os.path.join(desk, "setup.exe")).startswith("ERROR"))
check("lists a folder", "upwork-profile.md" in vh.list_folder(desk))

# ---- phone ------------------------------------------------------------------
sent = []
def fake_phone(cmd, timeout=25.0):
    sent.append(cmd)
    if cmd.get("op") == "screen":
        return {"ok": True, "app": "com.android.settings",
                "items": [{"t": "Mobile data", "c": 1}, {"t": "Roaming", "c": 1}]}
    return {"ok": True}


vh._phone_call = fake_phone
r = vh.phone({"op": "settings", "arg": "mobile"})
check("phone settings page opens the right intent",
      sent[-1]["action"] == "android.settings.DATA_ROAMING_SETTINGS", sent[-1])
r = vh.phone({"op": "screen"})
check("phone screen is read back as text", "Mobile data" in r and "[button]" in r, r)
vh.phone({"op": "alarm", "arg": "7:30", "arg2": "gym"})
ex = sent[-1]["extras"]
check("alarm 7:30", ex["android.intent.extra.alarm.HOUR"] == 7
      and ex["android.intent.extra.alarm.MINUTES"] == 30, ex)
vh.phone({"op": "volume", "arg": "all", "arg2": "100%"})
check("'all the volumes up' asks the phone for every stream at full",
      sent[-1]["action"] == "max" and sent[-1]["percent"] == 100, sent[-1])


def old_app(cmd, timeout=25.0):
    sent.append(cmd)
    if cmd.get("action") == "max":
        return {"ok": False, "error": "volume action? max"}
    return {"ok": True, "volume": 100}


vh._phone_call = old_app
r = vh.phone({"op": "volume", "arg": "max"})
check("an old phone app: media goes to full, and it says the alarm did NOT change",
      sent[-1] == {"op": "volume", "action": "set", "percent": 100} and "did NOT" in r, r)
vh._phone_call = lambda cmd, timeout=25.0: (sent.append(cmd) or {
    "ok": True, "locked": True, "items": [{"t": "Battery charging, 92 percent."}]})
r = vh.phone({"op": "settings", "arg": "battery"})
check("a locked phone: no false 'done' for a page he can't see",
      r.startswith("ERROR") and "LOCKED" in r, r)
check("...but what the lock screen shows comes along", "Battery charging" in r, r)
r = vh.phone({"op": "torch", "arg": "on"})
check("...the torch still works locked", r.startswith("done"), r)
vh._phone_call = fake_phone
vh.phone({"op": "tap_text", "arg": "Roaming"})
check("tap a button by its text", sent[-1] == {"op": "tap_text", "text": "Roaming"}, sent[-1])
check("unknown op names the ones that exist", "screen" in vh.phone({"op": "fly"}))

# texting: the app's Send button is pressed, and "sent" is checked
box = {"text": ""}


def messenger(cmd, timeout=25.0):
    sent.append(cmd)
    op = cmd.get("op")
    if op == "type":
        box["text"] = cmd["text"]
        return {"ok": True}
    if op == "screen":
        return {"ok": True, "app": "com.facebook.orca", "items": [
            {"t": "Mariam", "c": 1, "x": 200, "y": 100},
            {"t": box["text"], "edit": 1, "x": 300, "y": 1800},
            {"t": "Send", "c": 1, "x": 1000, "y": 1800}]}
    if op == "tap" and cmd.get("x") == 1000:
        box["text"] = ""          # Messenger sends and clears the box
    return {"ok": True}


vh._phone_call = messenger
r = vh.phone({"op": "type", "arg": "I love you", "arg2": "submit"})
check("typing a message presses the app's Send button", any(
    c.get("op") == "tap" and c.get("x") == 1000 for c in sent), sent[-3:])
check("...and says sent only when the box emptied", r.startswith("sent"), r)


def stuck(cmd, timeout=25.0):
    if cmd.get("op") == "screen":
        return {"ok": True, "items": [{"t": "I love you", "edit": 1}]}
    if cmd.get("op") == "tap_text":
        return {"ok": False, "error": "no match"}
    return {"ok": True}


vh._phone_call = stuck
r = vh.phone({"op": "type", "arg": "I love you", "arg2": "submit"})
check("no Send button: it says NOT sent (never a false 'sent')", "NOT sent" in r, r)

vh._phone_call = fake_phone
vh.phone({"op": "maps", "arg": "google.navigation:q=Hans-Böckler-Straße 23 Essen"})
check("a navigation link opens as it is", sent[-1]["data"].startswith("google.navigation:"), sent[-1])
vh.phone({"op": "maps", "arg": "car wash open now"})
check("words become a Maps search around his phone", sent[-1]["data"] == "geo:0,0?q=car%20wash%20open%20now",
      sent[-1])

# ---- the terminal's Claude --------------------------------------------------
vh._sessions = lambda: [{"id": "ab12", "kind": "background", "cwd": "C:\\Windows\\System32",
                         "name": "Continue previous work", "status": "busy",
                         "sessionId": "ab12-x"}]
vh._job_state = lambda short: {"detail": "Wiring the voice's hands",
                               "intent": "give the voice Claude's reach"}
r = vh.terminal_claude({"action": "status"})
check("terminal Claude: WORKING and on what", "WORKING" in r and "Wiring the voice's hands" in r, r)
r = vh.terminal_claude({"action": "continue"})
check("a working session is not resumed twice", "still working" in r, r)
r = vh.terminal_claude({"action": "send", "message": "continue", "at": "01:45"})
check("'continue it when my usage resets': a timed send is set",
      r.startswith("set: at 01:45") and vh._later and vh._later[-1][1] == "continue", r)
check("a bad time is refused plainly",
      vh.terminal_claude({"action": "send", "at": "25:99"}).startswith("ERROR"))

# ---- his browser history ----------------------------------------------------
import sqlite3
hist = os.path.join(tmp, "Chrome", "Default", "History")
os.makedirs(os.path.dirname(hist))
con = sqlite3.connect(hist)
con.execute("CREATE TABLE urls (url TEXT, title TEXT, visit_count INT, last_visit_time INT)")
now_c = int((time.time() + 11644473600) * 1_000_000)
con.executemany("INSERT INTO urls VALUES (?,?,?,?)", [
    ("https://www.imdb.com/title/tt-vq/", "VisionQuest (TV Series 2026) - IMDb", 4, now_c - 3_600_000_000),
    ("https://www.google.com/search?q=weather", "weather - Google Search", 1, now_c),
    ("https://old.example/visionquest", "VisionQuest rumours", 1, now_c - 90 * 86400 * 1_000_000)])
con.commit()
con.close()
vh._history_files = lambda: [hist]
r = vh.browser_history({"query": "vision quest"})
check("browser history finds the page he watched this week",
      "VisionQuest (TV Series 2026)" in r and "visited 4x" in r and "weather" not in r, r)
check("...and not one from three months ago", "rumours" not in r, r)
r = vh.browser_history({"query": "visionquest rumours", "days": 7})
check("nothing this week: it says when it last was",
      r.startswith("nothing") and "further back" in r and "VisionQuest rumours" in r, r)
check("no match says so plainly", vh.browser_history({"query": "zebra"}).startswith("nothing"))
check("he can't-type refusals are recognised, plain talk isn't",
      talk_brain.REFUSE_RE.search("Sir, I am unable to type commands directly into your terminal.")
      and talk_brain.REFUSE_RE.search("Sir, my hands don't have direct access to your history.")
      and talk_brain.REFUSE_RE.search("I do not have a feature to directly input your speech")
      and not talk_brain.REFUSE_RE.search("Sir, I can open the terminal for you."))

# ---- where he is -------------------------------------------------------------
import io
import json as _json
real_open = vh.urllib.request.urlopen
vh.urllib.request.urlopen = lambda url, timeout=8: io.BytesIO(_json.dumps({
    "status": "success", "city": "Essen", "regionName": "North Rhine-Westphalia",
    "country": "Germany", "zip": "45356", "timezone": "Europe/Berlin"}).encode())
r = vh.execute("where_am_i", {})
vh.urllib.request.urlopen = real_open
check("where am I: the city, honestly approximate", "Essen" in r and "approximately" in r, r)

# ---- wiring -----------------------------------------------------------------
names = {d["name"] for d in talk_brain.TOOLS[0]["functionDeclarations"]}
check("every hand is declared to the voice", vh.NAMES <= names, vh.NAMES - names)
check("the voice is told to use its own hands", "YOUR OWN HANDS" in talk_brain.TALK_SYSTEM
      and "terminal_claude" in talk_brain.TALK_SYSTEM)
import goat_app
check("Georgian replies say ბატონო, not Sir", "ბატონო" in goat_app.KA_TALK_NOTE
      and "ბატონო" not in talk_brain.TALK_SYSTEM)

# ---- the ear's German guard -------------------------------------------------
de = {"language_probabilities": {"de": 1.0, "en": 0.0}}
en = {"language_probabilities": {"en": 0.98, "de": 0.0}}
ka = {"language_probabilities": {"ru": 0.47, "en": 0.05, "de": 0.03}}
opus = {"language_probabilities": {"pt": 0.21, "en": 0.13, "de": 0.02}}
check("German nearby is someone else", stt_whisper._foreign(de) == "de")
check("English mode sends his Georgian to the Georgian ear by default",
      stt_whisper.EN_KA_CLOUD or os.environ.get("GOAT_STT_EN_KA_CLOUD"))
check("his English is never foreign", stt_whisper._foreign(en) is None)
check("his Georgian is never foreign", stt_whisper._foreign(ka) is None)
check("a one-word English guess is never foreign", stt_whisper._foreign(opus) is None)

import stt_live
check("Latin letters inside a Georgian word are mended",
      stt_live._one_script("მითხari რომელი Opus") == "მითხარი რომელი Opus",
      stt_live._one_script("მითხari რომელი Opus"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
