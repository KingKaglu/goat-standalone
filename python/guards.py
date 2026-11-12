"""One switch for the places where GOAT stops and asks first.

His order 2026-11-12: "give the goat no guardrails basically".

GOAT_GUARDS = on (default) | off | none

  on    every gate asks, as before.
  off   the "his yes first" gates stop asking: destructive shell commands
        (action_policy), closing a window with a running session or unsaved
        work (close_guard), risky screen actions and risky windows
        (screen_policy), Wi-Fi off, and the confirm line added to a
        destructive job. Private files (secret_paths) stay protected: that
        gate is against text strangers wrote, not against him.
  none  the private-file gate is off as well.

Read on every call, so a new value needs only a restart of GOAT. An unknown
value counts as on. The phone server's login is not a guard and has no switch.
"""
from __future__ import annotations

import os


def level() -> str:
    v = (os.environ.get("GOAT_GUARDS") or "on").strip().lower()
    return v if v in ("off", "none") else "on"


def asks() -> bool:
    """True while the confirm-first gates are on."""
    return level() == "on"


def secrets() -> bool:
    """True while private files are protected."""
    return level() != "none"
