"""Hands that games actually feel, plus the two rules around them.

His order, 2026-10-21: "I want it to be able to play games on my laptop."
screen_hands types with virtual-key codes, which is what desktop apps read —
but most games read the keyboard through DirectInput / raw input, which only
sees hardware SCANCODES, and read the mouse as raw relative motion (an
absolute jump of the cursor does nothing to an FPS camera). So:

  * keys go out as scancodes (KEYEVENTF_SCANCODE), held for real durations;
  * the mouse can move relatively (camera/aim) as well as absolutely;
  * a tiny sequence language lets one call do "w:800 space a+d:300 lclick".

Rule 1 — HE OUTRANKS GOAT. While GOAT plays, a low-level hook watches for
input that did NOT come from SendInput (the injected flag). The moment he
touches the keyboard or moves the mouse, everything GOAT holds is released
and the play stops.

Rule 2 — NO ONLINE CHEATING. GOAT won't drive a game protected by
anti-cheat (BattlEye, Easy Anti-Cheat, Vanguard, FACEIT, ...): automated
input there is cheating on real players and gets HIS account banned.
Single-player, offline and browser games are fair game.
"""
from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from ctypes import wintypes as wt

import screen_hands as sh

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ------------------------------------------------------------- scancodes --
# Set-1 make codes. (code, extended)
SC = {"esc": 0x01, "escape": 0x01, "minus": 0x0C, "equals": 0x0D,
      "backspace": 0x0E, "tab": 0x0F, "lbracket": 0x1A, "rbracket": 0x1B,
      "enter": 0x1C, "return": 0x1C, "ctrl": 0x1D, "lctrl": 0x1D,
      "semicolon": 0x27, "quote": 0x28, "backtick": 0x29, "tilde": 0x29,
      "shift": 0x2A, "lshift": 0x2A, "backslash": 0x2B, "comma": 0x33,
      "period": 0x34, "slash": 0x35, "rshift": 0x36, "alt": 0x38,
      "lalt": 0x38, "space": 0x39, "capslock": 0x3A, "f11": 0x57,
      "f12": 0x58, "num7": 0x47, "num8": 0x48, "num9": 0x49, "num4": 0x4B,
      "num5": 0x4C, "num6": 0x4D, "num1": 0x4F, "num2": 0x50, "num3": 0x51,
      "num0": 0x52}
SC.update({str(d): 0x02 + i for i, d in enumerate("123456789")})
SC["0"] = 0x0B
SC.update({c: 0x10 + i for i, c in enumerate("qwertyuiop")})
SC.update({c: 0x1E + i for i, c in enumerate("asdfghjkl")})
SC.update({c: 0x2C + i for i, c in enumerate("zxcvbnm")})
SC.update({f"f{n}": 0x3A + n for n in range(1, 11)})
SC_EXT = {"up": 0x48, "down": 0x50, "left": 0x4B, "right": 0x4D,
          "insert": 0x52, "delete": 0x53, "del": 0x53, "home": 0x47,
          "end": 0x4F, "pageup": 0x49, "pagedown": 0x51, "rctrl": 0x1D,
          "ralt": 0x38}

KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8
MOUSEEVENTF_MOVE = 0x0001

_held_keys: set = set()
_held_buttons: set = set()
_hold_lock = threading.RLock()


def _scan(name: str) -> tuple[int, bool]:
    k = name.strip().lower()
    if k in SC:
        return SC[k], False
    if k in SC_EXT:
        return SC_EXT[k], True
    raise ValueError(f"unknown game key {name!r}")


def key_down(name: str):
    code, ext = _scan(name)
    sh._send(sh._key_input(0, code, KEYEVENTF_SCANCODE
                           | (KEYEVENTF_EXTENDEDKEY if ext else 0)))
    with _hold_lock:
        _held_keys.add(name.lower())


def key_up(name: str):
    code, ext = _scan(name)
    sh._send(sh._key_input(0, code, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP
                           | (KEYEVENTF_EXTENDEDKEY if ext else 0)))
    with _hold_lock:
        _held_keys.discard(name.lower())


def button_down(b: str = "left"):
    sh._send(sh._mouse_input(sh._BUTTONS[b][0]))
    with _hold_lock:
        _held_buttons.add(b)


def button_up(b: str = "left"):
    sh._send(sh._mouse_input(sh._BUTTONS[b][1]))
    with _hold_lock:
        _held_buttons.discard(b)


def click_at(x: int, y: int, button: str = "left"):
    """Game-speed click: teleport + press, ~15 ms. screen_hands.click is
    built for desktop apps (hover settle, glass toggles) and costs ~120 ms,
    which capped the autopilot at 6 fps."""
    nx, ny = sh._abs_coords(x, y)
    sh._send(sh._mouse_input(sh.MOUSEEVENTF_MOVE | sh.MOUSEEVENTF_ABSOLUTE
                             | sh.MOUSEEVENTF_VIRTUALDESK, nx, ny))
    time.sleep(0.008)
    down, up = sh._BUTTONS[button]
    sh._send(sh._mouse_input(down))
    time.sleep(0.008)
    sh._send(sh._mouse_input(up))


def mouse_rel(dx: int, dy: int, ms: int = 60):
    """Raw relative motion — what turns a game camera. Spread over `ms` in
    small steps, because one 400-pixel jump reads as a teleport."""
    steps = max(1, min(30, ms // 8))
    sx = sy = 0
    for i in range(1, steps + 1):
        tx, ty = round(dx * i / steps), round(dy * i / steps)
        sh._send(sh._mouse_input(MOUSEEVENTF_MOVE, tx - sx, ty - sy))
        sx, sy = tx, ty
        if steps > 1:
            time.sleep(ms / 1000 / steps)


def release_all() -> list:
    """Let go of everything GOAT is holding. Always safe to call."""
    with _hold_lock:
        keys, buttons = list(_held_keys), list(_held_buttons)
    for k in keys:
        try:
            key_up(k)
        except Exception:  # noqa: BLE001
            pass
    for b in buttons:
        try:
            button_up(b)
        except Exception:  # noqa: BLE001
            pass
    return keys + buttons


# -------------------------------------------------------------- sequence --

_TOKEN = re.compile(
    r"^(?:(?P<wait>wait):(?P<wms>\d+)"
    r"|(?P<dir>down|up):(?P<dkey>[a-z][\w+]*)"
    r"|(?P<mouse>mouse|look):(?P<dx>[+-]?\d+),(?P<dy>[+-]?\d+)(?::(?P<mms>\d+))?"
    r"|(?P<btn>[lrm]click)(?::(?P<bms>\d+))?"
    r"|(?P<keys>[\w+]+?)(?::(?P<kms>\d+))?)(?:\*(?P<rep>\d+))?$", re.I)
MAX_SEQ_S = 30.0


def parse(seq: str) -> list[dict]:
    """'w:800 space*3 a+d:300 wait:200 lclick mouse:+120,-30 down:shift'
    -> steps. Raises ValueError on anything it can't read (nothing runs)."""
    steps, total = [], 0.0
    for tok in seq.split():
        m = _TOKEN.match(tok)
        if not m:
            raise ValueError(f"can't read {tok!r}")
        rep = int(m.group("rep") or 1)
        if m.group("wait"):
            st = {"op": "wait", "ms": int(m.group("wms"))}
        elif m.group("dir"):
            for k in m.group("dkey").split("+"):
                _scan(k)
            st = {"op": m.group("dir").lower(), "keys": m.group("dkey").split("+")}
        elif m.group("mouse"):
            st = {"op": "mouse", "dx": int(m.group("dx")), "dy": int(m.group("dy")),
                  "ms": int(m.group("mms") or 60)}
        elif m.group("btn"):
            st = {"op": "click", "button": {"l": "left", "r": "right",
                                            "m": "middle"}[m.group("btn")[0].lower()],
                  "ms": int(m.group("bms") or 40)}
        else:
            keys = m.group("keys").split("+")
            for k in keys:
                _scan(k)
            st = {"op": "keys", "keys": keys, "ms": int(m.group("kms") or 50)}
        total += rep * (st.get("ms", 0) + 30) / 1000
        steps += [st] * rep
    if total > MAX_SEQ_S:
        raise ValueError(f"sequence runs {total:.0f}s; keep one call under "
                         f"{MAX_SEQ_S:.0f}s (use the autopilot for longer play)")
    return steps


def run(seq: str, stop: threading.Event | None = None) -> str:
    """Play a sequence. `stop` (or his own input via HumanWatch) cuts it
    short and releases everything."""
    steps = parse(seq)
    sh._guard()
    done = 0
    try:
        for st in steps:
            if stop is not None and stop.is_set():
                return f"stopped after {done}/{len(steps)} steps"
            op = st["op"]
            if op == "wait":
                _sleep(st["ms"], stop)
            elif op == "down":
                for k in st["keys"]:
                    key_down(k)
            elif op == "up":
                for k in st["keys"]:
                    key_up(k)
            elif op == "mouse":
                mouse_rel(st["dx"], st["dy"], st["ms"])
            elif op == "click":
                button_down(st["button"])
                _sleep(st["ms"], stop)
                button_up(st["button"])
            else:
                for k in st["keys"]:
                    key_down(k)
                _sleep(st["ms"], stop)
                for k in reversed(st["keys"]):
                    key_up(k)
            done += 1
            time.sleep(0.02)
    finally:
        # down: without a matching up: stays held on purpose (walk while
        # the next call aims) — but a cut-short sequence lets go of all.
        if stop is not None and stop.is_set():
            release_all()
    held = sorted(_held_keys | _held_buttons)
    return (f"played {done} steps" + (f"; still holding {', '.join(held)}"
                                      if held else ""))


def _sleep(ms: int, stop):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        if stop is not None and stop.is_set():
            return
        time.sleep(min(0.01, max(0, end - time.monotonic())))


# ----------------------------------------------------------- human watch --

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_KEYDOWN, WM_SYSKEYDOWN, WM_MOUSEMOVE, WM_QUIT = 0x100, 0x104, 0x200, 0x12
LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01


class _KBD(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD),
                ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MSL(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM,
                               wt.LPARAM)
_user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wt.HINSTANCE,
                                      wt.DWORD]
_user32.SetWindowsHookExW.restype = wt.HHOOK


class HumanWatch:
    """Fires `on_human` once when he types or moves the mouse himself.
    Injected events (GOAT's own SendInput) carry a flag and are ignored."""

    MOVE_PX = 30          # a nudge of the desk isn't an intention

    def __init__(self, on_human):
        self.on_human = on_human
        self.fired = threading.Event()
        self.reason = ""
        self._tid = None
        self._ready = threading.Event()
        self._start_pt = None
        self._ok = False

    def start(self) -> bool:
        threading.Thread(target=self._loop, daemon=True,
                         name="human-watch").start()
        self._ready.wait(2.0)
        return self._ok

    def stop(self):
        if self._tid:
            _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._tid = None

    def _fire(self, why: str):
        if not self.fired.is_set():
            self.reason = why
            self.fired.set()
            try:
                self.on_human(why)
            except Exception:  # noqa: BLE001
                pass

    def _loop(self):
        self._tid = _kernel32.GetCurrentThreadId()

        def kb(n, w, l):
            if n == 0 and w in (WM_KEYDOWN, WM_SYSKEYDOWN):
                k = ctypes.cast(l, ctypes.POINTER(_KBD)).contents
                if not k.flags & LLKHF_INJECTED:
                    self._fire("he pressed a key")
            return _user32.CallNextHookEx(None, n, w, l)

        def ms(n, w, l):
            if n == 0:
                m = ctypes.cast(l, ctypes.POINTER(_MSL)).contents
                if not m.flags & LLMHF_INJECTED:
                    if w != WM_MOUSEMOVE:
                        self._fire("he clicked")
                    else:
                        p = (m.pt.x, m.pt.y)
                        if self._start_pt is None:
                            self._start_pt = p
                        elif (abs(p[0] - self._start_pt[0])
                              + abs(p[1] - self._start_pt[1]) > self.MOVE_PX):
                            self._fire("he moved the mouse")
            return _user32.CallNextHookEx(None, n, w, l)

        self._kb, self._ms = _HOOKPROC(kb), _HOOKPROC(ms)   # keep alive
        h1 = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb, None, 0)
        h2 = _user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms, None, 0)
        self._ok = bool(h1 and h2)
        self._ready.set()
        msg = wt.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        for h in (h1, h2):
            if h:
                _user32.UnhookWindowsHookEx(h)


# ------------------------------------------------------------ anti-cheat --

ANTICHEAT_PROCS = {
    "beservice.exe": "BattlEye", "beservice_x64.exe": "BattlEye",
    "easyanticheat.exe": "Easy Anti-Cheat",
    "easyanticheat_eos.exe": "Easy Anti-Cheat",
    "vgc.exe": "Riot Vanguard", "vgtray.exe": "Riot Vanguard",
    "faceit.exe": "FACEIT", "faceitservice.exe": "FACEIT",
    "eaanticheat.gameservice.exe": "EA Javelin",
    "eaanticheat.gameservicelauncher.exe": "EA Javelin",
    "pnkbstra.exe": "PunkBuster", "pnkbstrb.exe": "PunkBuster",
    "gamemon.des": "nProtect GameGuard", "gamemon64.des": "nProtect GameGuard",
    "xxd-0.xem": "XIGNCODE", "sguard64.exe": "ACE", "ace-base.exe": "ACE",
}
# Online competitive games, by exe stem or window title fragment.
ONLINE_GAMES = (
    "rainbowsix", "rainbow six", "valorant", "cs2", "csgo",
    "counter-strike", "fortnite", "r5apex", "apex legends", "tslgame",
    "pubg", "overwatch", "league of legends", "leagueclient", "dota2",
    "dota 2", "rocketleague", "rocket league", "modernwarfare", "cod.exe",
    "call of duty", "warzone", "blackops", "destiny2", "escapefromtarkov",
    "tarkov", "rustclient", "deadbydaylight", "dead by daylight", "gta5",
    "gtav", "thefinals", "the finals", "marvel-win64", "marvel rivals",
    "bf2042", "battlefield", "halo infinite", "haloinfinite", "splitgate",
    "xdefiant", "fc25", "fc26", "ea sports fc", "brawlhalla", "paladins",
    "smite", "warframe", "lostark", "lost ark", "newworld", "new world",
    "wow.exe", "world of warcraft", "ffxiv", "osu!", "osu.exe",
    "roblox", "minecraft server", "hunt showdown", "huntgame",
    "deadlock", "naraka", "finalsclient", "eldenring",
)


def anticheat_block(win: dict | None) -> str | None:
    """Why GOAT must not drive this window, or None if it may."""
    import psutil
    if not win:
        return None
    title = (win.get("title") or "").lower()
    exe, stem = "", ""
    try:
        p = psutil.Process(win["pid"])
        exe = (p.exe() or "").lower()
        stem = os.path.splitext(os.path.basename(exe))[0]
    except Exception:  # noqa: BLE001
        pass
    for g in ONLINE_GAMES:
        if g in stem or g in title:
            return (f"{win.get('title', stem)[:50]!r} is an online game with "
                    "anti-cheat")
    if exe:
        d = os.path.dirname(exe)
        for up in (d, os.path.dirname(d), os.path.dirname(os.path.dirname(d))):
            try:
                names = {n.lower() for n in os.listdir(up)}
            except OSError:
                continue
            for ac in ("battleye", "easyanticheat", "easyanticheat_eos"):
                if ac in names:
                    return f"this game ships with {ac} anti-cheat"
    browsers = ("brave", "chrome", "msedge", "firefox", "opera", "vivaldi")
    if stem in browsers or stem in ("explorer", "python", "pythonw"):
        return None
    try:
        running = {(q.info.get("name") or "").lower()
                   for q in psutil.process_iter(["name"])}
    except Exception:  # noqa: BLE001
        running = set()
    for name, ac in ANTICHEAT_PROCS.items():
        if name in running:
            return f"{ac} anti-cheat is running alongside it"
    return None


REFUSAL = ("I won't drive that one: {why}. Bot input in online games is "
           "cheating on real players and gets your account banned. "
           "Single-player, offline and browser games I'll happily play.")
