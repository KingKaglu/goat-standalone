"""Tests for the v8 Home desk (phase c, design.md 2026-10-24).

Covers the memory parsers (Open list titles/subs, Recent days), and the
window: the desk's three columns fill, search results take the desk's place
and give it back, the home presence is driven by hud_tick, the brains line
names both brains and Claude's state, the ambient line never raises.

UI_CONFIG goes to a temp file first: a GoatWindow writes preferences, and the
real ui-config.json is his live session.

Run: py -3.13 test_home_desk.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import ui_qt  # noqa: E402

_TMP_CFG = os.path.join(tempfile.gettempdir(), "goat-desk-test-config.json")
with open(_TMP_CFG, "w", encoding="utf-8") as _fh:
    json.dump({"theme": "midnight", "scale": 1.0, "ontop": False}, _fh)
ui_qt.UI_CONFIG = _TMP_CFG

from PySide6.QtWidgets import QApplication  # noqa: E402

FAILS: list = []


def check(cond: bool, what: str):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


MEMORY = """# GOAT memory

## Open
- 2026-10-24 18:5x: GOAT v8 REDESIGN (his order: make it alive). Phase (a) DONE (345caab). Next: phase (c) home desk + ambient line, then d-f (preflight, tests, idle CPU, commit each).
- 2026-10-22 23:25: FABLE 5 ON CREDITS (his goal): wants Fable. Blocked: credits are PREPAID. Next: he buys credits ("Buy usage credits" on claude.ai Settings > Usage) or turns on auto-reload, then I switch.
- 2026-10-19 08:2x: PHONE INTERRUPT FIX BUILT: echo canceller. UNVERIFIED on phone: he installs the APK, talks over me.
- 2026-10-18 14:2x: LOST-REPLY FIX (ec73b09): side talk kept. NEXT (his order): full audit of the phone link.
- 2026-10-16: Tech news TikTok first video ready, not posted (technews/2026-10-16/).

## Giorgi
- 2026-10-18: not an open item.
"""

SELF = """# Me
## Recent days (written automatically)
- 2026-10-22 (Thu): A late-night run of Claude credits and a pay sheet. He sounded tired.
- 2026-10-21 (Wed): OpenAI dispute, RAM cleanup. He was startled once.
## Keeping this file
- not a day
"""


def test_parsers():
    print("parsers")
    n, rows = ui_qt.desk_open(MEMORY, n=5)
    check(n == 5, f"counts every Open item ({n})")
    titles = [t for t, _s in rows]
    subs = [s for _t, s in rows]
    check(titles[0] == "GOAT v8 redesign", f"shouted lead, acronym kept: {titles[0]!r}")
    check(subs[0] == "phase (c) home desk + ambient line, then d-f",
          f"sub = Next, long parenthetical dropped: {subs[0]!r}")
    check(titles[1] == "Fable 5 on credits", f"small words lowered: {titles[1]!r}")
    check(subs[1].startswith("he buys credits or turns on auto-reload"),
          f"Next wins over Blocked: {subs[1]!r}")
    check(titles[2] == "Phone interrupt fix built", f"3-letter shout lowered: {titles[2]!r}")
    check(subs[2].startswith("untested on phone"), f"UNVERIFIED -> untested: {subs[2]!r}")
    check(subs[3] == "full audit of the phone link",
          f"leading '(his order):' stripped: {subs[3]!r}")
    check(titles[4] == "Tech news TikTok first video ready",
          f"no shout -> first clause: {titles[4]!r}")
    check(all(len(t) <= 37 and len(s) <= 71 for t, s in rows), "titles/subs clipped")
    days = ui_qt.desk_days(SELF)
    check(days == [("Thu 22 Oct", "A late-night run of Claude credits and a pay sheet"),
                   ("Wed 21 Oct", "OpenAI dispute, RAM cleanup")],
          f"recent days: {days}")
    check(ui_qt.desk_open("", n=4) == (0, []) and ui_qt.desk_days("") == [],
          "empty files give an empty desk, no error")


def test_window():
    print("window")
    w = ui_qt.GoatWindow()
    w.resize(1600, 900)
    w.show()
    QApplication.processEvents()
    w.show_page("home")
    QApplication.processEvents()
    for key in ("open", "days", "ask"):
        check(w.desk_cols[key].count() >= 1, f"desk column '{key}' has rows")
    check(w.desk_cols["ask"].count() == 4, "Ask me has its four rows")
    # v8 shell (mockup A): the rail's small presence is the logo mark, on every page.
    check(w.presence_mini.isVisible(), "Home: rail's mark (small presence) shown")
    w.show_page("skills")
    check(w.presence_mini.isVisibleTo(w), "other pages: rail's mark still shown")
    w.show_page("home")

    # Search swaps the desk for results, clearing gives it back.
    w.search.setText("zzqq-no-such-thing")
    w._refresh_recent()
    check(w.recent_card.isVisibleTo(w) and not w.desk.isVisibleTo(w),
          "searching: results replace the desk")
    w.search.clear()
    w._refresh_recent()
    check(w.desk.isVisibleTo(w) and not w.recent_card.isVisibleTo(w),
          "search cleared: desk is back")

    w.hud_tick(0.9, "listening", True)
    check(w.home_presence.state == "listening", "hud_tick drives the home presence")

    w._on_event("brains", "gemini-3.1-flash-lite|Opus 5.5|")
    txt = w.brains_line.text()
    check("voice" in txt and "Claude" in txt and "Opus 5.5" in txt and "free" in txt,
          "brains line names both brains and Claude's state")
    w._on_event("plan", "61|20|23:00")
    w._refresh_ambient()
    amb = w.ambient.text()
    check("RAM" in amb and "Claude 61% of 5-hour" in amb, f"ambient line: {amb!r}")
    w._on_event("claude", "out|17:00")
    w._refresh_ambient()
    check("Claude out, back 17:00" in w.ambient.text(), "ambient says when Claude is out")
    check("out" in w.brains_line.text(), "brains line says Claude is out")

    rows_before = w.desk_cols["open"].count()
    w._refresh_desk()
    check(w.desk_cols["open"].count() == rows_before, "desk refresh is idempotent")
    check(w.hello.text().endswith("Sir.") and "\u00b7" in w.hello_sub.text(),
          f"greeting + date line: {w.hello.text()!r} / {w.hello_sub.text()!r}")
    w.close()


if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    test_parsers()
    test_window()
    print("PASS" if not FAILS else f"FAIL ({len(FAILS)}): " + "; ".join(FAILS))
    sys.stdout.flush()
    os._exit(1 if FAILS else 0)
