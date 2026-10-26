"""Laptop controls behind the phone's Control tab (2026-10-17, his order:
"make the controls side as perfect as it gets, better UI/UX").

Everything here is blocking; phone_server runs it via asyncio.to_thread.
Each reader degrades to None on failure so one broken control never blanks
the tab. Volume and mute go through the real audio endpoint (pycaw), so the
phone shows the true level instead of guessing from key presses.
"""
import ctypes
import os
import subprocess
import threading
import time

_NOWIN = 0x08000000
_tls = threading.local()
_user32 = ctypes.windll.user32
_MEDIA_INIT_LOCK = threading.Lock()
_MEDIA_READY = threading.Event()
_MEDIA_LOOP = None
_MEDIA_THREAD = None
_MEDIA_ASYNC_LOCK = None

# Chords the phone may send. Anything else is refused: the remote is a
# convenience, not a way around the close guard.
CHORDS = {
    "alt+tab", "win+d", "win+tab", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z",
    "ctrl+y", "ctrl+a", "ctrl+s", "ctrl+f", "ctrl+t", "ctrl+shift+t",
    "ctrl+l", "ctrl+r", "alt+left", "alt+right", "f5", "f11", "win",
    "esc", "tab", "enter", "space", "backspace", "delete", "up", "down",
    "left", "right", "pageup", "pagedown", "home", "end", "shift+tab",
    "ctrl+plus", "ctrl+minus", "ctrl+0",
}


def _co():
    if not getattr(_tls, "com", False):
        import comtypes
        try:
            comtypes.CoInitialize()
        except OSError:
            pass
        _tls.com = True


def _endpoint():
    _co()
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


# ---- volume ----
def volume() -> dict:
    try:
        ev = _endpoint()
        return {"vol": round(ev.GetMasterVolumeLevelScalar() * 100),
                "muted": bool(ev.GetMute())}
    except Exception:  # noqa: BLE001
        return {"vol": None, "muted": None}


def set_volume(pct) -> dict:
    pct = max(0, min(100, int(round(float(pct)))))
    ev = _endpoint()
    ev.SetMasterVolumeLevelScalar(pct / 100.0, None)
    if pct > 0 and ev.GetMute():
        ev.SetMute(0, None)   # dragging the slider up means he wants sound
    return volume()


def set_mute(on=None) -> dict:
    ev = _endpoint()
    on = (not ev.GetMute()) if on is None else bool(on)
    ev.SetMute(1 if on else 0, None)
    return volume()


# ---- brightness (laptop panel, WMI; slow, so cached) ----
_bright = {"v": None, "t": 0.0, "busy": False}


def _ps(cmd: str, timeout: float = 8.0) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", cmd], capture_output=True, text=True,
                       timeout=timeout, creationflags=_NOWIN)
    return r.stdout.strip()


def _read_brightness():
    try:
        out = _ps("(Get-CimInstance -Namespace root/WMI -ClassName "
                  "WmiMonitorBrightness -ErrorAction Stop | "
                  "Select-Object -First 1).CurrentBrightness")
        _bright["v"] = int(out.splitlines()[-1])
    except Exception:  # noqa: BLE001 — external monitor or no WMI: hide it
        _bright["v"] = None
    _bright["t"] = time.time()
    _bright["busy"] = False


def brightness(max_age: float = 60.0):
    """Cached value now; a stale cache refreshes in the background so the
    tab never waits on PowerShell."""
    if time.time() - _bright["t"] > max_age and not _bright["busy"]:
        _bright["busy"] = True
        threading.Thread(target=_read_brightness, daemon=True).start()
    return _bright["v"]


def set_brightness(pct) -> int:
    pct = max(0, min(100, int(round(float(pct)))))
    _ps("Get-CimInstance -Namespace root/WMI -ClassName "
        "WmiMonitorBrightnessMethods -ErrorAction Stop | Invoke-CimMethod "
        f"-MethodName WmiSetBrightness -Arguments @{{Timeout=1;Brightness={pct}}}"
        " | Out-Null")
    _bright.update(v=pct, t=time.time())
    return pct


# ---- now playing (Windows media sessions: Spotify, YouTube in Chrome...) ----
def _app_name(aumid: str) -> str:
    n = (aumid or "").split("!")[-1].removesuffix(".exe")
    if "." in n:
        n = n.rsplit(".", 1)[-1]
    low = n.lower()
    for k, v in (("chrome", "Chrome"), ("msedge", "Edge"), ("spotify", "Spotify"),
                 ("firefox", "Firefox"), ("308046b0af4a39cb", "Firefox"),
                 ("zunemusic", "Media Player"), ("vlc", "VLC")):
        if k in low:
            return v
    return n[:1].upper() + n[1:] if n else ""


async def _read_media():
    """Read WinRT media state on the dedicated media event-loop thread."""
    global _MEDIA_ASYNC_LOCK
    import asyncio

    if _MEDIA_ASYNC_LOCK is None:
        _MEDIA_ASYNC_LOCK = asyncio.Lock()
    async with asyncio.timeout(3.0):
        async with _MEDIA_ASYNC_LOCK:
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Mgr)

            m = await Mgr.request_async()
            s = m.get_current_session()
            if s is None:
                return None
            p = await s.try_get_media_properties_async()
            st = int(s.get_playback_info().playback_status)
            return {"app": _app_name(s.source_app_user_model_id),
                    "title": (p.title or "")[:120],
                    "artist": (p.artist or "")[:80],
                    "playing": st == 4}


def _media_loop_thread():
    """Keep WinRT COM objects and async operations on one apartment/thread."""
    import asyncio

    global _MEDIA_LOOP, _MEDIA_ASYNC_LOCK
    # Keep this thread independent of the phone_ctl volume worker: importing
    # comtypes there initializes that worker's COM apartment as STA.
    _MEDIA_ASYNC_LOCK = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _MEDIA_LOOP = loop
    _MEDIA_READY.set()
    loop.run_forever()


def _media_loop():
    global _MEDIA_THREAD
    with _MEDIA_INIT_LOCK:
        if _MEDIA_THREAD is None or not _MEDIA_THREAD.is_alive():
            _MEDIA_READY.clear()
            _MEDIA_THREAD = threading.Thread(
                target=_media_loop_thread, name="phone-media-winrt", daemon=True)
            _MEDIA_THREAD.start()
    return _MEDIA_LOOP if _MEDIA_READY.wait(2.0) else None


def media():
    """{app, title, artist, playing} for the current media session, or None.

    Phone state polling uses a thread pool, whose worker can change on every
    request. WinRT media session interfaces are apartment-bound, so keep all
    calls on one persistent loop thread and return only plain Python data.
    """
    try:
        import asyncio

        loop = _media_loop()
        if loop is None:
            return None
        future = asyncio.run_coroutine_threadsafe(_read_media(), loop)
        return future.result(timeout=4.0)
    except Exception:  # noqa: BLE001 — package missing, timeout, or no session
        return None


# ---- windows ----
_SKIP = {"Program Manager", "Windows Input Experience", "GOAT", "GOAT live"}
_names: dict = {}


def _pname(pid: int) -> str:
    if pid not in _names:
        try:
            import psutil
            _names[pid] = psutil.Process(pid).name().removesuffix(".exe")
        except Exception:  # noqa: BLE001
            _names[pid] = "?"
    return _names[pid]


def windows(limit: int = 14) -> list:
    import screen_hands
    fg = _user32.GetForegroundWindow()
    me = os.getpid()
    out = []
    for w in screen_hands.windows():
        if w["pid"] == me or w["title"] in _SKIP:
            continue
        out.append({"h": w["hwnd"], "title": w["title"][:90],
                    "app": _pname(w["pid"]), "min": bool(w.get("minimized")),
                    "fg": w["hwnd"] == fg})
        if len(out) >= limit:
            break
    return out


def focus(hwnd) -> str:
    import screen_hands
    w = screen_hands.focus_window(str(int(hwnd)))
    return ("front: " if w.get("focused") else "tried: ") + w["title"][:40]


def minimize(hwnd) -> str:
    import screen_hands
    w = screen_hands.window_state(str(int(hwnd)), "minimize")
    return "minimized " + w["title"][:40]


# ---- keys / typing ----
def chord(combo: str) -> str:
    combo = str(combo).strip().lower()
    if combo not in CHORDS:
        raise ValueError(f"key not allowed: {combo}")
    import screen_hands
    screen_hands.press(combo)
    return combo


def type_text(text: str, enter: bool = False) -> str:
    import screen_hands
    text = str(text)[:2000]
    screen_hands.type_text(text)
    if enter:
        screen_hands.press("enter")
    return "typed" + (" + enter" if enter else "")


def screen_off() -> str:
    # PostMessage, not SendMessage: a hung window must not hang the remote.
    _user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)
    return "screen off"


def state() -> dict:
    out = volume()
    out["bright"] = brightness()
    out["media"] = media()
    try:
        out["windows"] = windows()
    except Exception:  # noqa: BLE001
        out["windows"] = []
    return out
