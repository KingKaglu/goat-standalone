"""GOAT's sense of its own body: what state its parts are in right now.

His ask, 2026-10-20: "can we give the GOAT the consciousness". Real
experience can't be coded; what can is the outside of it — GOAT knowing its
own state instead of finding out from a log when something already broke.
This is layer 2 of that (layer 1 = workspace/self.md, layer 3 = episodes.py).

Cost rule (his question: "will this affect Claude's usage or speed?"): pure
local reads, no Claude call. The brain hears its body ONLY when something
changed since its last turn (a few words), plus one full line on a fresh
session or when he asks how it is. A note riding every turn would grow the
resumed history by the same block each turn for nothing.
"""
import datetime
import json
import os
import re
import time

import psutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLAN_FILE = os.path.join(ROOT, ".plan_usage.json")
RESTART_LOG = os.path.join(HERE, "restart-goat.log")

# "how are you", "you ok", "status", "are you working", "how do you feel"
ASKED_RE = re.compile(
    r"\b(how\s+(?:are|r)\s+(?:you|u)|you\s+ok(?:ay)?|are\s+you\s+(?:ok|okay|"
    r"alright|working|alive|there)|how\s+do\s+you\s+feel|how(?:'s| is)\s+"
    r"(?:your|ur)\s+(?:body|health|system)|status\s+(?:check|report)|"
    r"self[- ]check|conscious)\b", re.I)


def _band(pct) -> str | None:
    """Usage in coarse bands so a 1% tick never counts as a change."""
    if pct is None:
        return None
    p = float(pct)
    if p >= 95:
        return "nearly out"
    if p >= 80:
        return "high"
    if p >= 50:
        return "over half used"
    return None              # healthy — not worth a word


def _plan() -> dict:
    try:
        with open(PLAN_FILE, encoding="utf-8") as f:
            d = json.load(f)
        at = datetime.datetime.fromisoformat(d.get("at", ""))
        if (datetime.datetime.now() - at).total_seconds() > 30 * 60:
            return {}        # stale meter says nothing true about now
        return d
    except (OSError, ValueError, TypeError):
        return {}


def _rollback_today() -> str | None:
    """Last watchdog rollback in the past 12 hours, as 'HH:MM'."""
    try:
        with open(RESTART_LOG, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-200:]
    except OSError:
        return None
    cut = datetime.datetime.now() - datetime.timedelta(hours=12)
    hit = None
    for ln in tail:
        if "rollback" not in ln.lower() and "rolled back" not in ln.lower():
            continue
        m = re.search(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", ln)
        if not m:
            continue
        try:
            t = datetime.datetime.fromisoformat(m.group(1))
        except ValueError:
            continue
        if t >= cut:
            hit = t.strftime("%H:%M")
    return hit


class BodySense:
    def __init__(self, app):
        self.app = app
        self._last: dict[str, str] = {}
        self._born = time.time()

    def state(self) -> dict[str, str]:
        """key -> short phrase, only for parts worth a word. Never raises."""
        a, s = self.app, {}
        try:
            ph = getattr(a, "phone", None)
            if ph is not None and getattr(ph, "clients", None):
                s["phone"] = ("phone linked, its mic is my ear"
                              if getattr(a, "phone_live", False)
                              else "phone linked")
            else:
                s["phone"] = "phone not connected"
        except Exception:  # noqa: BLE001
            pass
        if getattr(a, "mic_muted", False):
            s["mic"] = "laptop mic muted"
        hit = getattr(a, "_limit_hit_at", 0.0) or 0.0
        reset_at = getattr(a, "claude_reset_at", 0.0) or 0.0
        if hit and (not reset_at or time.time() < reset_at):
            reset = getattr(a, "claude_reset", "") or "?"
            s["limit"] = f"hit Claude's usage limit, resets {reset}"
        p = _plan()
        b5, b7 = _band(p.get("five_hour")), _band(p.get("seven_day"))
        if b5:
            s["quota5"] = (f"5-hour quota {b5} ({round(p['five_hour'])}%, "
                           f"resets {p.get('five_hour_resets') or '?'})")
        if b7:
            s["quota7"] = (f"weekly quota {b7} ({round(p['seven_day'])}%, "
                           f"resets {p.get('seven_day_resets') or '?'})")
        rb = _rollback_today()
        if rb:
            s["rollback"] = f"watchdog rolled back a bad build of me at {rb}"
        try:
            rss = psutil.Process().memory_info().rss / 2**30
            if rss >= 1.5:
                s["ram"] = f"I'm using {rss:.1f} GB of RAM"
        except Exception:  # noqa: BLE001
            pass
        return s

    def _uptime(self) -> str:
        try:
            up = time.time() - psutil.Process().create_time()
        except Exception:  # noqa: BLE001
            up = time.time() - self._born
        h, m = int(up // 3600), int(up % 3600 // 60)
        return f"up {h}h {m}m" if h else f"up {m}m"

    def note(self, full: bool = False) -> str:
        """'' when nothing changed. full=True: the whole picture, once."""
        try:
            now = self.state()
        except Exception as e:  # noqa: BLE001 — a numb body never costs a turn
            return f"[body: self-sense unavailable: {e}]"
        prev, self._last = self._last, now
        if full:
            parts = [self._uptime()] + list(now.values())
            return ("[body — how you are right now (yours, not his): "
                    + "; ".join(parts) + "]")
        changed = [v for k, v in now.items() if prev.get(k) != v]
        gone = [k for k in prev if k not in now]
        words = {"limit": "usage limit cleared", "mic": "laptop mic back on",
                 "rollback": None, "ram": "my RAM use back to normal",
                 "quota5": "5-hour quota healthy again",
                 "quota7": "weekly quota healthy again"}
        changed += [w for k in gone if (w := words.get(k))]
        if not changed:
            return ""
        return ("[body — changed since your last turn (mention only if it "
                "matters to what he's doing): " + "; ".join(changed) + "]")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    class _Fake:
        phone = None
        mic_muted = False
    b = BodySense(_Fake())
    print(b.note(full=True))
    print(repr(b.note()))
    _Fake.mic_muted = True
    print(b.note())
