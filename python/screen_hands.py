"""GOAT's eyes and hands on the screen itself.

Until now GOAT drove the machine through the terminal: shell commands, file
writes, `start` to open an app. That covers anything with a CLI and nothing
else. Everything that only exists as pixels — a settings toggle, a web app, a
dialog, a game, a program with no flags — was out of reach, and GOAT had to
say so out loud (2026-09-14, Giorgi: "make sure to achieve this").

This module is the missing organ: it SEES the screen (real screenshots handed
to the model as images) and it MOVES the real mouse and keyboard through Win32
SendInput — the same input path a human's hardware uses, so every app believes
it.

Design notes that matter:
- COORDINATES ARE REAL PHYSICAL SCREEN PIXELS, everywhere, always. Screenshots
  get downscaled to keep the token bill sane, so the picture the model sees is
  smaller than the screen. Rather than make the model undo that in its head (it
  will get it wrong), captures carry a grid whose labels are already in real
  coordinates. Read a label, click that number.
- Multi-monitor safe: the virtual desktop can start at negative coordinates (a
  second monitor to the left), so nothing here assumes 0,0 is the origin.
- DPI: the process must be per-monitor DPI aware or GetCursorPos and the
  capture disagree by the display scale factor. Qt already sets that for the
  app; dpi_init() covers the standalone-CLI case and is harmless twice.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import io
import os
import threading
import time

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# Hands can be parked without touching code: GOAT_SCREEN=off. Eyes stay open —
# looking is never the dangerous half.
ENABLED = os.environ.get("GOAT_SCREEN", "on").strip().lower() not in (
    "off", "0", "no", "false")

# One mutex over the whole organ. Two tool calls racing SendInput interleave
# their keystrokes into garbage, and mss instances are not thread safe.
_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------

def dpi_init() -> None:
    """Make this process per-monitor DPI aware (idempotent, never raises)."""
    try:
        # -4 == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except AttributeError:
        pass  # pre-1703 Windows
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 — "already set by Qt" is the common case
        try:
            _user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# SendInput plumbing
# --------------------------------------------------------------------------

ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x8000, 0x4000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x0004, 0x0008
WHEEL_DELTA = 120

_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _send(*inputs: _INPUT) -> int:
    arr = (_INPUT * len(inputs))(*inputs)
    n = _user32.SendInput(len(inputs), ctypes.byref(arr), ctypes.sizeof(_INPUT))
    if n != len(inputs):
        raise OSError(f"SendInput sent {n}/{len(inputs)} "
                      f"(err {ctypes.get_last_error()})")
    return n


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    i = _INPUT(type=INPUT_MOUSE)
    i.mi = _MOUSEINPUT(dx, dy, ctypes.c_uint32(data).value, flags, 0, 0)
    return i


def _key_input(vk: int, scan: int, flags: int) -> _INPUT:
    i = _INPUT(type=INPUT_KEYBOARD)
    i.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


def _guard() -> None:
    if not ENABLED:
        raise RuntimeError("screen hands are disabled (GOAT_SCREEN=off)")


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def virtual_screen() -> tuple[int, int, int, int]:
    """(left, top, width, height) of the whole desktop across all monitors."""
    g = _user32.GetSystemMetrics
    return (g(SM_XVIRTUALSCREEN), g(SM_YVIRTUALSCREEN),
            g(SM_CXVIRTUALSCREEN), g(SM_CYVIRTUALSCREEN))


def _abs_coords(x: int, y: int) -> tuple[int, int]:
    """Real pixels -> the 0..65535 normalized space SendInput's absolute mode
    wants. Normalized against the VIRTUAL desktop, so a monitor hanging off to
    the left (negative x) still lands correctly."""
    vx, vy, vw, vh = virtual_screen()
    nx = int(round((x - vx) * 65535 / max(vw - 1, 1)))
    ny = int(round((y - vy) * 65535 / max(vh - 1, 1)))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def cursor_pos() -> tuple[int, int]:
    pt = wt.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def monitors() -> list[dict]:
    """Every monitor as {index,left,top,width,height,primary}, index 1..n.

    Built on mss so the numbering matches capture(monitor=N) exactly.
    """
    import mss
    out = []
    with mss.mss() as sct:
        for i, m in enumerate(sct.monitors[1:], start=1):
            out.append({"index": i, "left": m["left"], "top": m["top"],
                        "width": m["width"], "height": m["height"],
                        "primary": m["left"] == 0 and m["top"] == 0})
    return out


# --------------------------------------------------------------------------
# Eyes
# --------------------------------------------------------------------------

def _grab(region: tuple[int, int, int, int] | None, monitor: int | None):
    """Raw pixels as a PIL RGB image, plus the real-screen box it came from."""
    import mss
    from PIL import Image
    with mss.mss() as sct:
        if region:
            left, top, width, height = region
            box = {"left": int(left), "top": int(top),
                   "width": max(1, int(width)), "height": max(1, int(height))}
        elif monitor:
            mons = sct.monitors
            if not 1 <= monitor < len(mons):
                raise ValueError(f"no monitor {monitor} (have {len(mons) - 1})")
            box = mons[monitor]
        else:
            box = sct.monitors[0]  # the whole virtual desktop
        raw = sct.grab(box)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    return img, (box["left"], box["top"], box["width"], box["height"])


def _draw_grid(img, origin: tuple[int, int], scale: float, step: int) -> None:
    """Overlay a coordinate grid whose labels are REAL screen pixels.

    This is what keeps the model honest about aiming: it never has to undo the
    downscale in its head, it reads a number off the picture and clicks it.
    """
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img, "RGBA")
    try:
        font = ImageFont.truetype("consola.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    ox, oy = origin
    w, h = img.size

    first_x = ((ox + step - 1) // step) * step
    for real in range(first_x, ox + int(w / scale) + 1, step):
        px = int((real - ox) * scale)
        if not 0 <= px < w:
            continue
        draw.line([(px, 0), (px, h)], fill=(255, 0, 90, 70), width=1)
        draw.rectangle([px + 1, 1, px + 46, 16], fill=(0, 0, 0, 150))
        draw.text((px + 3, 2), str(real), fill=(255, 210, 0, 255), font=font)

    first_y = ((oy + step - 1) // step) * step
    for real in range(first_y, oy + int(h / scale) + 1, step):
        py = int((real - oy) * scale)
        if not 0 <= py < h:
            continue
        draw.line([(0, py), (w, py)], fill=(255, 0, 90, 70), width=1)
        draw.rectangle([1, py + 1, 46, py + 16], fill=(0, 0, 0, 150))
        draw.text((3, py + 2), str(real), fill=(255, 210, 0, 255), font=font)


def _draw_cursor(img, origin: tuple[int, int], scale: float) -> None:
    from PIL import ImageDraw
    cx, cy = cursor_pos()
    px, py = int((cx - origin[0]) * scale), int((cy - origin[1]) * scale)
    if not (0 <= px < img.size[0] and 0 <= py < img.size[1]):
        return
    d = ImageDraw.Draw(img, "RGBA")
    d.ellipse([px - 9, py - 9, px + 9, py + 9], outline=(0, 230, 255, 255), width=2)
    d.line([(px - 14, py), (px + 14, py)], fill=(0, 230, 255, 220), width=1)
    d.line([(px, py - 14), (px, py + 14)], fill=(0, 230, 255, 220), width=1)


def _auto_step(scale: float) -> int:
    """Grid spacing that lands near 90 drawn pixels — dense enough to aim by,
    sparse enough not to smother the screenshot."""
    target = 90 / max(scale, 0.01)
    for step in (50, 100, 200, 250, 500, 1000):
        if step >= target:
            return step
    return 1000


def capture(region: tuple[int, int, int, int] | None = None,
            monitor: int | None = None,
            max_edge: int = 1400,
            grid: bool = True,
            cursor: bool = True,
            grid_step: int | None = None) -> tuple[bytes, dict]:
    """Screenshot as PNG bytes + metadata describing the real-pixel mapping."""
    with _LOCK:
        img, (left, top, width, height) = _grab(region, monitor)
        scale = 1.0
        if max_edge and max(img.size) > max_edge:
            scale = max_edge / max(img.size)
            img = img.resize((max(1, int(img.size[0] * scale)),
                              max(1, int(img.size[1] * scale))),
                             resample=1)  # BILINEAR — keeps small text legible
        step = grid_step or _auto_step(scale)
        if cursor:
            _draw_cursor(img, (left, top), scale)
        if grid:
            _draw_grid(img, (left, top), scale, step)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        cx, cy = cursor_pos()
        return buf.getvalue(), {
            "region": {"left": left, "top": top, "width": width, "height": height},
            "image_size": {"width": img.size[0], "height": img.size[1]},
            "scale": round(scale, 4),
            "grid_step": step if grid else None,
            "cursor": {"x": cx, "y": cy},
        }


# --------------------------------------------------------------------------
# Hands — mouse
# --------------------------------------------------------------------------

def move(x: int, y: int) -> tuple[int, int]:
    """Put the pointer at real pixel (x, y) and confirm it landed there."""
    _guard()
    with _LOCK:
        nx, ny = _abs_coords(x, y)
        _send(_mouse_input(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            nx, ny))
        time.sleep(0.012)
        got = cursor_pos()
        # Normalized absolute mode rounds; on odd virtual-desktop sizes that can
        # miss by a pixel or two. Nudge with SetCursorPos rather than hand back
        # a click that lands next to the button.
        if abs(got[0] - x) > 2 or abs(got[1] - y) > 2:
            _user32.SetCursorPos(int(x), int(y))
            time.sleep(0.008)
            got = cursor_pos()
        return got


def click(x: int | None = None, y: int | None = None, button: str = "left",
          clicks: int = 1, delay: float = 0.06) -> tuple[int, int]:
    _guard()
    if button not in _BUTTONS:
        raise ValueError(f"button must be one of {sorted(_BUTTONS)}")
    with _LOCK:
        if x is not None and y is not None:
            move(x, y)
            time.sleep(0.03)  # let hover/focus settle before the press lands
        down, up = _BUTTONS[button]
        for i in range(max(1, clicks)):
            if i:
                time.sleep(delay)
            _send(_mouse_input(down), _mouse_input(up))
        return cursor_pos()


def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left",
         steps: int = 22) -> tuple[int, int]:
    """Press at one point, glide, release at another.

    The glide matters: apps that implement drag with mouse-move handlers (file
    managers, canvases, sliders) ignore a teleport from press to release.
    """
    _guard()
    if button not in _BUTTONS:
        raise ValueError(f"button must be one of {sorted(_BUTTONS)}")
    with _LOCK:
        down, up = _BUTTONS[button]
        move(x1, y1)
        time.sleep(0.05)
        _send(_mouse_input(down))
        time.sleep(0.05)
        for i in range(1, max(1, steps) + 1):
            t = i / max(1, steps)
            move(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
            time.sleep(0.012)
        time.sleep(0.05)
        _send(_mouse_input(up))
        return cursor_pos()


def scroll(clicks: int, x: int | None = None, y: int | None = None,
           horizontal: bool = False) -> tuple[int, int]:
    """Wheel notches: positive = up / right, negative = down / left."""
    _guard()
    with _LOCK:
        if x is not None and y is not None:
            move(x, y)
            time.sleep(0.03)
        flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
        for _ in range(abs(int(clicks))):
            _send(_mouse_input(flag, data=WHEEL_DELTA * (1 if clicks > 0 else -1)))
            time.sleep(0.02)
        return cursor_pos()


# --------------------------------------------------------------------------
# Hands — keyboard
# --------------------------------------------------------------------------

VK = {
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D, "menukey": 0x5D,
    "numlock": 0x90, "scrolllock": 0x91,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "medianext": 0xB0, "mediaprev": 0xB1, "mediastop": 0xB2, "mediaplay": 0xB3,
    "semicolon": 0xBA, "plus": 0xBB, "equals": 0xBB, "comma": 0xBC,
    "minus": 0xBD, "period": 0xBE, "slash": 0xBF, "backtick": 0xC0,
    "tilde": 0xC0, "lbracket": 0xDB, "backslash": 0xDC, "rbracket": 0xDD,
    "quote": 0xDE,
}
VK.update({f"f{n}": 0x6F + n for n in range(1, 25)})            # F1..F24
VK.update({c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"})
VK.update({d: ord(d) for d in "0123456789"})
VK.update({f"num{n}": 0x60 + n for n in range(10)})             # numpad 0..9

# Keys that live on the extended half of the keyboard. Without the extended
# flag Windows hands the app the numpad twin instead (arrows become 4/8/6/2,
# Delete becomes decimal point) — a classic silent SendInput bug.
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
             0x5B, 0x5C, 0x5D, 0x90, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}


def _vk(name: str) -> int:
    key = name.strip().lower()
    if key in VK:
        return VK[key]
    if len(key) == 1:
        scan = _user32.VkKeyScanW(ctypes.c_wchar(key))
        if scan != -1:
            return scan & 0xFF
    raise ValueError(f"unknown key {name!r}")


def _key_down(vk: int) -> _INPUT:
    return _key_input(vk, 0, KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)


def _key_up(vk: int) -> _INPUT:
    f = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
    return _key_input(vk, 0, f)


def press(combo: str, times: int = 1) -> str:
    """Press a key or chord: "enter", "ctrl+s", "win+shift+s", "alt+f4".

    Several chords in one call are space separated: "ctrl+a ctrl+c".
    """
    _guard()
    with _LOCK:
        done = []
        for _ in range(max(1, times)):
            for chord in combo.split():
                keys = [_vk(p) for p in chord.split("+") if p]
                if not keys:
                    continue
                _send(*[_key_down(k) for k in keys],
                      *[_key_up(k) for k in reversed(keys)])
                done.append(chord)
                time.sleep(0.04)
        return " ".join(done)


def _utf16_units(ch: str) -> list[int]:
    """UTF-16 code units for one character — a surrogate pair for emoji."""
    b = ch.encode("utf-16-le")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


def type_text(text: str, wpm: int = 0) -> int:
    """Type a literal string, Unicode included.

    KEYEVENTF_UNICODE bypasses the keyboard layout entirely, which is why this
    types Georgian on an English layout without switching anything.
    """
    _guard()
    with _LOCK:
        pause = 0.0 if not wpm else 60.0 / (wpm * 5)
        sent = 0
        for ch in text:
            if ch == "\n":
                _send(_key_down(0x0D), _key_up(0x0D))
            elif ch == "\t":
                _send(_key_down(0x09), _key_up(0x09))
            else:
                for unit in _utf16_units(ch):
                    _send(_key_input(0, unit, KEYEVENTF_UNICODE),
                          _key_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            sent += 1
            if pause:
                time.sleep(pause)
            elif sent % 40 == 0:
                time.sleep(0.01)  # let a slow text field catch up
        return sent


def pixel(x: int, y: int) -> tuple[int, int, int]:
    """RGB of one screen pixel — cheap way to confirm a state change."""
    hdc = _user32.GetDC(0)
    try:
        c = _gdi32.GetPixel(hdc, int(x), int(y))
        if c == 0xFFFFFFFF:
            raise OSError("GetPixel failed (point off-screen?)")
        return c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF
    finally:
        _user32.ReleaseDC(0, hdc)


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

_ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9
DWMWA_CLOAKED = 14


def _cloaked(hwnd) -> bool:
    """UWP keeps dead windows around — visible to EnumWindows, not on screen."""
    val = ctypes.c_int(0)
    try:
        ctypes.WinDLL("dwmapi").DwmGetWindowAttribute(
            wt.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:  # noqa: BLE001
        return False
    return bool(val.value)


def windows(all_windows: bool = False) -> list[dict]:
    """Visible top-level windows with real-pixel rects."""
    found: list[dict] = []

    def _cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0 and not all_windows:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if not all_windows and (not title.strip() or _cloaked(hwnd)):
            return True
        r = wt.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(r))
        if not all_windows and (r.right - r.left <= 0 or r.bottom - r.top <= 0):
            return True
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append({"hwnd": int(hwnd), "title": title, "pid": int(pid.value),
                      "left": r.left, "top": r.top,
                      "width": r.right - r.left, "height": r.bottom - r.top,
                      "minimized": bool(_user32.IsIconic(hwnd))})
        return True

    _user32.EnumWindows(_ENUMPROC(_cb), 0)
    return found


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """The window's CLIENT area in screen pixels: (left, top, width, height).

    GetWindowRect includes the frame and, on a maximized window, several
    pixels of invisible resize border. Anything that has to line up with what
    is drawn inside the window needs this instead.
    """
    r = wt.RECT()
    _user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(r))
    pt = wt.POINT(r.left, r.top)
    _user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(pt))
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


def _match(target: str) -> dict:
    target = str(target)
    if target.isdigit():
        hwnd = int(target)
        for w in windows(all_windows=True):
            if w["hwnd"] == hwnd:
                return w
        raise ValueError(f"no window with hwnd {hwnd}")
    t = target.strip().lower()
    wins = windows()
    for w in wins:  # exact title first, so "Settings" can't grab "Settings — Foo"
        if w["title"].strip().lower() == t:
            return w
    for w in wins:
        if t in w["title"].lower():
            return w
    raise ValueError(f"no visible window matching {target!r}")


def focus_window(target: str) -> dict:
    """Bring a window to the front and give it keyboard focus.

    Windows refuses SetForegroundWindow from a process that does not own the
    current foreground — the documented way through is to attach to the
    foreground thread's input queue for the duration of the call.
    """
    _guard()
    with _LOCK:
        w = _match(target)
        hwnd = w["hwnd"]
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW_RESTORE)
        fg = _user32.GetForegroundWindow()
        cur = _user32.GetWindowThreadProcessId(fg, None)
        own = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = bool(_user32.AttachThreadInput(own, cur, True)) if cur != own else False
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetFocus(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(own, cur, False)
        time.sleep(0.12)
        w["focused"] = _user32.GetForegroundWindow() == hwnd
        return w


def window_state(target: str, state: str) -> dict:
    """minimize | maximize | restore a window."""
    _guard()
    cmd = {"minimize": SW_MINIMIZE, "maximize": SW_MAXIMIZE,
           "restore": SW_RESTORE}.get(state)
    if cmd is None:
        raise ValueError("state must be minimize, maximize or restore")
    w = _match(target)
    _user32.ShowWindow(w["hwnd"], cmd)
    time.sleep(0.1)
    w["state"] = state
    return w


def foreground() -> dict | None:
    hwnd = _user32.GetForegroundWindow()
    for w in windows(all_windows=True):
        if w["hwnd"] == hwnd:
            return w
    return None


# --------------------------------------------------------------------------
# Getting out of GOAT's own way
# --------------------------------------------------------------------------

HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
GWL_EXSTYLE, WS_EX_TOPMOST = -20, 0x00000008

_aside: dict | None = None  # what to put back when GOAT steps back in


def self_window() -> dict | None:
    """GOAT's own window.

    Inside the app this is a window owned by this very process; from the CLI
    there is no such window, so fall back to the title. Either way GOAT has to
    be able to find itself before it can step aside.
    """
    own = os.getpid()
    named = None
    for w in windows():
        if w["pid"] == own:
            return w
        if w["title"].strip() == "GOAT" and named is None:
            named = w
    return named


def is_topmost(hwnd: int) -> bool:
    return bool(_user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)


def set_topmost(hwnd: int, on: bool) -> None:
    _user32.SetWindowPos(hwnd, HWND_TOPMOST if on else HWND_NOTOPMOST,
                         0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def stand_aside(minimize: bool = True) -> dict:
    """Clear GOAT's window out of the way of whatever it is about to drive.

    GOAT floats always-on-top. That is right for a companion and fatal for a
    hand: a click aimed at an app underneath lands on GOAT instead, and the
    screenshot shows GOAT rather than the thing being worked on (measured
    2026-09-14 — a click at (308,108) meant for a browser field hit GOAT's
    panel and typed into nothing). Dropping topmost is the part that matters;
    minimizing also frees the pixels.
    """
    global _aside
    w = self_window()
    if not w:
        return {"aside": False, "reason": "GOAT's own window was not found"}
    if _aside is None:
        _aside = {"hwnd": w["hwnd"], "topmost": is_topmost(w["hwnd"]),
                  "minimized": w["minimized"]}
    set_topmost(w["hwnd"], False)
    if minimize and not w["minimized"]:
        _user32.ShowWindow(w["hwnd"], SW_MINIMIZE)
    time.sleep(0.2)  # let the desktop repaint before anyone screenshots it
    return {"aside": True, "hwnd": w["hwnd"], "minimized": bool(minimize),
            "was_topmost": _aside["topmost"]}


def step_back_in() -> dict:
    """Undo stand_aside — exactly as GOAT was, on-top state included."""
    global _aside
    w = self_window()
    if not w:
        return {"restored": False, "reason": "GOAT's own window was not found"}
    want = _aside or {"topmost": True, "minimized": False}
    if not want.get("minimized"):
        _user32.ShowWindow(w["hwnd"], SW_RESTORE)
    set_topmost(w["hwnd"], bool(want.get("topmost", True)))
    _aside = None
    time.sleep(0.15)
    return {"restored": True, "hwnd": w["hwnd"],
            "topmost": bool(want.get("topmost", True))}


dpi_init()


# --------------------------------------------------------------------------
# CLI — the same organ, testable from a terminal and not only from the model
# --------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    import argparse
    import sys
    # Georgian window and tab titles are routine here; the Windows
    # console is cp1252 and would raise on the first one.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - piped output is fine as is
            pass
    import json
    p = argparse.ArgumentParser(prog="screen_hands",
                                description="GOAT screen eyes and hands")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("shot", help="capture a screenshot to a file")
    s.add_argument("--out", default="screen.png")
    s.add_argument("--region", help="left,top,width,height in real pixels")
    s.add_argument("--monitor", type=int)
    s.add_argument("--max-edge", type=int, default=1400)
    s.add_argument("--no-grid", action="store_true")

    c = sub.add_parser("click")
    c.add_argument("x", type=int)
    c.add_argument("y", type=int)
    c.add_argument("--button", default="left")
    c.add_argument("--clicks", type=int, default=1)

    m = sub.add_parser("move")
    m.add_argument("x", type=int)
    m.add_argument("y", type=int)

    d = sub.add_parser("drag")
    for arg in ("x1", "y1", "x2", "y2"):
        d.add_argument(arg, type=int)

    sc = sub.add_parser("scroll")
    sc.add_argument("clicks", type=int)
    sc.add_argument("--x", type=int)
    sc.add_argument("--y", type=int)
    sc.add_argument("--horizontal", action="store_true")

    t = sub.add_parser("type")
    t.add_argument("text")
    k = sub.add_parser("key")
    k.add_argument("combo")
    k.add_argument("--times", type=int, default=1)
    px = sub.add_parser("pixel")
    px.add_argument("x", type=int)
    px.add_argument("y", type=int)
    sub.add_parser("cursor")
    sub.add_parser("monitors")
    sub.add_parser("windows")
    sub.add_parser("foreground")
    ap = sub.add_parser("aside", help="move GOAT's own window out of the way")
    ap.add_argument("--no-minimize", action="store_true")
    sub.add_parser("back", help="put GOAT's own window back")
    f = sub.add_parser("focus")
    f.add_argument("target")
    ws = sub.add_parser("window")
    ws.add_argument("target")
    ws.add_argument("state")

    a = p.parse_args(argv)
    if a.cmd == "shot":
        region = tuple(int(v) for v in a.region.split(",")) if a.region else None
        png, meta = capture(region=region, monitor=a.monitor,
                            max_edge=a.max_edge, grid=not a.no_grid)
        with open(a.out, "wb") as fh:
            fh.write(png)
        meta["file"] = os.path.abspath(a.out)
        meta["bytes"] = len(png)
        print(json.dumps(meta, indent=2))
    elif a.cmd == "click":
        print(click(a.x, a.y, a.button, a.clicks))
    elif a.cmd == "move":
        print(move(a.x, a.y))
    elif a.cmd == "drag":
        print(drag(a.x1, a.y1, a.x2, a.y2))
    elif a.cmd == "scroll":
        print(scroll(a.clicks, a.x, a.y, a.horizontal))
    elif a.cmd == "type":
        print(type_text(a.text), "chars")
    elif a.cmd == "key":
        print(press(a.combo, a.times))
    elif a.cmd == "pixel":
        print(pixel(a.x, a.y))
    elif a.cmd == "cursor":
        print(cursor_pos())
    elif a.cmd == "monitors":
        print(json.dumps(monitors(), indent=2))
    elif a.cmd == "windows":
        print(json.dumps(windows(), indent=2, ensure_ascii=False))
    elif a.cmd == "foreground":
        print(json.dumps(foreground(), indent=2, ensure_ascii=False))
    elif a.cmd == "aside":
        print(json.dumps(stand_aside(not a.no_minimize), ensure_ascii=False))
    elif a.cmd == "back":
        print(json.dumps(step_back_in(), ensure_ascii=False))
    elif a.cmd == "focus":
        print(json.dumps(focus_window(a.target), ensure_ascii=False))
    elif a.cmd == "window":
        print(json.dumps(window_state(a.target, a.state), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
