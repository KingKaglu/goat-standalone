"""Offscreen check: status lines must survive the 33ms hud_tick stomp.

Run:  cd C:/Users/user/goat-standalone/python && python test_statusword.py
"""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import sys
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

from ui_qt import GoatWindow  # noqa: E402

win = GoatWindow()

# Boot guard still works: "booting" is never stomped by the ticker.
win.hud_tick(0.0, "idle", True)
assert win.stateword.text() == "booting", win.stateword.text()
print("[PASS] booting survives ticks")

# A status line holds against the very next tick (the old bug: one 33ms
# tick wiped every status before a human could see it).
win._on_event("status", "calibrating — stay quiet for 2 seconds")
win.hud_tick(0.0, "idle", True)
assert win.stateword.text() == "calibrating — stay quiet for 2 seconds"
print("[PASS] status survives the next tick")

# After the hold expires, the audio state reclaims the word.
win._status_hold = 0.0
win.hud_tick(0.0, "listening", True)
assert win.stateword.text() == "listening", win.stateword.text()
print("[PASS] ticker reclaims after hold expires")

# The out-of-usage word gets the long hold.
win._on_event("limit", "Giorgi, we're out of Claude usage.")
win.hud_tick(0.0, "idle", True)
assert win.stateword.text() == "out of usage"
print("[PASS] limit word holds")

print("\nall statusword checks passed")
sys.stdout.flush()
os._exit(0)  # skip Qt teardown crashes in offscreen mode
