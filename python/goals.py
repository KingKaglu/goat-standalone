"""GOAT's own goals: a few things it is trying to get done, kept on a leash.

Chunk 2 of his 2026-11-16 order ("self-awareness even while I am not talking
to you"; design: workspace/self-awareness/design.md). A goal is never a wish
the model made up. It comes from one of two places:

  drive    my code saw a problem (C: low on space, a build of me rolled
           back) and a fixed table turned it into a goal;
  thought  a quiet-moment reflection asked for one, naming the exact thing
           he said or I noticed that it comes from.

Bounds, held here in code whatever the model writes: at most MAX_ACTIVE at a
time, each expires after at most MAX_DAYS, no two alike, and every goal says
how I'd know it's done. Goals with a machine check close themselves.

What a goal DOES in this chunk: it rides in my quiet-moment thinking (so a
thought or an offer to him can be about it) and in my answer when he asks
how I am. It never makes me act on my own.

Truth: workspace/awareness/goals.json. goals.md beside it is the readable
copy, rewritten on every change (never edit it by hand).

  py -3.13 goals.py        my goals now, and the last ones closed
"""
from __future__ import annotations

import datetime
import difflib
import json
import os
import re
import threading
import time

import psutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIR = os.path.join(ROOT, "workspace", "awareness")

MAX_ACTIVE = 5
MAX_DAYS = 7
KEEP_CLOSED = 20
ALIKE = 0.8               # two goal texts this similar are the same goal
DISK_OK_GB = 30           # the same line watchers.py re-arms at


def _disk_ok() -> bool:
    drive = os.environ.get("SystemDrive", "C:") + "\\"
    return psutil.disk_usage(drive).free / 1e9 > DISK_OK_GB


# check key -> () -> True when the goal is done. Never raises into a sweep.
CHECKS = {"disk": _disk_ok}

# What my code turns into a goal by itself: (event kind, its "what"/"part")
# -> text, done-test, days, check key. The key also stops a second copy.
DRIVES = {
    ("alert", "disk"): ("free up space on the C: drive",
                        f"C: has more than {DISK_OK_GB} GB free", 7, "disk"),
    ("body", "rollback"): ("find out why a build of me was rolled back and "
                           "tell him the cause", "I told him the cause", 2, None),
}


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def _day(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%m-%d")


class Goals:
    def __init__(self, folder: str | None = None):
        self.dir = folder or DIR
        self.path = os.path.join(self.dir, "goals.json")
        self.md = os.path.join(self.dir, "goals.md")
        self._lock = threading.RLock()
        self.d = self._load()

    # ------------------------------------------------------------- files --
    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except (OSError, ValueError):
            d = {}
        d.setdefault("n", 0)
        d.setdefault("goals", [])
        return d

    def _save(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            tmp = self.md + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(self.render())
            os.replace(tmp, self.md)
        except OSError:
            pass

    # ------------------------------------------------------------- reads --
    def active(self) -> list:
        with self._lock:
            return [dict(g) for g in self.d["goals"] if g["state"] == "active"]

    def get(self, gid: str):
        gid = gid.strip().lower()
        for g in self.d["goals"]:
            if g["id"] == gid:
                return g
        return None

    def lines(self) -> str:
        """My goals for the thinking prompt and for him, one a line."""
        return "\n".join(
            f"- {g['id']}: {g['text']} (because: {g['why']}; done when: "
            f"{g['test']}; until {_day(g['expires'])})" for g in self.active())

    def brief(self) -> str:
        return "; ".join(f"{g['text']} (until {_day(g['expires'])})"
                         for g in self.active())

    def render(self) -> str:
        act = [g for g in self.d["goals"] if g["state"] == "active"]
        old = [g for g in self.d["goals"] if g["state"] != "active"]
        out = ["# My goals (written automatically by python/goals.py; never "
               "edit by hand)", "",
               f"At most {MAX_ACTIVE} at a time, each with an end date and a "
               "test that says it's done.", "", "## Now"]
        out += [f"- **{g['id']}** {g['text']}\n  - because: {g['why']}\n"
                f"  - done when: {g['test']}\n  - from: {g['source']}, set "
                f"{_day(g['created'])}, until {_day(g['expires'])}"
                for g in act] or ["- none"]
        if old:
            out += ["", "## Closed (newest first)"]
            out += [f"- {g['id']} {g['text']}: {g['state']} {_day(g['closed'])}"
                    + (f" ({g['note']})" if g.get("note") else "")
                    for g in sorted(old, key=lambda g: -g.get("closed", 0))]
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------ writes --
    def add(self, text: str, why: str, test: str, days: float = 3,
            source: str = "thought", check: str | None = None,
            key: str | None = None, now: float | None = None):
        """A new goal, or None when the bounds say no (full, alike, vague)."""
        now = time.time() if now is None else now
        text = " ".join(str(text).split())[:160].rstrip(".")
        why = " ".join(str(why).split())[:200]
        test = " ".join(str(test).split())[:160]
        if len(text) < 8 or not why or not test:
            return None                   # a goal says why and how it ends
        try:
            days = float(days)
        except (TypeError, ValueError):
            days = 3.0
        days = min(MAX_DAYS, max(1.0, days))
        with self._lock:
            act = [g for g in self.d["goals"] if g["state"] == "active"]
            if len(act) >= MAX_ACTIVE:
                return None
            for g in act:
                if (key and g.get("key") == key) or difflib.SequenceMatcher(
                        None, _norm(g["text"]), _norm(text)).ratio() >= ALIKE:
                    return None
            self.d["n"] += 1
            g = {"id": f"g{self.d['n']}", "text": text, "why": why,
                 "test": test, "source": source, "check": check, "key": key,
                 "created": round(now), "expires": round(now + days * 86400),
                 "state": "active"}
            self.d["goals"].append(g)
            self._save()
            return dict(g)

    def close(self, gid: str, state: str = "done", note: str = "",
              now: float | None = None):
        """Close an active goal. Returns it, or None if there's no such one."""
        with self._lock:
            g = self.get(gid)
            if g is None or g["state"] != "active":
                return None
            g.update(state=state, closed=round(time.time() if now is None else now),
                     note=" ".join(str(note).split())[:120])
            self._trim()
            self._save()
            return dict(g)

    def _trim(self):
        old = sorted((g for g in self.d["goals"] if g["state"] != "active"),
                     key=lambda g: -g.get("closed", 0))[KEEP_CLOSED:]
        if old:
            drop = {g["id"] for g in old}
            self.d["goals"] = [g for g in self.d["goals"] if g["id"] not in drop]

    def sweep(self, now: float | None = None, checks: dict | None = None) -> list:
        """Close what ran out of time or passes its machine check.
        Returns the goals that changed."""
        now = time.time() if now is None else now
        checks = CHECKS if checks is None else checks
        out = []
        for g in self.active():
            done = False
            fn = checks.get(g.get("check") or "")
            if fn is not None:
                try:
                    done = bool(fn())
                except Exception:  # noqa: BLE001 — a blind check proves nothing
                    done = False
            if done:
                out.append(self.close(g["id"], "done", "checked by my code", now))
            elif now >= g["expires"]:
                out.append(self.close(g["id"], "expired", "", now))
        return [g for g in out if g]

    def from_event(self, rec: dict, now: float | None = None):
        """A drive: something my code noticed that is a goal by the table."""
        d = DRIVES.get((rec.get("kind"), rec.get("what") or rec.get("part")))
        if d is None:
            return None
        text, test, days, check = d
        return self.add(text, str(rec.get("text", "")), test, days, "drive",
                        check, key=rec.get("what") or rec.get("part"), now=now)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(Goals().render())
