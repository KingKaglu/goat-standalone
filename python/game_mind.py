"""GOAT's game mind: GOAT itself plays a game live (2026-11-02, his order).

Giorgi: "We were trying to achieve real-time decision-making while real-time
gameplay" ... "I don't want the autopilot. I want GOAT to play it."

Claude's turns take seconds and need screenshots; the autopilot is fixed
rules. The mind is neither: its own Gemini Live session (the same fast model
as GOAT's voice, thinking off) is fed the game view as a small JPEG every
step, straight from the screen capture (no screenshot tool, no Claude
tokens), and answers each frame with one `act` call: keys to press or hold,
a point to aim/click at, a short thought. The act runs, the next frame goes
out, ~1 decision a second. Claude (or the voice) only gives it a goal and
the controls, then reads status: what it saw, thought and did.

Stops on its own when: he touches the keyboard/mouse (HumanWatch), the game
loses focus, the time limit ends, or Live fails 3 times in a row. Never for
online games with anti-cheat (the caller checks game_hands.anticheat_block).
"""
from __future__ import annotations

import asyncio
import base64
import collections
import io
import json
import threading
import time

import game_hands as gh
import game_pilot
import screen_hands as sh

URL = ("wss://generativelanguage.googleapis.com/ws/"
       "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
MAX_S = 900                # one play session at most 15 min
FRAME_EDGE = 768           # Gemini's own tile size: sharper costs no more
TURN_S = 8.0               # no decision this long: the step failed
ACT_MAX_MS = 1500          # one act may hold keys this long, then it looks again
FPS_GAIN0 = 1.0            # first guess; _calibrate learns the real one per run
SMALL_W = 160              # width of the grey frame used for stuck/turn checks
SAME_DIFF = 2.5            # mean |delta| (0-255) under this = "frame unchanged"
STUCK_AFTER = 4            # this many unchanged frames / repeated moves = STUCK

ACT_TOOL = {"functionDeclarations": [{
    "name": "act",
    "description": (
        "Your next move in the game, run at once. Keep it short (100-800 ms) "
        "so you see the result in the next frame."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "thought": {"type": "STRING",
                        "description": "What you see and why this move, max 15 words."},
            "keys": {"type": "STRING",
                     "description": ('Keys: "d:400" holds d 400 ms, "w" taps w, '
                                     '"d+w:300" chord, "space*2", "wait:200". '
                                     'Empty = no keys.')},
            "point": {"type": "ARRAY", "items": {"type": "INTEGER"},
                      "description": ("[x, y] on the frame, 0-1000 each "
                                      "(0,0 top-left). Cursor view: moves the "
                                      "mouse there. First-person view: TURNS "
                                      "the camera so that spot comes under the "
                                      "crosshair; beyond 0-1000 turns further "
                                      "(-500 or 1500 = look well behind the "
                                      "edge).")},
            "click": {"type": "STRING", "enum": ["none", "left", "right"],
                      "description": "Mouse button at point."},
            "hold_ms": {"type": "INTEGER",
                        "description": ("How long to hold the button (mining/"
                                        "digging needs 300-1500). Keys run "
                                        "while it is held.")},
            "keep_holding": {"type": "BOOLEAN",
                             "description": ("true = the button STAYS down "
                                             "after this move, through your "
                                             "next look, until a move without "
                                             "it (blocks that take seconds "
                                             "to break: keep holding on the "
                                             "same point).")},
        },
        "required": ["thought"],
    }}]}

SYSTEM = """You are GOAT, Giorgi's own AI, playing a video game yourself, live, \
while he watches. Every user message is the CURRENT frame of the game (the \
newest picture is the truth) plus what your last move did. Answer EVERY \
message with exactly one call to act, nothing else: no speech, no text. \
Decide fast like a good human player: move, jump obstacles, aim, dig, fight, \
dodge danger, keep yourself alive, and work toward the goal. Look closely: \
is the thing you aimed at actually gone or changed? If 3 frames look the same, \
your move is not working: aim somewhere else, walk closer, or do something \
different. Whenever you target a new thing, give its point again. \
Coordinates are 0-1000 \
across the frame, [x, y], (0,0) top-left.

FIRST-PERSON frames (marked [first-person]: mouse locked to the game): you \
act ONLY at the crosshair in the centre. To aim at something, give its point: \
the camera turns until it sits under the crosshair. A wall, leaves or blocks \
filling the view means you FACE them: turn away (point [100,500] or [900,500]) \
instead of backing up again and again. Holding the left button to break a \
block: send click left with NO point each frame; the button stays down \
between frames until you turn, move or stop clicking (letting go resets the \
breaking). A text line starting STUCK means your moves change nothing: do \
something clearly different.

GAME: {game}
GOAL: {goal}
CONTROLS / NOTES: {notes}"""


def _key() -> str:
    import talk_brain
    return talk_brain.api_key()


def _model() -> str:
    import live_talk
    return live_talk.MODEL


class Mind:
    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self.status_d: dict = {"running": False}
        self.events = collections.deque(maxlen=16)
        self._last_frame = None
        self._held = None          # (button, (x, y)) kept down across moves
        self._fps = False          # mouse locked: turn the camera, not a cursor
        self.gain = FPS_GAIN0      # mouse counts per screen pixel of turn
        self._turn_probe = None    # (intended small-frame px, small frame before)
        self._prev_small = None
        self._same_n = 0
        self._moves = collections.deque(maxlen=6)

    # ---- control ----
    def start(self, win: dict, goal: str, notes: str = "", seconds: float = 180,
              region=None) -> str:
        if self._thread and self._thread.is_alive():
            self.stop("replaced by a new goal")
            self._thread.join(3)
        why = gh.anticheat_block(win)
        if why:
            raise PermissionError(gh.REFUSAL.format(why=why))
        if not goal:
            raise ValueError("the mind needs a goal")
        limit = max(10, min(MAX_S, float(seconds or 180)))
        if region:
            box = tuple(int(v) for v in region)
        else:
            box = sh.client_rect(win["hwnd"])
        self._stop.clear()
        self.events.clear()
        self._fps, self.gain, self._turn_probe = False, FPS_GAIN0, None
        self._prev_small, self._same_n = None, 0
        self._moves.clear()
        self.status_d = {"running": True, "game": win.get("title", "")[:60],
                         "goal": goal[:200], "started": time.time(),
                         "limit_s": limit, "steps": 0, "avg_decision_ms": 0,
                         "view": list(box), "reason": ""}
        self._thread = threading.Thread(
            target=self._run, args=(win, goal, notes, box, limit),
            daemon=True, name="game-mind")
        self._thread.start()
        return (f"GOAT is playing {win.get('title', '')[:50]!r} itself: goal "
                f"{goal[:80]!r}, view {box}, stops after {limit:.0f}s or the "
                "moment he touches the keyboard or mouse")

    def stop(self, reason: str = "stopped") -> str:
        if self.status_d.get("running"):
            self.status_d["reason"] = reason
        self._stop.set()
        gh.release_all()
        return reason

    def status(self, frame: bool = False):
        s = dict(self.status_d)
        if s.get("started"):
            s["elapsed_s"] = round(time.time() - s["started"], 1)
        s["recent"] = list(self.events)
        png = None
        if frame and self._last_frame is not None:
            png = _jpeg(self._last_frame, 900, fmt="PNG")
        return s, png

    def _log(self, line: str):
        self.events.append(f"{time.strftime('%H:%M:%S')} {line}")
        print(f"[mind] {line}", flush=True)

    # ---- the loop ----
    def _run(self, win, goal, notes, box, limit):
        watch = gh.HumanWatch(lambda why: self.stop(why))
        if not watch.start():
            self._log("human-watch hook unavailable: stop by voice")
        reason = "time limit reached"
        try:
            try:
                sh.focus_window(str(win["hwnd"]))
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.15)
            reason = asyncio.run(self._play(win, goal, notes, box, limit)) or reason
        except Exception as e:  # noqa: BLE001
            reason = f"crashed: {type(e).__name__}: {e}"
        finally:
            self._held = None
            gh.release_all()
            watch.stop()
            if watch.fired.is_set():
                reason = watch.reason + " (he took over)"
            elif self._stop.is_set() and self.status_d.get("reason"):
                reason = self.status_d["reason"]
            self.status_d["running"] = False
            self.status_d["reason"] = reason
            self._log(f"stopped: {reason}")

    async def _play(self, win, goal, notes, box, limit) -> str:
        import websockets
        cap = game_pilot.Capture(box)
        system = SYSTEM.format(game=win.get("title", "")[:80], goal=goal,
                               notes=notes or "work them out from the screen")
        setup = {"setup": {
            "model": f"models/{_model()}",
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "thinkingConfig": {"thinkingBudget": 0}},
            "systemInstruction": {"parts": [{"text": system}]},
            "tools": [ACT_TOOL],
            "contextWindowCompression": {"slidingWindow": {}},
            "realtimeInputConfig": {"automaticActivityDetection": {"disabled": True}},
        }}
        t0 = time.monotonic()
        fails, total_ms, last = 0, 0.0, "nothing yet (first frame)"
        try:
            async with websockets.connect(f"{URL}?key={_key()}",
                                          max_size=16 * 1024 * 1024,
                                          open_timeout=8) as ws:
                await ws.send(json.dumps(setup))
                first = json.loads(await asyncio.wait_for(ws.recv(), 8))
                if "setupComplete" not in first:
                    return f"Live refused the session: {str(first)[:160]}"
                self._log(f"mind online on {_model()}")
                while not self._stop.is_set():
                    if time.monotonic() - t0 > limit:
                        return "time limit reached"
                    if game_pilot._user32_fg() != win["hwnd"]:
                        return "the game lost focus"
                    frame = cap.grab()
                    self._last_frame = frame
                    small = _gray(frame)
                    self._calibrate(small)
                    self._fps = pointer_locked()
                    self.status_d["view_mode"] = "first-person" if self._fps else "cursor"
                    stuck = self._stuck_note(small, last)
                    sent = time.monotonic()
                    tag = "[first-person] " if self._fps else "[cursor] "
                    await ws.send(json.dumps({"clientContent": {"turns": [{
                        "role": "user", "parts": [
                            {"inlineData": {"mimeType": "image/jpeg",
                                            "data": _jpeg(frame, FRAME_EDGE, b64=True)}},
                            {"text": f"{tag}t={sent - t0:.1f}s. Your last move: {last}. "
                                     f"{stuck}Next act?"}]}],
                        "turnComplete": True}}))
                    call = await self._decision(ws)
                    if call is None:
                        fails += 1
                        self._log("no decision this step")
                        if fails >= 3:
                            return "the mind stopped answering (3 empty steps)"
                        continue
                    fails = 0
                    ms = (time.monotonic() - sent) * 1000
                    n = self.status_d["steps"] = self.status_d["steps"] + 1
                    total_ms += ms
                    self.status_d["avg_decision_ms"] = round(total_ms / n)
                    args = call.get("args") or {}
                    last = await asyncio.to_thread(self._act, args, box)
                    self._log(f"{ms:.0f}ms  {args.get('thought', '')[:90]}  -> {last}")
                    # No toolResponse: it made Live call act AGAIN on the
                    # old frame (2-15 ms "decisions", measured 2026-11-02);
                    # the result rides along with the next frame instead,
                    # and decisions got faster (0.4-0.9 s).
        finally:
            cap.close()
        return ""

    async def _decision(self, ws):
        """Read until the model calls act on THIS frame (or gives up)."""
        sent = time.monotonic()
        end = sent + TURN_S
        while time.monotonic() < end and not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), max(0.1, end - time.monotonic()))
            except asyncio.TimeoutError:
                return None
            msg = json.loads(raw)
            calls = (msg.get("toolCall") or {}).get("functionCalls") or []
            for c in calls:
                # under 80 ms can't be about the frame just sent: a leftover
                if c.get("name") == "act" and time.monotonic() - sent > 0.08:
                    return c
            if "goAway" in msg:
                self._log("Live said goAway")
        return None

    # ---- seeing its own effect ----
    def _stuck_note(self, small, last: str) -> str:
        """Code-side loop breaker: the 2026-11-02 Bloxd run hit one leaf block
        15 times and backed off foliage 6 times; the prompt alone didn't stop it."""
        prev, self._prev_small = self._prev_small, small
        if prev is not None and prev.shape == small.shape:
            same = float(abs(small - prev).mean()) < SAME_DIFF
            self._same_n = self._same_n + 1 if same else 0
        kind = _move_kind(last)
        if "still holding" not in kind:      # a held button is judged by the view
            self._moves.append(kind)
        repeats = (len(self._moves) == self._moves.maxlen
                   and len(set(self._moves)) == 1 and self._moves[0] != "nothing")
        if repeats:
            self._moves.clear()              # warn once per streak, not every frame
        if self._same_n >= STUCK_AFTER or repeats:
            why = (f"the view hasn't changed for {self._same_n} moves"
                   if self._same_n >= STUCK_AFTER else
                   f"you did the same kind of move {self._moves.maxlen} times")
            self._log(f"STUCK: {why}")
            return (f"STUCK: {why}; it is not working. Do something clearly "
                    "different (turn to a new target, jump, walk elsewhere). ")
        return ""

    def _calibrate(self, small):
        """Learn mouse counts per pixel from how far the view really slid
        after a pure turn (phase correlation), so aiming converges in 1-2
        moves whatever the game's sensitivity."""
        probe, self._turn_probe = self._turn_probe, None
        if probe is None or small is None:
            return
        want, before = probe
        if before.shape != small.shape:
            return
        got, peak = _shift_x(before, small)
        if peak < 0.08 or abs(got) < 0.2 * abs(want):
            return                          # unreliable, or the turn didn't land
        k = max(0.5, min(2.0, (abs(want) / abs(got)) ** 0.7))
        self.gain = max(0.1, min(10.0, self.gain * k))
        self.status_d["turn_gain"] = round(self.gain, 2)

    def _act(self, a: dict, box) -> str:
        if self._stop.is_set():
            return "stopped"
        if self._fps:
            return self._act_fps(a, box)
        if self._held and self._held[1] is None:     # left first-person view
            gh.button_up(self._held[0])
            self._held = None
        did = []
        l, t, w, h = box
        button = str(a.get("click") or "none")
        keep = bool(a.get("keep_holding")) and button in ("left", "right")
        pt = a.get("point")
        xy = None
        if isinstance(pt, list) and len(pt) == 2:
            xy = (l + int(max(0, min(1000, pt[0])) * w / 1000),
                  t + int(max(0, min(1000, pt[1])) * h / 1000))
        held = self._held
        # still holding the same button on (about) the same spot: carry on
        same = (held and keep and held[0] == button
                and (xy is None or abs(xy[0] - held[1][0]) + abs(xy[1] - held[1][1]) < 24))
        if held and not same:
            gh.button_up(held[0])
            self._held = None
        if xy and not same:
            sh.move(*xy)
            did.append(f"mouse at {pt}")
        hold = max(40, min(ACT_MAX_MS, int(a.get("hold_ms") or 60)))
        keys = _fix_keys(str(a.get("keys") or "").strip())
        if same:
            did.append(f"still holding {button}")
        elif button in ("left", "right"):
            gh.button_down(button)
        if keep:
            self._held = (button, xy or (held[1] if held else sh_cursor()))
        t_keys = time.monotonic()
        if keys:
            try:
                gh.run(keys, self._stop)
                did.append(f"keys {keys}")
            except Exception as e:  # noqa: BLE001
                did.append(f"keys {keys!r} failed: {e}")
        if button in ("left", "right") and not keep:
            rest = hold / 1000 - (time.monotonic() - t_keys)
            if rest > 0:
                self._stop.wait(rest)
            gh.button_up(button)
            did.append(f"{button} held {hold}ms")
        elif keep and not same:
            did.append(f"{button} down, kept held")
        return ", ".join(did) or "did nothing"

    def _act_fps(self, a: dict, box) -> str:
        """Mouse locked to the game: `point` turns the camera (relative motion,
        the absolute cursor moves of the 2026-11-02 Bloxd run did nothing) and
        the left button stays down between frames so blocks actually break."""
        did = []
        l, t, w, h = box
        button = str(a.get("click") or "none")
        keys = _fix_keys(str(a.get("keys") or "").strip())
        pt = a.get("point")
        dx = dy = 0.0
        if isinstance(pt, list) and len(pt) == 2:
            px = max(-1000, min(2000, int(pt[0])))
            py = max(0, min(1000, int(pt[1])))
            dx, dy = (px - 500) * w / 1000, (py - 500) * h / 1000
        turning = abs(dx) + abs(dy) > 0.025 * w
        keep = button == "left" and a.get("keep_holding", True) is not False
        held = self._held
        if held and (turning or not keep or held[0] != button):
            gh.button_up(held[0])
            self._held = held = None
            did.append("let go")
        if turning:
            gh.mouse_rel(int(dx * self.gain), int(dy * self.gain), ms=90)
            time.sleep(0.05)
            did.append(f"turned toward {pt}")
            want = dx * SMALL_W / w
            if not keys and 4 <= abs(want) <= 0.35 * SMALL_W \
                    and self._prev_small is not None:
                self._turn_probe = (want, self._prev_small)
        if keep:
            if held:
                did.append("still holding left")
            else:
                gh.button_down("left")
                self._held = ("left", None)
                did.append("left down, kept held")
        elif button in ("left", "right"):
            gh.button_down(button)
        t_keys = time.monotonic()
        if keys:
            try:
                gh.run(keys, self._stop)
                did.append(f"keys {keys}")
            except Exception as e:  # noqa: BLE001
                did.append(f"keys {keys!r} failed: {e}")
        if button in ("left", "right") and not keep:
            hold = max(40, min(ACT_MAX_MS, int(a.get("hold_ms") or 60)))
            rest = hold / 1000 - (time.monotonic() - t_keys)
            if rest > 0:
                self._stop.wait(rest)
            gh.button_up(button)
            did.append(f"{button} click {hold}ms")
        return ", ".join(did) or "did nothing"


def pointer_locked() -> bool:
    """True when the game has captured the mouse (browser pointer lock, FPS
    games): the cursor is hidden or clipped to a tiny box."""
    import ctypes
    from ctypes import wintypes as wt

    class _CI(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD),
                    ("hCursor", wt.HANDLE), ("pt", wt.POINT)]
    u = ctypes.windll.user32
    ci = _CI()
    ci.cbSize = ctypes.sizeof(_CI)
    hidden = bool(u.GetCursorInfo(ctypes.byref(ci))) and not (ci.flags & 1)
    r = wt.RECT()
    clipped = (bool(u.GetClipCursor(ctypes.byref(r)))
               and r.right - r.left < 64 and r.bottom - r.top < 64)
    return hidden or clipped


def _gray(frame):
    import numpy as np
    from PIL import Image
    if frame is None:
        return None
    img = Image.fromarray(np.ascontiguousarray(frame[..., 1]))
    hh = max(8, int(img.size[1] * SMALL_W / img.size[0]))
    return np.asarray(img.resize((SMALL_W, hh)), dtype=np.float32)


def _shift_x(a, b) -> tuple:
    """Horizontal slide between two grey frames (phase correlation) and how
    sure it is (peak height, ~0 noise .. 1 exact)."""
    import numpy as np
    fa = np.fft.fft2(a - a.mean())
    fb = np.fft.fft2(b - b.mean())
    r = fa * np.conj(fb)
    r /= np.abs(r) + 1e-9
    c = np.fft.ifft2(r).real
    y, x = np.unravel_index(int(c.argmax()), c.shape)
    if x > c.shape[1] // 2:
        x -= c.shape[1]
    return float(x), float(c.max())


def _fix_keys(keys: str) -> str:
    """'a:500+w:500' (each key timed) -> 'a+w:500', the chord gh.run reads."""
    import re
    out = []
    for tok in keys.split():
        if "+" in tok and tok.count(":") > 1:
            parts = [p.split(":") for p in tok.split("+")]
            ms = max((int(p[1]) for p in parts if len(p) > 1 and p[1].isdigit()),
                     default=200)
            tok = "+".join(p[0] for p in parts) + f":{ms}"
        out.append(re.sub(r"\s+", "", tok))
    return " ".join(out)


def _move_kind(last: str) -> str:
    import re
    return " ".join(re.sub(r"[-\d.:\[\],]+", " ", last or "").split()) or "nothing"


def sh_cursor() -> tuple:
    import ctypes
    from ctypes import wintypes as wt
    p = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def _jpeg(frame, edge: int, b64: bool = False, fmt: str = "JPEG"):
    from PIL import Image
    img = Image.fromarray(frame[..., ::-1])
    if max(img.size) > edge:
        k = edge / max(img.size)
        img = img.resize((int(img.size[0] * k), int(img.size[1] * k)))
    buf = io.BytesIO()
    img.save(buf, format=fmt, **({"quality": 70} if fmt == "JPEG" else {}))
    data = buf.getvalue()
    return base64.b64encode(data).decode() if b64 else data


MIND = Mind()
