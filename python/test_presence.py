"""Tests for the v8 presence (ring + string, design.md 2026-10-24).

What it proves:
  - at rest it does not repaint (asleep: never; idle: only the 8 s breath),
  - live states repaint every frame,
  - blends land on time (150 ms listening, 300 ms speaking, 400 ms asleep),
  - Claude's light fades in with a work turn and keeps frames running,
  - error flinches once, then settles,
  - every state paints without error, compact and full, in every theme,
  - the window wires both presences (rail + chat) and the boot ritual.

Time is a fake clock (ui_qt.time is swapped for a proxy), so the timings are
exact and the test takes about a second. UI_CONFIG goes to a temp file first:
a GoatWindow writes preferences, and the real ui-config.json is his session.

Run: py -3.13 test_presence.py   (--render writes the state strip PNG)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time as _real_time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import ui_qt  # noqa: E402

_TMP_CFG = os.path.join(tempfile.gettempdir(), "goat-presence-test-config.json")
with open(_TMP_CFG, "w", encoding="utf-8") as _fh:
    json.dump({"theme": "midnight", "scale": 1.0, "ontop": False}, _fh)
ui_qt.UI_CONFIG = _TMP_CFG

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QRegion  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402


class FakeTime:
    """time module stand-in: monotonic() is ours, the rest is real."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(_real_time, name)


CLOCK = FakeTime()
FAILS: list = []


def check(cond: bool, what: str):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def run(pr, secs: float, level: float, state: str, work: bool = False) -> int:
    """Tick at the window's 33 ms for secs; return how many paints it asked."""
    n = 0
    for _ in range(int(round(secs / 0.033))):
        CLOCK.now += 0.033
        n += bool(pr.tick(level, state, work))
    return n


def test_clock():
    print("clock")
    pr = ui_qt.Presence()
    pr.resize(600, 94)
    run(pr, 1.0, 0.0, "booting")
    check(run(pr, 3.0, 0.0, "booting") == 0, "asleep at rest: zero paints")
    run(pr, 0.6, 0.0, "idle")          # blend into idle (400 ms)
    check(pr._v["idle"] == 1.0 and pr._v["asleep"] == 0.0, "idle blend settles")
    n = run(pr, 4.5, 0.1, "idle")      # breath starts 5.6 s into idle
    check(n == 0, f"idle before the breath: zero paints (got {n})")
    n = run(pr, 8.0 * 3, 0.1, "idle")
    per_cycle = n / 3
    check(10 <= per_cycle <= 42,
          f"idle breath: {per_cycle:.0f} paints per 8 s (<=42 = 15 fps x 2.4 s)")
    n = run(pr, 1.0, 0.8, "speaking")
    check(n >= 29, f"speaking repaints every frame ({n}/30)")
    n = run(pr, 1.0, 0.9, "listening")
    check(n >= 29, f"listening repaints every frame ({n}/30)")


def test_blend_timing():
    print("blend timing")
    for state, dur in (("listening", 0.15), ("speaking", 0.3), ("asleep", 0.4)):
        pr = ui_qt.Presence()
        pr.tick(0, "idle")
        run(pr, 1.0, 0.0, "idle")
        start = "asleep" if state != "asleep" else "idle"
        run(pr, 1.0, 0.0, start if start != "asleep" else "booting")
        run(pr, dur * 0.4, 0.5, state)
        mid = pr._v[state]
        run(pr, dur * 0.8, 0.5, state)
        end = pr._v[state]
        check(0.2 < mid < 0.9 and end >= 0.94,
              f"{state}: eases in ({mid:.2f} at 40%, {end:.2f} at {dur * 1.2:.2f} s)")


def test_work_and_error():
    print("work + error")
    pr = ui_qt.Presence()
    run(pr, 1.0, 0.0, "idle")
    run(pr, 8.0, 0.0, "idle")
    run(pr, 0.4, 0.2, "working", work=True)   # the timer's "working" = voice idle
    check(pr.state == "idle" and pr._v["work"] >= 0.94,
          f"Claude's light fades in with the work turn ({pr._v['work']:.2f})")
    n = run(pr, 1.0, 0.2, "working", work=True)
    check(n >= 29, "Claude working keeps frames running")
    run(pr, 0.6, 0.0, "idle", work=False)
    check(pr._v["work"] == 0.0, "Claude's light leaves when the turn ends")
    run(pr, 0.05, 0.0, "error")
    check(bool(pr._flinch_t0), "error starts a flinch")
    run(pr, 1.0, 0.0, "error")
    n = run(pr, 2.0, 0.0, "error")
    check(n == 0, f"error settles: no repaint once still ({n})")


def render(path: str | None):
    """Paint every state, full and compact, in every theme."""
    print("render")
    states = [("asleep", 0.0, False), ("idle", 0.0, False),
              ("listening", 0.8, False), ("thinking", 0.25, False),
              ("speaking", 0.7, False), ("idle", 0.2, True),
              ("error", 0.0, False)]
    cw, ch, sh = 228, 160, 34
    ok = True
    for theme in ui_qt.THEMES:
        t = ui_qt.THEMES[theme]
        img = QImage(cw * len(states), ch, QImage.Format_ARGB32)
        img.fill(QColor(t["bg_top"]))
        p = QPainter(img)
        for i, (st, lv, wk) in enumerate(states):
            for compact, top, hgt in ((False, 6, 94), (True, 104, sh)):
                pr = ui_qt.Presence(compact=compact)
                pr.set_theme(t)
                pr.resize(cw - 8, hgt)
                run(pr, 1.0, lv, st, wk)
                pr._t = 1.3 + i * 0.4
                try:
                    pr.render(p, ui_qt.QPoint(i * cw + 4, top), QRegion(),
                              QWidget.RenderFlag.DrawChildren)
                except Exception as e:  # noqa: BLE001
                    ok = False
                    print("   ", theme, st, compact, e)
            p.setPen(QColor(t["dim"]))
            p.setFont(QFont("Consolas", 9))
            label = "working" if wk else st
            p.drawText(QRectF(i * cw, ch - 20, cw, 16), Qt.AlignHCenter, label.upper())
        p.end()
        if path and theme == "midnight":
            img.save(path)
        if path and theme in ("paper", "jarvis"):
            img.save(path.replace(".png", f"-{theme}.png"))
    check(ok, "every state paints in every theme, full + compact")


def test_window():
    print("window")
    w = ui_qt.GoatWindow()
    w.resize(1400, 860)
    w.show()
    QApplication.processEvents()
    check(isinstance(w.presence, ui_qt.Presence) and w.presence.compact is False,
          "chat page carries the full presence")
    check(isinstance(w.presence_mini, ui_qt.Presence) and w.presence_mini.compact,
          "rail card carries the compact presence")
    check(not hasattr(ui_qt, "StringLine"), "StringLine is gone")
    w.show_page("chat")
    w.hud_tick(0.9, "listening", True)
    check(w.presence.state == "listening" and w.presence_mini.state == "listening",
          "hud_tick drives both presences")
    w._on_event("status", "engine crashed — check python\\goat-app.log")
    w._status_hold = 0
    w.hud_tick(0.0, "idle", True)
    check(w.presence.state == "error", "a crash word turns the presence to error")
    w.close()


if __name__ == "__main__":
    app = QApplication.instance() or QApplication(sys.argv)
    ui_qt.time = CLOCK
    try:
        test_clock()
        test_blend_timing()
        test_work_and_error()
        out = None
        if "--render" in sys.argv:
            out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                               "workspace", "goat-redesign", "after-b",
                               "presence-states.png")
            os.makedirs(os.path.dirname(out), exist_ok=True)
        render(out)
    finally:
        ui_qt.time = _real_time
    test_window()
    print("PASS" if not FAILS else f"FAIL ({len(FAILS)}): " + "; ".join(FAILS))
    sys.stdout.flush()
    os._exit(1 if FAILS else 0)
