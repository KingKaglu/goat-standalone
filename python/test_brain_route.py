"""Auto brain (2026-10-01): talk + simple tasks on Sonnet 5.5, code and
serious questions on his working brain — and the UI shows which.

Run:  py -3.13 test_brain_route.py
"""
import json
import os
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)
import brain_route as br  # noqa: E402
import ui_qt  # noqa: E402

PASS = FAIL = 0


def check(name, ok, info=""):
    global PASS, FAIL
    PASS += bool(ok)
    FAIL += not ok
    print(("[PASS] " if ok else "[FAIL] ") + name + ("" if ok else f"  {info}"))


# His real lines (workspace/transcript.jsonl), sorted by what they deserve.
CASES = [
    # talk -> sonnet, low
    ("How are you, Ava, have a good day, bye bye.", br.TALK),
    ("So, everything done? Do I need to restart?", br.TALK),
    ("GG.", br.TALK),
    ("What's the capital of Australia?", br.TALK),
    ("No, no, no need to check.", br.TALK),
    # unsure / advice / long rambles -> Sonnet, low (his order 2026-10-17)
    ("Should I take the job offer or stay where I am?", br.TALK),
    ("Explain how the prompt cache works.", br.TALK),
    ("word " * 50, br.TALK),
    # simple tasks -> Sonnet, low
    ("Uh, open download files.", br.TASK),
    ("Click Internal Storage.", br.TASK),
    ("Okay, close the previous tab. I don't need it.", br.TASK),
    ("Change my keyboard language from Georgian to English", br.TASK),
    ("Okay. Delete all the League of Legends images.", br.TASK),
    ("კაი, კამერა გახსენი ახლა", br.TASK),
    ("კარგი, შეგიძლია Google გამიხსნა?", br.TASK),
    # deep -> his working brain, his dial
    ("Can you build this for me?", br.DEEP),
    ("Uh, commit everything to GitHub. Every last update. Okay.", br.DEEP),
    ("Okay, the Intel graphics. It's, it's 12:00 this morning. Install now.", br.DEEP),
    ("Fix the scroll bug in ui_qt.py", br.DEEP),
    ("Make me a plan for the launch week.", br.DEEP),
    ("კოდში ბაგია, გაასწორე", br.DEEP),
]
for text, want in CASES:
    got = br.classify(text)
    check(f"{text[:50]!r} -> {want}", got == want, got)

check("an explicit hard order is deep", br.classify("what's up", hard=True) == br.DEEP)
check("an order right after a deep turn stays deep",
      br.classify("Now push it.", last_tier=br.DEEP, since_last=30) == br.DEEP)
check("'try again' right after a deep turn stays deep",
      br.classify("Okay, try again.", last_tier=br.DEEP, since_last=30) == br.DEEP)
check("thanks after a deep turn is talk",
      br.classify("Thanks, man.", last_tier=br.DEEP, since_last=30) == br.TALK)
check("an order long after a deep turn is a task again",
      br.classify("Close this tab.", last_tier=br.DEEP,
                  since_last=br.FOLLOW_S + 1) == br.TASK)

# Share of his history that stays on Sonnet — the point of the split.
tx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                  "workspace", "transcript.jsonl")
if os.path.exists(tx):
    lines = []
    for ln in open(tx, encoding="utf-8"):
        try:
            u = (json.loads(ln).get("user") or "").strip()
        except json.JSONDecodeError:
            continue
        if u and not u.startswith("["):
            lines.append(u)
    deep = sum(br.classify(u) == br.DEEP for u in lines)
    print(f"      his {len(lines)} lines: {deep} deep ({deep / len(lines):.0%}), "
          f"the rest on sonnet 5.5")
    check("most of his turns run on the talk brain", deep / len(lines) < 0.3)

# ---- the UI shows it ------------------------------------------------------
ui_qt.UI_CONFIG = os.path.join(tempfile.gettempdir(), "goat-test-brain-cfg.json")
win = ui_qt.GoatWindow()
win.cfg["work_model"] = "opus 5.5"
win.cfg["effort"] = "max"

win._on_event("you", "how's it going?")
win._on_event("delta", "")
win._on_event("model", "sonnet 5.5")
win._on_event("brain", "sonnet 5.5|talk|low")
win._on_event("work_start", "sonnet 5.5|how's it going?")
check("card names the brain answering now", win.model_name.text() == "Sonnet 5.5",
      win.model_name.text())
check("card says it was picked automatically for talk",
      "Auto · talking" in win.model_sub.text()
      and "Opus\u00a05.5 takes the hard work" in win.model_sub.text(),
      win.model_sub.text())
check("card pill shows this turn's depth", win.model_pill.text() == "low",
      win.model_pill.text())
check("chip keeps his pick (it is a selector)", "Opus 5.5" in win.chip_model.text(),
      win.chip_model.text())
check("activity line: brain and real depth",
      win.work_panel.sub.text().startswith("sonnet 5.5 · low"),
      win.work_panel.sub.text())
check("the reply knows who answered it",
      win._reply_label is not None
      and win._reply_label.toolTip() == "answered by sonnet 5.5",
      win._reply_label and win._reply_label.toolTip())

win._on_event("you", "fix the scroll bug")
win._on_event("delta", "")
win._on_event("model", "opus 5.5")
win._on_event("brain", "opus 5.5|deep|max")
win._on_event("work_start", "opus 5.5|fix the scroll bug")
check("deep turn: card switches to opus",
      win.model_name.text() == "Opus 5.5"
      and "code & serious work" in win.model_sub.text(),
      (win.model_name.text(), win.model_sub.text()))
check("deep turn: activity line at his dial",
      win.work_panel.sub.text().startswith("opus 5.5 · max"),
      win.work_panel.sub.text())

shot = os.path.join(tempfile.gettempdir(), "goat-brain-ui.png")
win.resize(1400, 860)
win.show()
app.processEvents()
win.grab().save(shot)
print(f"      screenshot: {shot}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
