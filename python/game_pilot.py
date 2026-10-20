"""Game autopilot: GOAT's reflexes for real-time play.

The brain needs 2-5 s per look-think-act, so it can play a turn-based game
(chess, cards, puzzles, strategy, menus) directly, but not anything that
moves. For that the brain writes RULES and this loop runs them 20-60 times a
second, locally, with no model in the loop:

    {"fps": 30, "stop_after_s": 120,
     "rules": [
       {"name": "jump", "if": {"color": [83, 83, 83], "tol": 20,
                               "in": [700, 400, 120, 60], "min_px": 30},
        "do": {"keys": "space"}, "cooldown_ms": 250},
       {"name": "shoot", "if": {"template": "games/templates/target.png"},
        "do": {"click": "match"}},
       {"name": "done", "if": {"text": "game over"}, "do": "stop"}]}

The brain checks status() now and then (hits per rule, fps, a frame if it
wants one) and rewrites the rules when play goes badly — a strategist over a
reflex arc.

Stops on its own when: he touches the keyboard/mouse (HumanWatch), the game
loses focus (he alt-tabbed), the time limit passes, a "stop" rule fires, or
stop() is called. Everything held is released on every exit.
"""
from __future__ import annotations

import collections
import os
import threading
import time

import numpy as np

import game_hands as gh
import screen_hands as sh

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(os.path.dirname(HERE), "workspace")
MAX_S = 900
MAX_FPS = 60


def _box(spec, win_box):
    """Rule area [x,y,w,h] in real pixels (default: the whole game view)."""
    if not spec:
        return win_box
    x, y, w, h = (int(v) for v in spec)
    return (x, y, max(1, w), max(1, h))


def _sub(frame, fbox, box):
    """Slice of the captured frame for box (both in real pixels)."""
    x0, y0 = box[0] - fbox[0], box[1] - fbox[1]
    x0, y0 = max(0, x0), max(0, y0)
    x1 = min(frame.shape[1], x0 + box[2])
    y1 = min(frame.shape[0], y0 + box[3])
    return frame[y0:y1, x0:x1], (fbox[0] + x0, fbox[1] + y0)


class Pilot:
    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self.status_d: dict = {"running": False}
        self.events = collections.deque(maxlen=12)
        self._last_frame = None
        self._templates: dict = {}
        self._t0 = time.monotonic()
        self._lock = threading.Lock()

    # ---- control ----
    def start(self, plan: dict, win: dict) -> str:
        if self._thread and self._thread.is_alive():
            self.stop("replaced by a new plan")
            self._thread.join(2)
        why = gh.anticheat_block(win)
        if why:
            raise PermissionError(gh.REFUSAL.format(why=why))
        rules = plan.get("rules") or []
        if not rules:
            raise ValueError("plan has no rules")
        for r in rules:
            self._check_rule(r)
        self._stop.clear()
        self.events.clear()
        fps = max(1, min(MAX_FPS, int(plan.get("fps") or 20)))
        limit = max(5, min(MAX_S, float(plan.get("stop_after_s") or 120)))
        self.status_d = {"running": True, "game": win.get("title", "")[:60],
                         "started": time.time(), "frames": 0, "fps": 0.0,
                         "limit_s": limit, "hits": {r.get("name") or f"rule{i}": 0
                                                    for i, r in enumerate(rules)},
                         "reason": ""}
        self._thread = threading.Thread(
            target=self._run, args=(plan, rules, win, fps, limit),
            daemon=True, name="game-pilot")
        self._thread.start()
        return (f"autopilot on {win.get('title', '')[:50]!r}: {len(rules)} rules "
                f"at {fps} fps, stops after {limit:.0f}s or the moment he "
                "touches the keyboard or mouse")

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
            import io
            from PIL import Image
            img = Image.fromarray(self._last_frame[..., ::-1])
            if max(img.size) > 1000:
                k = 1000 / max(img.size)
                img = img.resize((int(img.size[0] * k), int(img.size[1] * k)))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png = buf.getvalue()
        return s, png

    # ---- rules ----
    def _check_rule(self, r: dict):
        cond, do = r.get("if"), r.get("do")
        if not isinstance(cond, dict) or not cond:
            raise ValueError(f"rule {r.get('name')!r} needs an 'if' object")
        known = {"color", "template", "change", "pixel", "text", "every_ms",
                 "always"}
        if not known & set(cond):
            raise ValueError(f"rule {r.get('name')!r}: 'if' needs one of "
                             f"{sorted(known)}")
        if do != "stop" and not (isinstance(do, dict) and
                                 {"keys", "click", "aim", "move_to"} & set(do)):
            raise ValueError(f"rule {r.get('name')!r}: 'do' must be 'stop' or "
                             "{keys|click|aim|move_to}")
        if isinstance(do, dict) and do.get("keys"):
            gh.parse(do["keys"])
        if "template" in cond:
            self._template(cond["template"])

    def _template(self, path: str):
        import cv2
        p = path if os.path.isabs(path) else os.path.join(WORKSPACE, path)
        if p not in self._templates:
            img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
            if img is None:
                raise ValueError(f"template not found: {path}")
            self._templates[p] = img
        return self._templates[p]

    def test(self, cond: dict, frame, fbox, prev, state: dict):
        """(hit, (x, y) match centre or None) for one condition."""
        import cv2
        box = _box(cond.get("in"), fbox)
        if "always" in cond:
            return True, None
        if "every_ms" in cond:
            now = time.monotonic()
            if now - state.get("t", 0) >= cond["every_ms"] / 1000:
                state["t"] = now
                return True, None
            return False, None
        if "pixel" in cond:
            x, y = cond["pixel"]
            px = frame[y - fbox[1], x - fbox[0]][::-1].astype(int)  # BGR->RGB
            same = np.abs(px - np.array(cond.get("rgb", [0, 0, 0]))).max() \
                <= cond.get("tol", 25)
            return (same != bool(cond.get("not"))), (x, y)
        sub, origin = _sub(frame, fbox, box)
        if sub.size == 0:
            return False, None
        if "color" in cond:
            r, g, b = cond["color"]
            tol = cond.get("tol", 30)
            # Half resolution: 4x fewer pixels, still pixel-accurate enough
            # to click a target (min_px is counted at full resolution).
            half = sub[::2, ::2]
            lo = np.array([max(0, b - tol), max(0, g - tol), max(0, r - tol)],
                          np.uint8)
            hi = np.array([min(255, b + tol), min(255, g + tol),
                           min(255, r + tol)], np.uint8)
            mask = cv2.inRange(np.ascontiguousarray(half), lo, hi)
            if cv2.countNonZero(mask) * 4 < cond.get("min_px", 12):
                return False, None
            k, _, stats, cents = cv2.connectedComponentsWithStats(mask)
            if k <= 1:
                return False, None
            big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            cx, cy = cents[big]
            return True, (origin[0] + int(cx * 2), origin[1] + int(cy * 2))
        if "template" in cond:
            tpl = self._template(cond["template"])
            gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
            if gray.shape[0] < tpl.shape[0] or gray.shape[1] < tpl.shape[1]:
                return False, None
            res = cv2.matchTemplate(gray, tpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if mx < cond.get("thr", 0.8):
                return False, None
            return True, (origin[0] + loc[0] + tpl.shape[1] // 2,
                          origin[1] + loc[1] + tpl.shape[0] // 2)
        if "change" in cond:
            if prev is None:
                return False, None
            psub, _ = _sub(prev, fbox, box)
            if psub.shape != sub.shape:
                return False, None
            d = float(np.abs(sub.astype(np.int16) - psub).mean())
            thr = cond["change"]          # "change": 18, or true for 12
            thr = 12 if isinstance(thr, bool) else float(thr)
            return d >= thr, None
        if "text" in cond:
            # OCR costs ~200 ms — far too slow for the loop, so it runs on
            # its own thread at most twice a second and the loop reads the
            # latest answer (measured 2026-10-21: inline OCR held the loop
            # at 6 fps).
            now = time.monotonic()
            w = state.get("ocr")
            if (w is None or not w.is_alive()) and now - state.get("ocr_t", 0) >= 0.5:
                state["ocr_t"] = now

                def job(b=box):
                    import screen_sense
                    try:
                        state["ocr_text"] = " ".join(
                            ln["name"] for ln in screen_sense.ocr_lines(b)).lower()
                    except Exception:  # noqa: BLE001
                        pass
                state["ocr"] = threading.Thread(target=job, daemon=True)
                state["ocr"].start()
            body = state.pop("ocr_text", "")
            return bool(body) and str(cond["text"]).lower() in body, None
        return False, None

    def _act(self, do, match, fbox):
        if do == "stop":
            return "stop"
        if do.get("keys"):
            gh.run(do["keys"], self._stop)
        if do.get("click"):
            xy = match if do["click"] == "match" else tuple(do["click"])
            if xy:
                gh.click_at(int(xy[0]), int(xy[1]),
                            str(do.get("button") or "left"))
        if do.get("move_to"):
            xy = match if do["move_to"] == "match" else tuple(do["move_to"])
            if xy:
                sh.move(int(xy[0]), int(xy[1]))
        if do.get("aim") and match:
            cx = fbox[0] + fbox[2] // 2
            cy = fbox[1] + fbox[3] // 2
            gain = float(do.get("gain", 0.5))
            gh.mouse_rel(int((match[0] - cx) * gain), int((match[1] - cy) * gain),
                         int(do.get("ms", 40)))
            if do.get("then_click"):
                gh.button_down("left")
                time.sleep(0.03)
                gh.button_up("left")
        return ""

    # ---- loop ----
    def _apply(self, rules, names, states, last_hit, frame, fbox, prev,
               tick) -> str:
        """Try the rules on one frame; the first that fires acts (unless it
        says continue). Returns a stop reason, or ''."""
        for i, r in enumerate(rules):
            if self._stop.is_set():
                return ""
            if tick - last_hit[i] < r.get("cooldown_ms", 120) / 1000:
                continue
            # Time gates: games speed up, so a rule can own a phase of play
            # ("after_s": 20 -> only from 20 s in; "until_s": 20 -> only before).
            el = tick - self._t0
            if el < r.get("after_s", 0) or el >= r.get("until_s", 1e9):
                continue
            mh = r.get("max_hits")
            if mh and self.status_d["hits"][names[i]] >= mh:
                continue
            hit, match = self.test(r["if"], frame, fbox, prev, states[i])
            if not hit:
                continue
            last_hit[i] = tick
            self.status_d["hits"][names[i]] += 1
            self.events.append(f"{time.strftime('%H:%M:%S')} {names[i]}"
                               + (f" at {match}" if match else ""))
            if self._act(r["do"], match, fbox) == "stop":
                self._stop.set()
                return f"rule {names[i]!r} said stop"
            if not r.get("continue", False):
                return ""         # one action per frame by default
        return ""

    def _run(self, plan, rules, win, fps, limit):
        watch = gh.HumanWatch(lambda why: self.stop(why))
        self._watch = watch
        hooked = watch.start()
        if not hooked:
            self.events.append("human-watch hook unavailable: stop by voice")
        try:
            sh.focus_window(str(win["hwnd"]))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.15)
        x, y, w, h = sh.client_rect(win["hwnd"])
        fbox = (x, y, w, h)
        states = [dict() for _ in rules]
        last_hit = [0.0] * len(rules)
        names = [r.get("name") or f"rule{i}" for i, r in enumerate(rules)]
        prev, t0, n, last_fg = None, time.monotonic(), 0, 0.0
        self._t0 = t0
        period = 1.0 / fps
        reason = "time limit reached"
        cap = Capture(fbox)
        self.status_d["capture"] = cap.kind
        try:
            while not self._stop.is_set():
                tick = time.monotonic()
                if tick - t0 > limit:
                    break
                if tick - last_fg > 0.5:
                    last_fg = tick
                    if _user32_fg() != win["hwnd"]:
                        reason = "the game lost focus"
                        break
                frame = cap.grab()
                said = self._apply(rules, names, states, last_hit, frame,
                                   fbox, prev, tick)
                if said:
                    reason = said
                    break
                prev = frame.copy()
                self._last_frame = frame
                n += 1
                self.status_d["frames"] = n
                el = time.monotonic() - t0
                self.status_d["fps"] = round(n / el, 1) if el else 0.0
                rest = period - (time.monotonic() - tick)
                if rest > 0:
                    time.sleep(rest)
        except Exception as e:  # noqa: BLE001
            reason = f"crashed: {type(e).__name__}: {e}"
        finally:
            cap.close()
            gh.release_all()
            watch.stop()
            if watch.fired.is_set():
                reason = watch.reason + " (he took over)"
            elif self._stop.is_set() and self.status_d.get("reason"):
                reason = self.status_d["reason"]
            self.status_d["running"] = False
            self.status_d["reason"] = reason
            self.events.append(f"{time.strftime('%H:%M:%S')} stopped: {reason}")


class Capture:
    """Game frames as BGR arrays. Desktop Duplication (dxcam) first: ~2.4 ms
    a frame against mss's ~7.8 ms, and it sees what the GPU actually shows —
    which BitBlt-based mss misses for many fullscreen games (they come out
    black). mss stays as the fallback (multi-monitor regions, no dxcam)."""

    _cam = None

    def __init__(self, box):
        self.box = box
        self.kind = "mss"
        self._last = None
        self._mss = None
        try:
            import dxcam
            if Capture._cam is None:
                Capture._cam = dxcam.create(output_color="BGR")
            cam = Capture._cam
            l, t, w, h = box
            if l >= 0 and t >= 0 and l + w <= cam.width and t + h <= cam.height:
                self.kind = "dxcam"
        except Exception:  # noqa: BLE001 — no dxcam: mss still works
            pass
        if self.kind == "mss":
            import mss
            self._mss = mss.mss()

    def grab(self):
        l, t, w, h = self.box
        if self.kind == "dxcam":
            f = Capture._cam.grab(region=(l, t, l + w, t + h))
            if f is not None:            # None = nothing new since last grab
                self._last = f
            if self._last is not None:
                return self._last
        if self._mss is None:
            import mss
            self._mss = mss.mss()
        raw = self._mss.grab({"left": l, "top": t, "width": w, "height": h})
        self._last = np.frombuffer(raw.bgra, np.uint8).reshape(
            raw.height, raw.width, 4)[..., :3]
        return self._last

    def close(self):
        if self._mss is not None:
            self._mss.close()


def _user32_fg() -> int:
    import ctypes
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


PILOT = Pilot()
