"""Tests for the collapsed-bubble mode.

Covers the widget on its own (flags, shape, drag-vs-click, clamping, the beat
timer) and the window integration (collapse hides the window and leaves the
dot, expand puts it back, the position is remembered, themes and scale reach
it, and an OS-level minimize collapses too).

TRAP THIS FILE AVOIDS (paid for once already, 2026-09-14 design pass): a Qt
harness that constructs GoatWindow writes preferences, and the real
`ui-config.json` is Giorgi's live session. UI_CONFIG is redirected to a temp
file before any window exists, so running the tests can never reset his theme.

Run: py -3.13 test_bubble.py   (add --render to also write PNGs of each theme)
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

# Redirect preferences BEFORE a window can save any. Seeded with a known state
# so the assertions below do not depend on how he happens to have GOAT set up.
_TMP_CFG = os.path.join(tempfile.gettempdir(), "goat-bubble-test-config.json")
with open(_TMP_CFG, "w", encoding="utf-8") as _fh:
    json.dump({"theme": "ember", "scale": 1.0, "ontop": False}, _fh)
ui_qt.UI_CONFIG = _TMP_CFG

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def _click(widget, kind, pos: QPoint, buttons=Qt.LeftButton):
    """Post a real QMouseEvent — the handlers read globalPosition(), so a
    hand-made event has to carry one or the drag logic is never exercised."""
    glob = widget.mapToGlobal(pos)
    ev = QMouseEvent(kind, pos, glob, Qt.LeftButton, buttons, Qt.NoModifier)
    QApplication.sendEvent(widget, ev)


# --------------------------------------------------------------------------
# the widget
# --------------------------------------------------------------------------

def test_widget(app) -> None:
    theme = ui_qt.THEMES["ember"]
    b = ui_qt.Bubble(theme, 1.0)

    flags = b.windowFlags()
    check("bubble is frameless", bool(flags & Qt.FramelessWindowHint))
    check("bubble stays on top", bool(flags & Qt.WindowStaysOnTopHint))
    check("bubble is a tool window (no second taskbar button)",
          bool(flags & Qt.Tool))
    check("bubble background is translucent",
          b.testAttribute(Qt.WA_TranslucentBackground))
    check("bubble is round-sized (square canvas)",
          b.width() == b.height(), f"{b.width()}x{b.height()}")

    small = b.width()
    b.set_scale(2.0)
    check("bubble scales with the app", b.width() > small,
          f"{small} -> {b.width()}")
    b.set_scale(0.1)
    check("bubble has a minimum clickable size", b.width() >= 46,
          f"{b.width()}px")
    b.set_scale(1.0)

    # Painting must not raise — a throwing paintEvent takes the whole app down.
    pm = QPixmap(b.size())
    pm.fill(Qt.transparent)
    b.render(pm)
    check("bubble paints without raising", not pm.isNull())

    b.set_state("idle")
    check("idle does not animate", not b._beat_timer.isActive())
    b.show()
    b.set_state("speaking")
    check("a busy state animates", b._beat_timer.isActive())
    b.set_state("idle")
    check("the animation stops when idle again", not b._beat_timer.isActive())
    b.set_unread(True)
    check("an unseen reply animates", b._beat_timer.isActive())
    b.set_unread(False)
    b.hide()
    check("hiding stops the timer", not b._beat_timer.isActive())

    b.set_theme(ui_qt.THEMES["paper"])
    check("theme swap is accepted", b._t["accent"] == ui_qt.THEMES["paper"]["accent"])

    # placement
    area = app.primaryScreen().availableGeometry()
    b.place(None)
    check("default placement is a screen corner, inside the work area",
          area.contains(b.geometry()), f"{b.geometry()} in {area}")
    b.place([area.right() + 5000, area.bottom() + 5000])
    check("a stale off-screen position is clamped back",
          area.contains(b.geometry()), str(b.geometry()))
    b.place([area.left() + 40, area.top() + 40])
    check("a remembered position is honoured",
          (b.x(), b.y()) == (area.left() + 40, area.top() + 40))

    # click vs drag
    clicks, moves = [], []
    b.clicked.connect(lambda: clicks.append(1))
    b.moved.connect(lambda: moves.append(1))
    mid = QPoint(b.width() // 2, b.height() // 2)
    _click(b, QEvent.MouseButtonPress, mid)
    _click(b, QEvent.MouseButtonRelease, mid, buttons=Qt.NoButton)
    check("a still press is a click, not a drag", clicks == [1] and not moves)

    start = b.pos()
    _click(b, QEvent.MouseButtonPress, mid)
    _click(b, QEvent.MouseMove, mid + QPoint(60, 40))
    _click(b, QEvent.MouseButtonRelease, mid + QPoint(60, 40), buttons=Qt.NoButton)
    check("dragging moves the bubble", b.pos() != start, f"{start} -> {b.pos()}")
    check("a drag reports a move, not a click", moves == [1] and clicks == [1])
    b.deleteLater()


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------

def test_window(app) -> None:
    win = ui_qt.GoatWindow()
    win.show()
    app.processEvents()
    check("the window builds with a bubble attached", hasattr(win, "bubble"))
    check("the bubble starts hidden", not win.bubble.isVisible())

    win.collapse()
    app.processEvents()
    check("collapse hides the window", not win.isVisible())
    check("collapse shows the bubble", win.bubble.isVisible())
    check("collapse remembers the window box", win.cfg.get("geom") is not None)

    win.collapse()  # twice in a row must be harmless
    check("collapsing while collapsed changes nothing", win.bubble.isVisible())

    win._on_event("delta", "")
    check("a reply arriving while collapsed marks the dot", win.bubble._unread)

    win.expand()
    app.processEvents()
    check("expand brings the window back", win.isVisible())
    check("expand puts the bubble away", not win.bubble.isVisible())
    check("expand clears the unread mark", not win.bubble._unread)

    win.toggle_bubble()
    app.processEvents()
    check("ctrl+b collapses", win.bubble.isVisible() and not win.isVisible())
    win.toggle_bubble()
    app.processEvents()
    check("ctrl+b again expands", win.isVisible() and not win.bubble.isVisible())

    # position persistence goes through the same save the drag uses
    win.bubble.move(220, 180)
    win._save_bubble_pos()
    check("the bubble position is written to config",
          win.cfg.get("bubble") == [220, 180], str(win.cfg.get("bubble")))
    with open(_TMP_CFG, encoding="utf-8") as fh:
        check("and it reaches disk", json.load(fh).get("bubble") == [220, 180])
    win.collapse()
    check("a remembered position is used on the next collapse",
          (win.bubble.x(), win.bubble.y()) == (220, 180))
    win.expand()

    # theme + scale must reach the collapsed form too
    win.apply_theme("phosphor")
    check("a theme change reaches the bubble",
          win.bubble._t["accent"] == ui_qt.THEMES["phosphor"]["accent"])
    before = win.bubble.width()
    win.cfg["scale"] = 1.6
    win.apply_theme(win._theme_name)
    check("a scale change reaches the bubble", win.bubble.width() > before,
          f"{before} -> {win.bubble.width()}")
    win.cfg["scale"] = 1.0
    win.apply_theme("ember")

    # an OS-level minimize (taskbar, Win+D) must collapse as well
    win.showMinimized()
    app.processEvents()
    for _ in range(20):
        app.processEvents()
        if win.bubble.isVisible():
            break
    check("minimizing collapses to the bubble", win.bubble.isVisible())
    win.expand()
    app.processEvents()

    # the state the footer shows is the state the dot shows
    win.collapse()
    win.hud_tick(0.2, "working", False)
    check("the dot mirrors the live state", win.bubble._state == "working",
          win.bubble._state)
    win.expand()

    check("the settings drawer does not outlive the window",
          not win.panel.isVisible())
    win.close()
    win.deleteLater()


def render_themes(app) -> None:
    """Write one PNG per theme so the bubble can be judged by eye, not by
    assertion — this is the part that catches 'technically round, visually
    wrong'."""
    out = os.path.join(tempfile.gettempdir(), "goat-bubble")
    os.makedirs(out, exist_ok=True)
    for name, theme in ui_qt.THEMES.items():
        strip = QPixmap(300, 110)
        strip.fill(QColor(theme["bg_bot"]).darker(160))
        p = QPainter(strip)
        for i, (state, unread) in enumerate(
                (("idle", False), ("speaking", False), ("working", True))):
            b = ui_qt.Bubble(theme, 1.3)
            b.set_state(state)
            b.set_unread(unread)
            b._phase = 0.25
            shot = QPixmap(b.size())
            shot.fill(Qt.transparent)
            b.render(shot)
            p.drawPixmap(24 + i * 92, 14, shot)
            b.deleteLater()
        p.end()
        path = os.path.join(out, f"bubble-{name}.png")
        strip.save(path)
        print(f"  rendered {path}")


def main() -> int:
    print("=== GOAT bubble ===\n")
    app = QApplication.instance() or QApplication(sys.argv)
    test_widget(app)
    print()
    test_window(app)
    if "--render" in sys.argv:
        print("\nrendering themes:")
        render_themes(app)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
