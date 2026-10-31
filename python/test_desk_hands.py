"""The voice's desk hands and the 2026-10-31 log fixes, offline: text files
(never overwritten, only text, never system folders), the chat log with the
limit-retry spam folded, hours parsing, "that requires Claude" counted as a
refusal, a silent model sent to the back of the chain, "open my email" and
"shut down yourself". No Excel, no network, nothing opened on screen.
Run: py -3.13 test_desk_hands.py
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import desk_hands as dh
import talk_brain as tb
import voice_hands as vh
import reflex

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


tmp = tempfile.mkdtemp()
dh._NO_WRITE = ("\\windows\\",)          # the temp folder lives under AppData
dh._desktop = lambda: tmp

# ---- write_file ---------------------------------------------------------------
r = vh.execute("write_file", {"path": "shopping", "content": "milk\neggs", "open": False})
p = os.path.join(tmp, "shopping.txt")
check("a bare name lands on the Desktop as .txt", r.startswith("wrote") and os.path.exists(p), r)
r2 = vh.execute("write_file", {"path": "shopping", "content": "other", "open": False})
check("a taken name is never overwritten", "shopping (2).txt" in r2
      and open(p, encoding="utf-8").read() == "milk\neggs", r2)
vh.execute("write_file", {"path": "shopping.txt", "content": "bread", "mode": "append",
                          "open": False})
check("append adds a line", open(p, encoding="utf-8").read() == "milk\neggs\nbread")
check("no programs or scripts", vh.execute(
    "write_file", {"path": "x.bat", "content": "x", "open": False}).startswith("ERROR"))
check("no system folders", vh.execute(
    "write_file", {"path": r"C:\Windows\x.txt", "content": "x", "open": False}).startswith("ERROR"))
check("the voice declares its desk hands",
      {"write_file", "save_chat_log", "work_hours"} <= {d["name"] for d in vh.DECLS}
      and {"write_file", "save_chat_log", "work_hours"} <= vh.NAMES)

# ---- save_chat_log --------------------------------------------------------------
tx = os.path.join(tmp, "transcript.jsonl")
day = datetime.datetime(2026, 10, 28, 9, 0).timestamp()
rows = [{"t": day, "user": "set an alarm", "reply": "Sir, done."}]
rows += [{"t": day + 60 * i, "user": "[event: Claude's job failed]",
          "reply": f"Sir, out of usage again ({i})."} for i in range(1, 40)]
rows += [{"t": day + 3600 * 5, "user": "hello", "reply": "[[quiet]]"},
         {"t": day - 86400 * 5, "user": "old", "reply": "old"}]
with open(tx, "w", encoding="utf-8") as f:
    f.write("\n".join(json.dumps(x) for x in rows))
dh.TRANSCRIPT = tx
lines = dh.chat_lines(datetime.date(2026, 10, 28), datetime.date(2026, 10, 28), tx)
text = "\n".join(lines)
check("the chat log keeps his words and mine", "Giorgi: set an alarm" in text
      and "GOAT: Sir, done." in text, text[:200])
check("the 20-minute limit spam folds into one line",
      text.count("[event: Claude's job failed]") == 1 and "the same 38 more" in text, text)
check("days outside the range stay out", "old" not in text)
r = vh.execute("save_chat_log", {"from": "2026-10-28", "to": "2026-10-28", "open": False})
check("save_chat_log writes a file", r.split(": ", 1)[-1].startswith("wrote")
      and os.path.exists(os.path.join(tmp, "GOAT chat 2026-10-28.txt")), r)

# ---- hours ------------------------------------------------------------------------
check("hours: '10' '8:30' '7.5' '8h 15m' 'seven and a half'-ish",
      (dh._hours("10"), dh._hours("8:30"), dh._hours("7.5"), dh._hours("8h 15m"),
       dh._hours("7 and a half")) == (10, 8.5, 7.5, 8.25, 7.5))
check("hours shown as h:mm", dh._hm(8.1333) == "8:08")
check("'yesterday' and '29.10' are days", dh._day("yesterday", datetime.date(2026, 10, 31))
      == datetime.date(2026, 10, 30) and dh._day("29.10", datetime.date(2026, 10, 31))
      == datetime.date(2026, 10, 29))

# ---- the voice owns up instead of refusing ---------------------------------------
for said in ("Sir, I would gladly create that notepad for you, but that requires "
             "Claude's active brain",
             "my ability to compile and save chat logs depends entirely on Claude",
             "Sir, I truly wish I could, but my tools for editing spreadsheets require Claude"):
    check(f"refusal caught: {said[:40]!r}", bool(tb.REFUSE_RE.search(said)))
check("a plain action is no refusal", not tb.REFUSE_RE.search("Sir, opening Spotify."))

# ---- a model that keeps losing the race goes last --------------------------------
gt = tb.GeminiTalk.__new__(tb.GeminiTalk)
gt._down = {}
gt._slow = {tb.CHAIN[0]: time.time() + 60}
check("a slow model goes to the back of the chain",
      gt._models() == tb.CHAIN[1:] + tb.CHAIN[:1], gt._models())
check("Flash-Lite 3.5 is in the chain", "gemini-3.5-flash-lite" in tb.CHAIN)

# ---- reflexes from the log ----------------------------------------------------------
for said in ("Could you please open my email?", "open my inbox", "open email"):
    rx = reflex.match(said)
    check(f"{said!r} opens Gmail", rx is not None and "mail.google.com" in rx.detail,
          repr(rx))
for said in ("Okay, please shut down yourself.", "shut down GOAT", "turn yourself off"):
    rx = reflex.match(said)
    check(f"{said!r} quits GOAT", rx is not None and rx.kind == "quit", repr(rx))
check("'shut down the computer' is not GOAT quitting",
      getattr(reflex.match("shut down the computer"), "kind", "") != "quit")

# ---- name-only mode ---------------------------------------------------------------
import goat_app as g
check("'don't say anything if I don't call the name' turns name-only on",
      bool(g.NAME_ONLY_ON_RE.search("When I talk, don't say anything if I don't call the name.")))
check("'only answer when I call your name' turns it on",
      bool(g.NAME_ONLY_ON_RE.search("only answer when I call your name")))
check("'you can talk freely again' turns it off",
      bool(g.NAME_ONLY_OFF_RE.search("ok you can talk freely again"))
      and not g.NAME_ONLY_ON_RE.search("ok you can talk freely again"))
app = g.GoatApp.__new__(g.GoatApp)
app.name_only, app._exchanges, app._last_exchange = True, [("hi", "Sir, hello.")], time.monotonic()
check("name-only: no name -> ignored", app._name_gate("What do we do now, sorry?"))
check("name-only: my name -> heard", not app._name_gate("GOAT, what do we do now?"))
app._exchanges = [("x", "Sir, shall I send it?")]
check("name-only: his answer to my question -> heard", not app._name_gate("yes"))
app._last_exchange -= 60
check("name-only: a minute later it needs the name again", app._name_gate("yes"))
app.name_only = False
check("name-only off: no gate", not app._name_gate("what now"))

# ---- usage questions stay with the voice -------------------------------------------
check("'cloud usage is up?' is a usage question (the voice answers from the meter)",
      bool(g.USAGE_Q_RE.search("So I can see cloud usage is up, right?")))
check("'how long can I get my limit back' is a usage question",
      bool(g.USAGE_Q_RE.search("How long can I get my limit back? How many hours exactly?")))
check("'reset my router' is not", not g.USAGE_Q_RE.search("reset my router"))
app._plan_now = {"five_hour": 12.0, "five_hour_resets": "21:00", "seven_day": 3.0,
                 "seven_day_resets": "Sat 07 Nov 16:00",
                 "at": datetime.datetime.now().isoformat(timespec="seconds")}
app.turn_lang = "en"
ans = app._meter_answer("So I can see cloud usage is up, right?")
check("a usage question is answered from the meter, not sent to Claude",
      "12%" in ans and "21:00" in ans and "3%" in ans, ans)
for job in ("Take a look at how GOAT spends my usage",
            "why did the usage go up 12% on start",
            "fix the usage limit loop in Claude"):
    check(f"a usage JOB still goes to Claude: {job[:30]!r}", app._meter_answer(job) == "")
app._plan_now["at"] = (datetime.datetime.now() - datetime.timedelta(hours=1)).isoformat()
check("a stale meter doesn't answer", app._meter_answer("is Claude usage back?") == "")

# ---- a big cold session isn't re-cached at boot ---------------------------------
proj = os.path.join(tmp, "projects", "C--goat-workspace")
os.makedirs(proj)
sess_file = os.path.join(tmp, ".goat-session-py")
g.SESSION_FILE = sess_file


def fake_session(sid, ctx, idle_s):
    p = os.path.join(proj, sid + ".jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 4, "cache_read_input_tokens": ctx - 1004,
            "cache_creation_input_tokens": 1000}}}) + "\n")
    os.utime(p, (time.time() - idle_s,) * 2)
    with open(sess_file, "w") as f:
        f.write(sid)


real_weight = g.session_weight
g.session_weight = lambda sid: real_weight(sid, os.path.join(tmp, "projects"))
fake_session("cold-big", 128_523, 16 * 3600)
check("session_weight reads the last context size", g.session_weight("cold-big")[0] == 128_523)
check("a 128k session idle 16h is dropped at boot",
      g.drop_cold_heavy_session() == 128_523 and not os.path.exists(sess_file))
fake_session("warm-mid", 41_000, 600)
check("a 41k session used 10 minutes ago is kept",
      g.drop_cold_heavy_session() == 0 and os.path.exists(sess_file))
fake_session("cold-small", 33_000, 5 * 3600)
check("a near-baseline cold session is kept (a fresh one costs the same)",
      g.drop_cold_heavy_session() == 0 and os.path.exists(sess_file))
fake_session("cold-mid", 42_000, 2 * 3600)
check("a 42k session cold for 2h is dropped", g.drop_cold_heavy_session() == 42_000)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
