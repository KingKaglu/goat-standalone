"""Live eyes: GOAT watches the laptop continuously, not one screenshot at a time.

His goal, 2026-11-01: "I want you to be in my laptop, be live, see everything
in time" (better for trading and overall). No model can hold a 30 fps video,
so the seeing happens here, locally and for free, and the brains read the
result (workspace/live-sight/proposal.md, phase 1):

  * Window events (SetWinEventHook): focus moves, windows opening, closing,
    minimizing, dialogs, title changes (Binance's title IS the live price).
    Instant, no pixels.
  * Pixels (Desktop Duplication via dxcam, mss fallback): the whole screen
    is compared 5x a second at 1/4 scale (~5 ms a tick). dxcam hands back
    nothing at all when nothing was redrawn, which is the cheapest possible
    "no change".
  * When the window he's on changes and settles, its controls (UI
    Automation) and its text (Windows OCR, only the changed areas when the
    change is small) are refreshed into a cached SCREEN STATE.

screen_sense.look/find/read use that cache when it is fresh, so they answer
in milliseconds instead of ~0.5-1.5 s; the `computer` tool's `live` action
and changes() read the event log ("what happened in the last minute"), so
the brain knows what moved without taking a screenshot.

Off switch: GOAT_LIVE_EYES=off. Standalone test: py -3.13 live_eyes.py [secs]
"""
from __future__ import annotations

import collections
import ctypes
import ctypes.wintypes as wt
import os
import threading
import time

import numpy as np

import screen_hands

TICK_S = 0.2              # pixel check rate (5 Hz)
SETTLE_S = 0.35           # a change counts as finished after this much calm
BUSY_REFRESH_S = 4.0      # while it never settles (live chart), refresh this often
MIN_REFRESH_S = 0.8       # never refresh more often than this
UIA_BUSY_S = 8.0          # controls refresh while busy (UIA is the costly part)
FACTOR = 4                # downscale for change detection
EVENTS_MAX = 300

_user32 = ctypes.windll.user32
_ME = os.getpid()

# WinEvent ids
EV_ALERT, EV_FOREGROUND, EV_DIALOGSTART = 0x0002, 0x0003, 0x0010
EV_MINSTART, EV_MINEND = 0x0016, 0x0017
EV_DESTROY, EV_SHOW, EV_HIDE, EV_NAMECHANGE = 0x8001, 0x8002, 0x8003, 0x800C
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS = 0x0000, 0x0002
GA_ROOT = 2

_WINEVENTPROC = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG,
                                   wt.LONG, wt.DWORD, wt.DWORD)


def _title(hwnd) -> str:
    n = _user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


_app_cache: dict = {}


def _app(pid: int) -> str:
    if pid not in _app_cache:
        try:
            import psutil
            _app_cache[pid] = psutil.Process(pid).name().rsplit(".", 1)[0]
        except Exception:  # noqa: BLE001
            _app_cache[pid] = "?"
    return _app_cache[pid]


def _pid(hwnd) -> int:
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


class LiveEyes:
    def __init__(self):
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.events = collections.deque(maxlen=EVENTS_MAX)
        self.seq = 0                  # event counter, for "since" queries
        self.tick = 0                 # pixel ticks done
        self.version = 0              # cache refreshes done
        self.front: dict = {}         # window he's on (not GOAT)
        self.box = None               # its client area (l, t, w, h)
        self.ui: list = []            # cached controls of that window
        self.lines: list = []         # cached OCR lines of that window
        self.refreshed = 0.0          # monotonic time of last refresh
        self.ui_refreshed = 0.0
        self.dirty: list = []         # changed rects inside box since refresh
        self.last_change = 0.0
        self.changing_since = 0.0
        self.capture = "none"
        self.stats = collections.deque(maxlen=50)   # tick ms
        self.refresh_ms = collections.deque(maxlen=20)
        self.running = False
        self.error = ""
        self._q = collections.deque(maxlen=2000)    # raw WinEvents
        self._known: dict = {}        # hwnd -> title, for top-level windows
        self._region_ev: dict = {}    # region key -> event (coalescing)
        self._title_ev: dict = {}     # hwnd -> title-change event
        self._stop = threading.Event()
        self._wake = threading.Event()   # fresh() asks for a tick right now
        self._cam = None
        self.skip_pids = {_ME}
        self.switched_at = 0.0

    # ------------------------------------------------------------ events --
    def _log(self, kind: str, text: str, hwnd: int = 0, **extra) -> dict:
        with self.lock:
            self.seq += 1
            e = {"seq": self.seq, "t": time.time(), "kind": kind,
                 "text": text, "hwnd": hwnd, **extra}
            self.events.append(e)
            self.cv.notify_all()
            return e

    def _bump(self, e: dict, text: str):
        """Coalesce a repeating event (price ticking in a title) in place."""
        with self.lock:
            self.seq += 1
            e.update(text=text, t=time.time(), seq=self.seq,
                     n=e.get("n", 1) + 1)
            try:                      # move to the end: it's the newest again
                self.events.remove(e)
            except ValueError:
                pass
            self.events.append(e)
            self.cv.notify_all()

    def _hook_thread(self):
        def cb(_h, event, hwnd, id_obj, id_child, _thr, _ms):
            if id_obj == 0 and id_child == 0 and hwnd:
                self._q.append((event, int(hwnd), time.time()))
        self._cb = _WINEVENTPROC(cb)      # keep a reference or it's collected
        hooks = []
        for lo, hi in ((EV_ALERT, EV_FOREGROUND), (EV_DIALOGSTART, EV_DIALOGSTART),
                       (EV_MINSTART, EV_MINEND), (EV_DESTROY, EV_HIDE),
                       (EV_NAMECHANGE, EV_NAMECHANGE)):
            h = _user32.SetWinEventHook(lo, hi, None, self._cb, 0, 0,
                                        WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
            if h:
                hooks.append(h)
        self._hook_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = wt.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        for h in hooks:
            _user32.UnhookWinEvent(h)

    def _drain(self):
        """Turn raw WinEvents into readable log lines."""
        while self._q:
            ev, hwnd, _t = self._q.popleft()
            if _user32.GetAncestor(hwnd, GA_ROOT) != hwnd and ev != EV_DESTROY:
                continue              # child objects: not a window event
            if ev == EV_DESTROY:
                title = self._known.pop(hwnd, None)
                if title:
                    self._log("closed", f"window closed: {title[:70]!r}", hwnd)
                continue
            title = _title(hwnd)
            pid = _pid(hwnd)
            if pid == _ME:
                continue
            if not title.strip():
                continue
            app = _app(pid)
            if ev == EV_FOREGROUND:
                self._log("focus", f"{app} came to the front: {title[:70]!r}", hwnd)
            elif ev == EV_SHOW and _user32.IsWindowVisible(hwnd):
                if hwnd not in self._known:
                    self._log("opened", f"{app} opened a window: {title[:70]!r}", hwnd)
            elif ev == EV_HIDE:
                if self._known.pop(hwnd, None):
                    self._log("closed", f"window hidden: {title[:70]!r}", hwnd)
                continue
            elif ev == EV_DIALOGSTART:
                self._log("dialog", f"{app} showed a dialog: {title[:70]!r}", hwnd)
            elif ev == EV_MINSTART:
                self._log("minimized", f"minimized: {title[:70]!r}", hwnd)
            elif ev == EV_MINEND:
                self._log("restored", f"restored: {title[:70]!r}", hwnd)
            elif ev == EV_ALERT:
                self._log("alert", f"{app} raised an alert: {title[:70]!r}", hwnd)
            elif ev == EV_NAMECHANGE:
                old = self._known.get(hwnd)
                if old is not None and old != title:
                    txt = f"{app} title now {title[:80]!r}"
                    prev = self._title_ev.get(hwnd)
                    if prev and time.time() - prev["t"] < 30:
                        self._bump(prev, txt)
                    else:
                        self._title_ev[hwnd] = self._log("title", txt, hwnd)
            if _user32.IsWindowVisible(hwnd):
                self._known[hwnd] = title

    # ------------------------------------------------------------ pixels --
    def _grab_small(self):
        """Whole screen, grey, 1/4 scale. None = nothing was redrawn."""
        if self._cam is not None:
            f = self._cam.grab()
            if f is None:
                return None
            s = f[::FACTOR, ::FACTOR]
        else:
            import mss
            if not hasattr(self, "_mss"):
                self._mss = mss.mss()
            raw = self._mss.grab(self._mss.monitors[1])
            a = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(raw.height, raw.width, 4)
            s = a[::FACTOR, ::FACTOR, :3]
        return ((s[..., 0].astype(np.uint16) + s[..., 1] + s[..., 2]) // 3
                ).astype(np.uint8)

    def _open_capture(self):
        try:
            import dxcam
            try:
                import game_pilot
                cam = getattr(game_pilot.Capture, "_cam", None)
            except Exception:  # noqa: BLE001
                cam = None
            self._cam = cam or dxcam.create(output_color="BGR")
            self.capture = "dxcam"
        except Exception:  # noqa: BLE001 — mss still works
            self._cam = None
            self.capture = "mss"

    # ----------------------------------------------------------- refresh --
    def _front_window(self):
        """Topmost real window that isn't GOAT (fast_hands.target_window's
        rule, with GOAT's pid list settable for the standalone test)."""
        import fast_hands
        w = None
        for c in screen_hands.windows():
            if (c["pid"] in self.skip_pids or c["minimized"]
                    or c["width"] < 200 or c["height"] < 150
                    or c["title"].strip().lower() in fast_hands._SHELL_TITLES):
                continue
            w = c
            break
        if w is None:
            return None, None
        x, y, cw, ch = screen_hands.client_rect(w["hwnd"])
        if cw > 50 and ch > 50:
            w.update(left=x, top=y, width=cw, height=ch)
        w["app"] = _app(w["pid"])
        return w, (w["left"], w["top"], w["width"], w["height"])

    def _refresh(self, full: bool, want_ui: bool):
        import screen_sense
        win, box = self.front, self.box
        t0 = time.monotonic()
        with self.lock:
            dirty = list(self.dirty)
            self.dirty = []
        old_lines = self.lines
        area = box[2] * box[3]
        dirty_area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in dirty)
        if not full and (len(dirty) > 6 or dirty_area > 0.4 * area):
            full = True
        lines = []
        try:
            if full:
                lines = screen_sense.ocr_lines(box)
            else:
                # OCR full-width STRIPS around the changed rows: a tight crop
                # cuts words in half ("k 11" for "tick 11", 2026-11-01 test).
                # Old lines touching a strip are replaced by what it reads.
                pad = 12
                spans = sorted((max(box[1], r[1] - pad),
                                min(box[1] + box[3], r[3] + pad)) for r in dirty)
                grown = []
                for a, b in spans:            # cover whole glyphs of a big
                    for ln in old_lines:      # line whose middle changed
                        if ln["rect"][1] < b and ln["rect"][3] > a:
                            a = max(box[1], min(a, ln["rect"][1] - 4))
                            b = min(box[1] + box[3], max(b, ln["rect"][3] + 4))
                    grown.append((a, b))
                spans = sorted(grown)
                merged = []
                for a, b in spans:
                    if merged and a <= merged[-1][1]:
                        merged[-1][1] = max(merged[-1][1], b)
                    else:
                        merged.append([a, b])
                boxes = [(box[0], a, box[2], b - a) for a, b in merged if b - a > 8]
                fresh = []
                for b in boxes:
                    fresh.extend(screen_sense.ocr_lines(b))

                def inside(ln):
                    return any(ln["rect"][1] < b[1] + b[3] and ln["rect"][3] > b[1]
                               for b in boxes)
                lines = [ln for ln in old_lines if not inside(ln)] + fresh
                self._log_content(boxes, fresh, old_lines, inside)
        except Exception as e:  # noqa: BLE001
            self.error = f"ocr: {type(e).__name__}: {e}"
            lines = old_lines
        ui = self.ui
        if want_ui:
            try:
                ui = screen_sense.uia_elements(win)
                self.ui_refreshed = time.monotonic()
            except Exception as e:  # noqa: BLE001
                self.error = f"uia: {type(e).__name__}: {e}"
        with self.lock:
            if win is self.front:          # still the same window
                self.lines, self.ui = lines, ui
                self.refreshed = time.monotonic()
                self.version += 1
                self.cv.notify_all()
        self.refresh_ms.append(int((time.monotonic() - t0) * 1000))

    def _log_content(self, boxes, fresh, old_lines, inside):
        """One readable event per changed area whose TEXT changed; repeats in
        the same spot (a ticking price) update one entry in place."""
        app = self.front.get("app", "")
        for b in boxes[:4]:
            def within(ln, b=b):
                c = ((ln["rect"][0] + ln["rect"][2]) // 2,
                     (ln["rect"][1] + ln["rect"][3]) // 2)
                return b[0] <= c[0] <= b[0] + b[2] and b[1] <= c[1] <= b[1] + b[3]
            new = " | ".join(ln["name"] for ln in fresh if within(ln))[:160]
            old = " | ".join(ln["name"] for ln in old_lines if within(ln))[:160]
            if not new or new == old:
                continue
            key = (self.front.get("hwnd"), b[0] // 80, b[1] // 60)
            txt = f"{app} ({b[0]},{b[1]} {b[2]}x{b[3]}) now reads {new!r}"
            prev = self._region_ev.get(key)
            if prev and time.time() - prev["t"] < 30:
                self._bump(prev, txt)
            else:
                self._region_ev[key] = self._log("text", txt,
                                                 self.front.get("hwnd", 0))
        if len(self._region_ev) > 200:
            self._region_ev.clear()

    # -------------------------------------------------------------- loop --
    def _loop(self):
        screen_hands.dpi_init()
        self._open_capture()
        for w in screen_hands.windows():
            if w["pid"] != _ME:
                self._known[w["hwnd"]] = w["title"]
        prev = None
        next_windows = 0.0
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._drain()
                if t0 >= next_windows:            # catch anything hooks missed
                    next_windows = t0 + 3.0
                    for w in screen_hands.windows():
                        if w["pid"] != _ME:
                            self._known.setdefault(w["hwnd"], w["title"])
                win, box = self._front_window()
                switched = bool(win) and (win["hwnd"] != self.front.get("hwnd")
                                          or box != self.box)
                if switched:
                    with self.lock:
                        self.front, self.box = win, box
                        self.dirty, self.ui, self.lines = [], [], []
                        self.refreshed = 0.0
                    self.switched_at = t0
                    self._region_ev.clear()
                small = self._grab_small()
                if small is not None:
                    if prev is not None and prev.shape == small.shape and self.box:
                        self._diff(prev, small)
                    prev = small
                now = time.monotonic()
                if self.box:
                    calm = now - self.last_change >= SETTLE_S
                    if self.refreshed == 0.0:
                        # First read of a new window: once it holds still,
                        # or after 1 s if it never does (animated pages).
                        if (now - self.last_change >= 0.25
                                or now - self.switched_at >= 1.0):
                            self._refresh(full=True, want_ui=True)
                            self.changing_since = 0.0
                    elif self.dirty and now - self.refreshed >= MIN_REFRESH_S:
                        busy = now - self.changing_since >= BUSY_REFRESH_S
                        if calm or busy:
                            want_ui = calm or now - self.ui_refreshed >= UIA_BUSY_S
                            self._refresh(full=False, want_ui=want_ui)
                            if calm:
                                self.changing_since = 0.0
                            elif busy:
                                self.changing_since = now
                with self.lock:
                    self.tick += 1
                    self.cv.notify_all()
            except Exception as e:  # noqa: BLE001 — the eyes never kill GOAT
                self.error = f"{type(e).__name__}: {e}"
                time.sleep(1.0)
            self.stats.append((time.monotonic() - t0) * 1000)
            self._wake.wait(max(0.0, TICK_S - (time.monotonic() - t0)))
            self._wake.clear()
        self.running = False

    def _diff(self, a, b):
        import cv2
        d = cv2.absdiff(a, b)
        _, m = cv2.threshold(d, 24, 255, cv2.THRESH_BINARY)
        if not m.any():
            return
        m = cv2.dilate(m, np.ones((5, 5), np.uint8), iterations=2)
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        bl, bt, bw, bh = self.box
        hit = []
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            if w * h < 12:
                continue
            r = (x * FACTOR, y * FACTOR, (x + w) * FACTOR, (y + h) * FACTOR)
            l, t = max(r[0], bl), max(r[1], bt)
            rr, bb = min(r[2], bl + bw), min(r[3], bt + bh)
            if rr - l > 4 and bb - t > 4:
                hit.append((l, t, rr, bb))
        if not hit:
            return
        now = time.monotonic()
        with self.lock:
            if not self.changing_since:
                self.changing_since = now
            self.last_change = now
            self.dirty.extend(hit)
            if len(self.dirty) > 40:      # collapse to one bounding box
                xs = [r[0] for r in self.dirty] + [r[2] for r in self.dirty]
                ys = [r[1] for r in self.dirty] + [r[3] for r in self.dirty]
                self.dirty = [(min(xs), min(ys), max(xs), max(ys))]

    # --------------------------------------------------------------- api --
    def start(self):
        if self.running:
            return self
        self.running = True
        self._stop.clear()
        threading.Thread(target=self._hook_thread, name="live-eyes-hooks",
                         daemon=True).start()
        threading.Thread(target=self._loop, name="live-eyes", daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()
        tid = getattr(self, "_hook_tid", None)
        if tid:
            _user32.PostThreadMessageW(tid, 0x0012, 0, 0)   # WM_QUIT

    def fresh(self, hwnd, box, wait: float = 1.5):
        """(ui, lines) for this window if the cache reflects the screen right
        now, else None. Waits for one more pixel tick (so a click made a
        moment ago is seen) and for a pending refresh, up to `wait` s."""
        if not self.running:
            return None
        deadline = time.monotonic() + wait
        with self.cv:
            # Two ticks: the one possibly under way began before this call.
            # Waking the loop makes each one start at once (~2 ms apiece).
            start_tick = self.tick
            while time.monotonic() < deadline:
                if self.tick < start_tick + 2:
                    self._wake.set()
                    self.cv.wait(0.05)
                    continue
                same = (self.front.get("hwnd") == hwnd and self.box is not None
                        and tuple(self.box) == tuple(box))
                if not same:
                    return None
                if self.refreshed:
                    if not self.dirty:
                        return list(self.ui), list(self.lines)
                    area = box[2] * box[3]
                    d = sum((r[2] - r[0]) * (r[3] - r[1]) for r in self.dirty)
                    # A small area that never stops moving (a live price,
                    # a clock): controls haven't moved, serve the cache.
                    if (d < 0.05 * area and self.changing_since
                            and time.monotonic() - self.refreshed < BUSY_REFRESH_S + 1):
                        return list(self.ui), list(self.lines)
                self.cv.wait(0.05)
        return None

    def since(self, seq: int = 0, seconds: float | None = None) -> list:
        cut = time.time() - seconds if seconds else 0
        with self.lock:
            return [dict(e) for e in self.events
                    if e["seq"] > seq and e["t"] >= cut]

    def wait_event(self, pred, timeout: float) -> dict | None:
        """Block until an event matching pred arrives (newer than now)."""
        deadline = time.monotonic() + timeout
        with self.cv:
            start = self.seq
            while time.monotonic() < deadline:
                for e in self.events:
                    if e["seq"] > start and pred(e):
                        return dict(e)
                self.cv.wait(min(0.25, max(0.01, deadline - time.monotonic())))
        return None

    def wait_text(self, text=None, gone=None, timeout: float = 10.0):
        """Wait for text to appear in / vanish from the front window's cached
        text. Returns seconds taken, or None on timeout."""
        import screen_sense
        t0 = time.monotonic()
        want = screen_sense._norm(text) if text else ""
        drop = screen_sense._norm(gone) if gone else ""
        seen = -1
        with self.cv:
            while time.monotonic() - t0 < timeout:
                if self.version != seen and self.refreshed:
                    seen = self.version
                    body = screen_sense._norm(" ".join(
                        [ln["name"] for ln in self.lines] +
                        [e["name"] for e in self.ui]))
                    if want and want in body:
                        return time.monotonic() - t0
                    if drop and drop not in body:
                        return time.monotonic() - t0
                self.cv.wait(0.1)
        return None

    def digest(self, seconds: float = 60, limit: int = 25) -> str:
        """What is going on right now, in words, for the brain."""
        with self.lock:
            f, box = dict(self.front), self.box
            age = time.monotonic() - self.refreshed if self.refreshed else None
            nui, nl, dirty = len(self.ui), len(self.lines), bool(self.dirty)
            evs = [dict(e) for e in self.events][-limit * 3:]
        cut = time.time() - seconds
        evs = [e for e in evs if e["t"] >= cut][-limit:]
        out = []
        if f:
            out.append(f"front: {f.get('app', '')} {f.get('title', '')[:80]!r} "
                       f"area {box[2]}x{box[3]} at ({box[0]},{box[1]}); "
                       f"{nui} controls + {nl} text lines cached"
                       + (f", refreshed {age:.1f}s ago" if age is not None else "")
                       + ("; parts are changing right now" if dirty else "; still"))
        try:
            wins = [w for w in screen_hands.windows() if w["pid"] != _ME]
            out.append("open (top first): " + " | ".join(
                f"{w['title'][:45]}" + (" [min]" if w["minimized"] else "")
                for w in wins[:10]))
        except Exception:  # noqa: BLE001
            pass
        if evs:
            out.append(f"last {int(seconds)}s:")
            for e in evs:
                n = f" (x{e['n']})" if e.get("n", 1) > 1 else ""
                out.append(f"  {time.strftime('%H:%M:%S', time.localtime(e['t']))} "
                           f"{e['text']}{n}")
        else:
            out.append(f"nothing happened in the last {int(seconds)}s")
        st = list(self.stats)
        if st:
            out.append(f"(eyes: {self.capture}, {1 / TICK_S:.0f} checks/s, "
                       f"tick {sum(st) / len(st):.1f} ms"
                       + (f", refresh ~{int(sum(self.refresh_ms) / len(self.refresh_ms))} ms"
                          if self.refresh_ms else "")
                       + (f", last error: {self.error}" if self.error else "") + ")")
        return "\n".join(out)


_EYES: LiveEyes | None = None


def start() -> LiveEyes | None:
    """Start once per process. Off with GOAT_LIVE_EYES=off."""
    global _EYES
    if os.environ.get("GOAT_LIVE_EYES", "").lower() in ("off", "0", "false"):
        return None
    if _EYES is None:
        _EYES = LiveEyes().start()
    return _EYES


def get() -> LiveEyes | None:
    return _EYES if _EYES and _EYES.running else None


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 10
    import psutil
    proc = psutil.Process()
    proc.cpu_percent()
    eyes = LiveEyes()
    # Outside GOAT, GOAT's own window would be "the front": skip it.
    eyes.skip_pids |= {w["pid"] for w in screen_hands.windows()
                       if w["title"].strip() == "GOAT"}
    _EYES = eyes.start()
    time.sleep(secs)
    cpu = proc.cpu_percent()
    print(eyes.digest(seconds=secs + 5))
    print(f"cpu {cpu:.1f}% of one core over {secs:.0f}s, "
          f"rss {proc.memory_info().rss // 2**20} MB, version {eyes.version}")
