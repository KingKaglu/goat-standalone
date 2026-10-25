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
vh.phone({"op": "tap_text", "arg": "Roaming"})
check("tap a button by its text", sent[-1] == {"op": "tap_text", "text": "Roaming"}, sent[-1])
check("unknown op names the ones that exist", "screen" in vh.phone({"op": "fly"}))

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

# ---- wiring -----------------------------------------------------------------
names = {d["name"] for d in talk_brain.TOOLS[0]["functionDeclarations"]}
check("every hand is declared to the voice", vh.NAMES <= names, vh.NAMES - names)
check("the voice is told to use its own hands", "YOUR OWN HANDS" in talk_brain.TALK_SYSTEM
      and "terminal_claude" in talk_brain.TALK_SYSTEM)

# ---- the ear's German guard -------------------------------------------------
de = {"language_probabilities": {"de": 1.0, "en": 0.0}}
en = {"language_probabilities": {"en": 0.98, "de": 0.0}}
ka = {"language_probabilities": {"ru": 0.47, "en": 0.05, "de": 0.03}}
opus = {"language_probabilities": {"pt": 0.21, "en": 0.13, "de": 0.02}}
check("German nearby is someone else", stt_whisper._foreign(de) == "de")
check("his English is never foreign", stt_whisper._foreign(en) is None)
check("his Georgian is never foreign", stt_whisper._foreign(ka) is None)
check("a one-word English guess is never foreign", stt_whisper._foreign(opus) is None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
