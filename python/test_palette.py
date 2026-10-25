"""Tests for v8 phase (e): the Ctrl K palette and the proactive cards.

Covers ranking/grouping (pure), and the window: Ctrl K opens the palette
with Actions/Settings/Skills/Pages, typing filters, Enter runs the chosen
entry, Esc closes; Ctrl F scopes it to conversations; typing anywhere lands
in the message box; a "card" event shows a card that rises in, never takes
focus, and leaves on its own (life shortened for the test). Renders shots
to workspace/goat-redesign/after-e/.

Run: py -3.13 test_palette.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import ui_qt  # noqa: E402
import ui_palette as up  # noqa: E402

_TMP_CFG = os.path.join(tempfile.gettempdir(), "goat-palette-test-config.json")
with open(_TMP_CFG, "w", encoding="utf-8") as _fh:
    json.dump({"theme": "midnight", "scale": 1.0, "ontop": False}, _fh)
ui_qt.UI_CONFIG = _TMP_CFG

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

SHOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "workspace", "goat-redesign", "after-e")
FAILS: list = []


def check(cond: bool, what: str):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def pump(ms: int = 0):
    end = time.time() + ms / 1000
    while True:
        QApplication.processEvents()
        if time.time() >= end:
            break
        time.sleep(0.01)


def test_rank():
    print("rank / filter")
    check(up.rank("re", "Restart GOAT") > up.rank("re", "Theme · Ember"),
          "prefix beats substring")
    check(up.rank("goat", "Restart GOAT") > 0, "word start matches")
    check(up.rank("rsg", "Restart GOAT") > 0, "letters in order match")
    check(up.rank("zzz", "Restart GOAT") == 0, "no match is 0")
    check(up.rank("mic", "Mute the mic") > up.rank("mic", "x", "microphone"),
          "title beats keywords")
    E = up.Entry
    es = [E("Actions", "Restart GOAT", "", None), E("Actions", "New chat", "", None),
          E("Settings", "Theme · Ember", "", None), E("Pages", "Memory", "", None),
          E("Skills", "morning-briefing", "", None, "start of day brief")]
    got = [e.title for e in up.filter_entries(es, "re")]
    check(got[0] == "Restart GOAT", f"best first: {got}")
    check("New chat" not in got, f"non-matches dropped: {got}")
    got = [e.section for e in up.filter_entries(es, "")]
    check(got == ["Actions", "Actions", "Settings", "Pages", "Skills"],
          "empty query keeps the given order")


def test_window():
    print("window")
    os.makedirs(SHOTS, exist_ok=True)
    w = ui_qt.GoatWindow()
    w.resize(1600, 900)
    w.show()
    pump(50)
    w.show_page("chat")
    pump(50)
    pal = w.palette
    w._show_cmd()
    pump(200)
    check(pal.isVisible() and pal.mode == "all", "Ctrl K opens the palette")
    check(pal.box.hasFocus() or QApplication.focusWidget() is pal.box,
          "the palette box has focus")
    heads = [pal.list.item(i).text() for i in range(pal.list.count())
             if pal.list.item(i).data(up.ROLE_KIND) == "head"]
    check(heads[:4] == ["Actions", "Settings", "Skills", "Pages"], f"sections: {heads}")
    QTest.keyClicks(pal.box, "re")
    pump(250)
    cur = pal.list.currentItem()
    check(cur is not None and cur.text() == "Restart GOAT",
          f"'re' -> Restart GOAT first ({cur.text() if cur else None})")
    room = w._room_rect()
    g = pal.panel.geometry()
    check(abs(g.center().x() - room.center().x()) <= 2, "panel centred over the room")
    w.grab().save(os.path.join(SHOTS, "C-palette.png"))
    pal.box.clear()
    QTest.keyClicks(pal.box, "memory")
    pump(50)
    # Enter on "Memory" page
    cur = pal.list.currentItem()
    check(cur is not None and cur.text() == "Memory", f"'memory' -> Memory page ({cur.text() if cur else None})")
    QTest.keyClick(pal.box, Qt.Key_Return)
    pump(80)
    check(not pal.isVisible(), "Enter closes the palette")
    check(w._page == "memory", f"Enter ran it: page {w._page}")
    w._show_cmd()
    pump(50)
    QTest.keyClick(pal.box, Qt.Key_Escape)
    pump(30)
    check(not pal.isVisible(), "Esc closes an empty palette")
    w._show_search()
    pump(80)
    check(pal.isVisible() and pal.mode == "chat", "Ctrl F opens conversation search")
    secs = {e.section for e in pal._entries}
    check(secs <= {"Conversations"}, f"Ctrl F only conversations: {secs}")
    w.grab().save(os.path.join(SHOTS, "C-search.png"))
    pal.close_palette()
    # typing anywhere goes to the message box
    w.show_page("chat")
    w.setFocus()
    w.input.clearFocus()
    pump(30)
    QTest.keyClick(w, Qt.Key_H, Qt.NoModifier)
    pump(30)
    check(w.input.hasFocus() and w.input.text().endswith("h"),
          f"typing anywhere -> box ({w.input.text()!r})")
    w.input.clear()
    w.input.clearFocus()
    w.setFocus()
    QTest.keyClick(w, Qt.Key_Slash)
    pump(30)
    check(w.input.hasFocus() and w.input.text() == "", "'/' focuses the box, types nothing")
    # proactive card
    card = w.pcard
    card.LIFE_MS = 1200
    focus_before = QApplication.focusWidget()
    w._on_event("card", json.dumps({"text": "Download finished: report.pdf",
                                    "kicker": "heads up", "page": "files"}))
    pump(60)
    y_early = card.y()
    pump(400)
    check(card.isVisible(), "card shows")
    check(card.y() < y_early, f"card rises ({y_early} -> {card.y()})")
    check(QApplication.focusWidget() is focus_before, "card never takes focus")
    box = w._pcard_box()
    check(card.geometry().right() <= box.right() + 1
          and card.geometry().bottom() <= box.bottom() + 1, "card sits above the composer")
    check(card.btn_row.isVisible(), "page -> Open button")
    w.grab().save(os.path.join(SHOTS, "D-card.png"))
    w._on_event("card", json.dumps({"text": "second", "key": "x"}))
    pump(1700)
    check(card.showing() in ("", "second"), "first card left on its own")
    pump(1900)
    check(not card.isVisible(), f"queue drains ({card.showing()!r})")
    w._on_event("limit", "Out of Claude usage until 17:00.")
    pump(300)
    check(card.showing().startswith("Out of Claude usage"), "usage limit -> card")
    card.dismiss()
    pump(300)
    check(not card.isVisible(), "dismiss hides it")
    w.close()


if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    test_rank()
    test_window()
    print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED'}")
    sys.exit(1 if FAILS else 0)
