"""GOAT's awareness: what it notices and knows about itself while he's silent.

His order, 2026-11-16: "self-awareness even while I am not talking to you".
Before this, every sense was sampled on his turns only; six silent hours
left no record. This is the part of the inner life that keeps running:

  Stream      one line per CHANGE (he went quiet, he's back after 2 h, a
              session in an app, my body changing, an alert, my own restart,
              a thought, a line I said first and whether he answered), each
              with a fixed score for how much it matters.
  Self-state  kept current every tick: is he here, what he's doing, are we
              talking, my health, and the pressure = scores added up since
              the last thought.
  Reports     one note on his next turn after a silence, one measured self
              line when he asks how I am, a CLI for the whole day.

No model anywhere in this file, and no senses either: inner_life.py takes the
snapshot (it owns the senses) and drives tick() from its own 30 s loop;
watchers and the reflection post through note(). ready() says when enough
has happened to be worth a thought; inner_life decides whether one may be
spent and calls thought() when it was (design:
workspace/self-awareness/design.md).

The stream keeps app names, never window titles or screen text, and lives in
workspace/awareness/ (not in git).

  py -3.13 awareness.py [hours]    my state + what I noticed (default 24 h)
"""
from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time

import metrics

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIR = os.path.join(ROOT, "workspace", "awareness")

AWAY_S = 5 * 60           # no keys, mouse or talk this long = he went quiet
BACK_S = 60               # activity this recent = he's back
DWELL_MIN_S = 5 * 60      # an app session shorter than this isn't worth a line
SWITCH_TICKS = 2          # a new app must hold this many ticks to end a session
TALK_ON_S = 120           # an exchange this recent = we're talking
TALK_OFF_S = 10 * 60      # this long without one = that talk is over
SLEEP_GAP_S = 5 * 60      # a hole this long between ticks = the laptop slept
HEARTBEAT_S = 5 * 60      # state file rewrite (how I know how long I was off)
ROLLUP_S = 3600           # one "aware" metrics line an hour
SAME_S = 10 * 60          # the same line again inside this is dropped
CPU_HOT = 40.0            # % of one core, while idle ...
CPU_TICKS = 4             # ... for this many ticks
CPU_COOL_S = 30 * 60
THINK_AT = 20.0           # pressure worth a thought ...
THINK_PEAK = 3            # ... if at least one thing in it scored this much
PENDING_MAX = 30          # lines of the stream one thought reads
BOOT_TELL = ("rollback",)  # body parts worth a line even on the first look
NOTE_MIN_SAL = 2          # the turn note skips anything below this
NOTE_MIN_SILENCE_S = 10 * 60
NOTE_MAX = 6
STREAM_MAX = 1_000_000    # bytes; cut back to half at boot

BODY_SAL = {"limit": 5, "rollback": 5, "quota5": 3, "quota7": 3, "ram": 2,
            "mic": 1, "phone": 1}
BODY_GONE = {"limit": "usage limit cleared", "mic": "laptop mic back on",
             "ram": "my RAM use back to normal",
             "quota5": "5-hour quota healthy again",
             "quota7": "weekly quota healthy again"}
QUIET_KINDS = ("gate",)   # never told to the brain


def dur(s: float) -> str:
    s = max(0, int(s))
    if s < 90:
        return f"{s} s"
    m = round(s / 60)
    if m < 90:
        return f"{m} min"
    return f"{m // 60} h {m % 60} m" if m % 60 else f"{m // 60} h"


def _hm(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M")


def _shape(v: str) -> str:
    """A body phrase without its numbers: 83% -> 84% is not a change."""
    return re.sub(r"\d+", "#", v)


class Awareness:
    def __init__(self, folder: str | None = None):
        self.dir = folder or DIR
        self.stream = os.path.join(self.dir, "stream.jsonl")
        self.state_file = os.path.join(self.dir, "state.json")
        self.on_event = None              # called with each new stream line
        self._lock = threading.RLock()
        self.st = self._load()
        self.pressure = float(self.st.get("pressure") or 0.0)
        self.peak = float(self.st.get("peak") or 0.0)   # top score in it
        self.gates = int(self.st.get("gates") or 0)
        self.thought_t = float(self.st.get("thought_t") or time.time())
        # him
        self.him = "here"
        self.him_since = time.time()
        self.app = ""                     # the app he's in
        self.app_since = 0.0
        self._cand, self._cand_n, self._cand_t = "", 0, 0.0
        self.talking = False
        self.talk_since = 0.0
        # me
        self.body: dict | None = None
        self.engine_down = False
        self.up = 0.0
        self._hot = 0
        self._hot_said = 0.0
        # bookkeeping
        self._last_tick: float | None = None
        self._tnow: float | None = None   # the tick's clock, while it runs
        self._beat = 0.0
        self._recent: dict = {}           # (kind, text) -> t, for SAME_S
        self._seen_t = time.time()        # the brain has heard up to here
        self._silence_peak = 0.0          # longest quiet since its last turn
        self._since_gate = 0
        self._roll = {"t": time.time(), "ticks": 0, "ms": 0.0, "max": 0.0,
                      "events": 0}
        self.tick_ms = 0.0                # running average
        self._trim()

    # ------------------------------------------------------------- files --
    def _load(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                st = json.load(f)
            return st if isinstance(st, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, now: float):
        self.st.update(alive=now, pressure=round(self.pressure, 1),
                       peak=self.peak, gates=self.gates,
                       thought_t=round(self.thought_t, 3))
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.st, f)
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def _trim(self):
        """Keep the newer half once the stream passes STREAM_MAX."""
        try:
            if os.path.getsize(self.stream) <= STREAM_MAX:
                return
            with open(self.stream, "rb") as f:
                f.seek(-STREAM_MAX // 2, 2)
                tail = f.read()
            tail = tail[tail.find(b"\n") + 1:]
            tmp = self.stream + ".tmp"
            with open(tmp, "wb") as f:
                f.write(tail)
            os.replace(tmp, self.stream)
        except OSError:
            pass

    def events(self, since: float = 0.0, max_bytes: int = 300_000) -> list:
        """Stream entries newer than `since` (reads only the file's tail)."""
        try:
            with open(self.stream, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                raw = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        out = []
        for ln in raw.splitlines():
            try:
                e = json.loads(ln)
                if float(e["t"]) > since:
                    out.append(e)
            except (ValueError, KeyError, TypeError):
                continue
        return out

    # ------------------------------------------------------------ posting --
    def note(self, kind: str, text: str, sal: float = 1, t: float | None = None,
             **extra) -> bool:
        """Any part of GOAT posts what it noticed. Never raises."""
        try:
            return self._post(kind, text, sal, t, **extra)
        except Exception as e:  # noqa: BLE001 — awareness never hurts GOAT
            print(f"[aware] {type(e).__name__}: {e}")
            return False

    def _post(self, kind, text, sal, t=None, **extra) -> bool:
        text = " ".join(str(text).split())[:240]
        if not text:
            return False
        with self._lock:
            now = self._tnow if self._tnow is not None else time.time()
            key = (kind, text)
            last = self._recent.get(key)
            if last is not None and now - last < SAME_S:
                return False
            if len(self._recent) > 200:
                self._recent = {k: v for k, v in self._recent.items()
                                if now - v < SAME_S}
            self._recent[key] = now
            rec = {"t": round(now if t is None else t, 3), "kind": kind,
                   "text": text, "sal": sal}
            rec.update(extra)
            if t is not None:
                rec["at"] = round(now, 3)     # when I noticed, if later
            try:
                os.makedirs(self.dir, exist_ok=True)
                with open(self.stream, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                return False
            self.pressure += float(sal)
            self.peak = max(self.peak, float(sal))
            self._since_gate += 1
            self._roll["events"] += 1
        hook = self.on_event
        if hook is not None and kind not in QUIET_KINDS:
            try:
                hook(rec)
            except Exception as e:  # noqa: BLE001
                print(f"[aware] on_event: {type(e).__name__}: {e}")
        return True

    # --------------------------------------------------------------- tick --
    def tick(self, s: dict, now: float | None = None) -> None:
        """One look around. `s` is the snapshot inner_life.sense() took:
        idle (s since his last key/mouse), heard (s since his last words),
        quiet (s since our last exchange, my replies included), front (app
        name), busy, engine_down, up (my uptime), boot (the laptop's boot
        time), cpu (my %), and body (dict) on the ticks that sampled it."""
        now = time.time() if now is None else now
        t0 = time.perf_counter()
        with self._lock:
            self._tnow = now
            try:
                self.up = float(s.get("up") or 0.0)
                if self._last_tick is None:
                    self._wake(s, now)
                elif now - self._last_tick > SLEEP_GAP_S:
                    self._post("wake", "the laptop slept from "
                               f"{_hm(self._last_tick)} to {_hm(now)} "
                               f"({dur(now - self._last_tick)})", 2)
                self._last_tick = now
                self._presence(s, now)
                self._activity(s, now)
                self._talk(s, now)
                self._body(s)
                self._health(s, now)
                self._silence_peak = max(self._silence_peak, self._heard(s))
                if now - self._beat >= HEARTBEAT_S:
                    self._beat = now
                    self._save(now)
            finally:
                self._tnow = None
            ms = (time.perf_counter() - t0) * 1000.0
            r = self._roll
            r["ticks"] += 1
            r["ms"] += ms
            r["max"] = max(r["max"], ms)
            self.tick_ms = ms if not self.tick_ms else 0.9 * self.tick_ms + 0.1 * ms
            if now - r["t"] >= ROLLUP_S:
                metrics.emit("aware", ticks=r["ticks"],
                             ms=round(r["ms"] / max(1, r["ticks"]), 3),
                             ms_max=round(r["max"], 2), events=r["events"],
                             pressure=round(self.pressure, 1))
                self._roll = {"t": now, "ticks": 0, "ms": 0.0, "max": 0.0,
                              "events": 0}

    def _wake(self, s, now):
        prev = self.st.get("alive")
        born = now - self.up
        self._beat = now
        if not prev:
            self._post("wake", "my awareness ran for the first time", 1)
        else:
            off = born - float(prev)
            if off > 10 * 60:
                more = (" (the laptop was shut down or restarted meanwhile)"
                        if float(s.get("boot") or 0) > float(prev) else "")
                self._post("wake", f"I woke up at {_hm(born)}; off about "
                           f"{dur(off)}, since {_hm(float(prev))}{more}", 3,
                           t=born, off=round(off))
            else:
                self._post("wake", f"I restarted at {_hm(born)}", 2, t=born)
        self._save(now)

    @staticmethod
    def _heard(s) -> float:
        """Seconds since HIS last words. `quiet` also resets when I finish
        a job, which says nothing about where he is."""
        h = s.get("heard")
        return float(s.get("quiet") or 0.0) if h is None else float(h)

    def _presence(self, s, now):
        idle = float(s.get("idle") or 0.0)
        heard = self._heard(s)
        # GOAT's own clicks reset Windows' idle clock, so while a job runs
        # only his words count, and only to bring him back.
        if s.get("busy"):
            if self.him == "here" or heard >= BACK_S:
                return
            active = heard
        else:
            active = min(idle, heard)
        if self.him == "here" and active >= AWAY_S:
            self.him, self.him_since = "quiet", now - active
            self._close_app(self.him_since)
            self._post("presence", "he went quiet (no keys, mouse or talk)", 1,
                       t=self.him_since)
        elif self.him == "quiet" and active < BACK_S:
            d = now - self.him_since
            self._post("presence", f"he's back after {dur(d)}",
                       3 if d >= 7200 else 2 if d >= 900 else 1, dur=round(d))
            self.him, self.him_since = "here", now

    def _close_app(self, end: float):
        d = end - self.app_since
        if self.app and d >= DWELL_MIN_S:
            self._post("activity", f"he spent {dur(d)} in {self.app}",
                       2 if d >= 1800 else 1, t=end, app=self.app, dur=round(d))
        self.app, self._cand, self._cand_n = "", "", 0

    def _activity(self, s, now):
        f = str(s.get("front") or "")
        if self.him != "here" or not f:
            return
        if not self.app:
            self.app, self.app_since = f, now
        elif f == self.app:
            self._cand, self._cand_n = "", 0
        elif f == self._cand:
            self._cand_n += 1
            if self._cand_n >= SWITCH_TICKS:
                self._close_app(self._cand_t)
                self.app, self.app_since = f, self._cand_t
        else:
            self._cand, self._cand_n, self._cand_t = f, 1, now

    def _talk(self, s, now):
        quiet = float(s.get("quiet") or 0.0)
        if quiet < TALK_ON_S:
            if not self.talking:
                self.talking, self.talk_since = True, now - quiet
        elif self.talking and quiet >= TALK_OFF_S:
            self.talking = False
            end = now - quiet
            self._post("talk", f"we talked ({dur(end - self.talk_since)})", 1,
                       t=end)

    def _body(self, s):
        down = bool(s.get("engine_down"))
        if down != self.engine_down:
            self.engine_down = down
            self._post("body", "my Claude engine can't connect" if down
                       else "my Claude engine is back", 4)
        new = s.get("body")
        if new is None:
            return
        old, self.body = self.body, dict(new)
        if old is None:                   # the first look is the baseline,
            for k in BOOT_TELL:           # but waking up after a rollback
                if k in new:              # is news
                    self._post("body", new[k], BODY_SAL.get(k, 2), part=k)
            return
        for k, v in new.items():
            if k not in old or _shape(old[k]) != _shape(v):
                self._post("body", v, BODY_SAL.get(k, 2), part=k)
        for k in old:
            if k not in new and BODY_GONE.get(k):
                self._post("body", BODY_GONE[k], 2, part=k)

    def _health(self, s, now):
        cpu = float(s.get("cpu") or 0.0)
        idle_me = not s.get("busy") and not self.talking
        self._hot = self._hot + 1 if (idle_me and cpu >= CPU_HOT) else 0
        if self._hot >= CPU_TICKS and now - self._hot_said >= CPU_COOL_S:
            self._hot_said = now
            self._post("health", f"I'm using {cpu:.0f}% of a core while idle", 2)

    # --------------------------------------------------------- think gate --
    def ready(self) -> bool:
        """Enough has happened to be worth a thought: the scores add up to
        THINK_AT and at least one thing in them mattered (THINK_PEAK). A pile
        of small things (he spent 20 min in an app, ten times) keeps waiting
        for something that does."""
        return self.pressure >= THINK_AT and self.peak >= THINK_PEAK

    def pending(self, limit: int = PENDING_MAX) -> list:
        """What I noticed since my last thought, as lines for the next one:
        the highest-scored `limit`, in time order."""
        since = round(self.thought_t, 3)
        evs = [e for e in self.events()
               if e.get("at", e["t"]) >= since
               and e.get("kind") not in QUIET_KINDS]
        top = sorted(evs, key=lambda e: (-e.get("sal", 0), e["t"]))[:limit]
        top.sort(key=lambda e: e["t"])
        return [f"{_hm(e['t'])} {e['text']}" for e in top]

    def thought(self, why: str = "world", dry: bool = False,
                now: float | None = None) -> tuple:
        """A thought was spent on what I noticed (or, dry: would have been).
        Empties the pressure and moves the 'since my last thought' mark."""
        now = time.time() if now is None else now
        with self._lock:
            p, n = self.pressure, self._since_gate
            self.gates += 1
            metrics.emit("aware_gate", pressure=round(p, 1), events=n,
                         dry=dry, why=why)
            self._post("gate", f"a thought ({why}) over {n} things, pressure "
                       f"{p:.0f}" + (", dry run: no model call" if dry else ""), 0)
            self.pressure, self.peak, self._since_gate = 0.0, 0.0, 0
            self.thought_t = now
            self._save(now)
        return p, n

    # ------------------------------------------------------------ reports --
    def health(self) -> tuple:
        """('ok' | 'strained' | 'limited', [reasons]) from measured state."""
        b = self.body or {}
        hard = [b[k] for k in ("limit",) if k in b]
        if self.engine_down:
            hard.append("my Claude engine can't connect")
        soft = [b[k] for k in ("quota5", "quota7", "ram", "rollback") if k in b]
        if hard:
            return "limited", hard + soft
        return ("strained", soft) if soft else ("ok", [])

    def state(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            level, why = self.health()
            return {"up_s": round(self.up), "him": self.him,
                    "him_for_s": round(now - self.him_since),
                    "app": self.app,
                    "app_for_s": round(now - self.app_since) if self.app else 0,
                    "talking": self.talking, "health": level, "why": why,
                    "pressure": round(self.pressure, 1), "gates": self.gates,
                    "tick_ms": round(self.tick_ms, 3)}

    def self_line(self, now: float | None = None) -> str:
        """My measured state in one line, for when he asks how I am."""
        now = time.time() if now is None else now
        s = self.state(now)
        parts = []
        if s["up_s"]:
            parts.append(f"up {dur(s['up_s'])}")
        if s["him"] == "here":
            parts.append("he's here" + (f", in {s['app']} for "
                                        f"{dur(s['app_for_s'])}" if s["app"] else ""))
        else:
            parts.append(f"he's been quiet for {dur(s['him_for_s'])}")
        parts.append("health ok" if s["health"] == "ok"
                     else f"health {s['health']}: " + ", ".join(s["why"]))
        day = [e for e in self.events(now - 24 * 3600)
               if e.get("kind") not in QUIET_KINDS]
        if day:
            alerts = sum(1 for e in day if e.get("kind") == "alert")
            parts.append(f"noticed {len(day)} things in the last 24 h"
                         + (f" ({alerts} alerts)" if alerts else ""))
        if s["tick_ms"]:
            parts.append(f"my awareness loop costs {s['tick_ms']:.2f} ms "
                         "every 30 s, no model")
        return ("[self — your own state, measured by your code, not guessed: "
                + "; ".join(parts) + "]")

    def since_note(self, now: float | None = None) -> str:
        """What I noticed on my own while he was silent; rides once on his
        next turn. '' unless the silence was long and something mattered."""
        now = time.time() if now is None else now
        with self._lock:
            since, self._seen_t = self._seen_t, now
            silence, self._silence_peak = self._silence_peak, 0.0
        if silence < NOTE_MIN_SILENCE_S:
            return ""
        # "at" = when I noticed it, for things stamped in the past (he went
        # quiet 5 min before I could tell; I woke up before my first look)
        since = round(since, 3)           # stamps are rounded the same way
        evs = [e for e in self.events()
               if e.get("at", e["t"]) >= since
               and e.get("sal", 0) >= NOTE_MIN_SAL
               and e.get("kind") not in QUIET_KINDS]
        if not evs:
            return ""
        top = sorted(evs, key=lambda e: (-e.get("sal", 0), e["t"]))[:NOTE_MAX]
        top.sort(key=lambda e: e["t"])
        more = len(evs) - len(top)
        return ("[while he was silent — what you noticed on your own in the "
                f"{dur(silence)} since you last talked; context only, mention "
                "one only if it matters to what he's doing now]\n"
                + "\n".join(f"- {_hm(e['t'])} {e['text']}" for e in top)
                + (f"\n- (+{more} smaller things)" if more > 0 else ""))

    def digest(self, hours: float = 24, min_sal: float = 0,
               now: float | None = None) -> str:
        now = time.time() if now is None else now
        evs = [e for e in self.events(now - hours * 3600, 2_000_000)
               if e.get("sal", 0) >= min_sal]
        rows, day = [], None
        for e in sorted(evs, key=lambda e: e["t"]):
            d = datetime.datetime.fromtimestamp(e["t"]).strftime("%a %Y-%m-%d")
            if d != day:
                day = d
                rows.append(d)
            rows.append(f"  {_hm(e['t'])} [{e.get('kind', '?')} "
                        f"{e.get('sal', 0)}] {e['text']}")
        return "\n".join(rows) or "nothing noticed"


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
    a = Awareness()
    alive = a.st.get("alive")
    print("last heartbeat:", f"{_hm(alive)} ({dur(time.time() - alive)} ago)"
          if alive else "never", "| pressure", a.pressure, "of", THINK_AT,
          "| thoughts it would have had:", a.gates)
    print(a.digest(hours))
    import goals
    print("\n" + goals.Goals().render())
