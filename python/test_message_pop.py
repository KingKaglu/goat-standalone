"""Tests for the message card that pops out beside the collapsed dot.

UI_CONFIG is redirected to a temp file before any window exists — the real
ui-config.json is Giorgi's live session (see test_bubble.py for the story).

Run: py -3.13 test_message_pop.py   (add --render to write a PNG to look at)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import ui_qt

_TMP_CFG = os.path.join(tempfile.gettempdir(), "goat-pop-test-config.json")
with open(_TMP_CFG, "w", encoding="utf-8") as _fh:
    json.dump({"theme": "ember", "scale": 1.0, "ontop": False}, _fh)
ui_qt.UI_CONFIG = _TMP_CFG

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
ok = fail = 0


def check(name, cond):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print("FAIL", name)


win = ui_qt.GoatWindow()
pop = win.pop
check("flags: frameless/top/tool",
      bool(pop.windowFlags() & Qt.FramelessWindowHint)
      and bool(pop.windowFlags() & Qt.WindowStaysOnTopHint)
      and bool(pop.windowFlags() & Qt.Tool))
check("never steals focus", pop.testAttribute(Qt.WA_ShowWithoutActivating))
check("hidden at start", not pop.isVisible())

win.show(); app.processEvents()
win.update_spoken("hello")            # expanded: no card
check("no card while expanded", not pop.isVisible())

win.collapse(); app.processEvents()
area = win.bubble.screen().availableGeometry()
win._on_event("you", "what's up")
win._on_event("delta", "x")
win.update_spoken("All systems nominal.")
app.processEvents()
check("card shows while collapsed", pop.isVisible())
check("card text", pop.label.text() == "All systems nominal.")
b = win.bubble.frameGeometry()
check("card beside the dot (vertically overlaps)",
      pop.y() < b.bottom() and pop.geometry().bottom() > b.top())
check("card left of a right-side dot", pop.geometry().right() < b.left())
check("card on screen", area.contains(pop.geometry()))
check("fade timer armed", pop._fade.isActive())

long = " ".join(f"word{i}" for i in range(200))
win.update_spoken(long); app.processEvents()
check("long text trimmed to tail", pop.label.text().startswith("…")
      and pop.label.text().endswith("word199") and len(pop.label.text()) <= 241)
check("card still on screen with long text", area.contains(pop.geometry()))

# Dot dragged to the left edge: card flips to its right and follows.
win.bubble.move(area.left() + 20, area.top() + 200); app.processEvents()
b = win.bubble.frameGeometry()
check("card follows dot", abs(pop.geometry().center().y() - b.center().y()) < 200
      or pop.y() == area.top())
check("card right of a left-side dot", pop.x() > b.right())

if "--render" in sys.argv:
    win.update_spoken("Consider it done. The build passed and the preview is up.")
    app.processEvents()
    pm = QPixmap(pop.size()); pm.fill(Qt.transparent)
    pop.render(pm)
    out = os.path.join(tempfile.gettempdir(), "goat-pop.png")
    pm.save(out); print("rendered", out)

win._on_event("you", "next")
check("new turn clears old card", not pop.isVisible())
win._on_event("delta", "x")
win.update_spoken("Next reply."); app.processEvents()
check("reply shows again", pop.isVisible())
win.expand(); app.processEvents()
check("expand hides card", not pop.isVisible())

win.collapse(); app.processEvents()
win.update_spoken("Next reply, more."); app.processEvents()
clicked = []
win.pop.clicked.connect(lambda: clicked.append(1))
from PySide6.QtTest import QTest  # noqa: E402
QTest.mouseClick(pop, Qt.LeftButton, pos=QPoint(10, 10)); app.processEvents()
check("click opens GOAT", clicked and win.isVisible() and not win.bubble.isVisible())

win.apply_theme("paper"); app.processEvents()
check("theme reaches card", pop._t.get("bg_top") == ui_qt.THEMES["paper"]["bg_top"])

print(f"{ok}/{ok + fail} passed")
sys.exit(1 if fail else 0)
