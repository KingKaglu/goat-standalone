"""Offscreen simulation of the follow-mode autoscroll in GoatWindow.

Run:  cd C:/Users/user/goat-standalone/python && python test_scroll.py
"""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import sys
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

from ui_qt import GoatWindow  # noqa: E402 — needs QApplication first

win = GoatWindow()
win.resize(800, 400)
win.show()


def pump():
    for _ in range(5):
        app.processEvents()


def fill(n, text="line of conversation text that wraps and takes space"):
    for i in range(n):
        win._add_line(f"{i} {text}", "replyOld")
    pump()
    win._scroll_down()  # the singleShot(30) hasn't fired; call directly
    pump()


sb = win.scroll.verticalScrollBar()

# 1. Default: following — new content pins to the bottom.
fill(40)
assert sb.maximum() > 0, "page never became scrollable"
assert sb.value() == sb.maximum(), f"not pinned: {sb.value()}/{sb.maximum()}"
print(f"[PASS] following pins to bottom ({sb.value()}/{sb.maximum()})")

# 2. He scrolls up to read — new content must NOT yank him down.
sb.setValue(0)
pump()
assert win._follow is False, "_follow should disengage when scrolled up"
before = sb.value()
fill(10)
assert sb.value() <= before + 5, f"page got yanked: {before} -> {sb.value()}"
print(f"[PASS] scrolled-up page stays put ({before} -> {sb.value()})")

# 3. He scrolls back to the bottom — following re-engages.
sb.setValue(sb.maximum())
pump()
assert win._follow is True, "_follow should re-engage at the bottom"
fill(5)
assert sb.value() == sb.maximum(), "should be pinned again"
print("[PASS] returning to bottom re-engages following")

# 4. A new message from him re-engages following even from mid-history.
sb.setValue(0)
pump()
assert win._follow is False
win._on_event("you", "okay what about this")
pump()
win._scroll_down()
pump()
assert win._follow is True, "'you' event should re-engage following"
assert sb.value() == sb.maximum(), "his new message should bring him down"
print("[PASS] speaking re-engages following")

print("\nall scroll simulations passed")
sys.stdout.flush()  # os._exit skips buffered-stdout flush
os._exit(0)  # skip Qt teardown crashes in offscreen mode
