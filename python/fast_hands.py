"""Fast hands: simple on-screen actions with no model in the loop.

His complaint (2026-10-01): "goat takes about 10 seconds to do the simple
tasks, close the tab, click this, click that". The log said why: "close the
two tabs" went to the Claude brain, which thought, pressed ctrl+w (42ms),
screenshotted the tab strip, pressed again, screenshotted again and stepped
back in — five model round trips for two keystrokes.

This module is what the reflex lane calls instead:

- keys(): a key chord aimed at the window he means — the topmost real
  window that is NOT GOAT (and for tab verbs, the topmost browser) — focused
  first, so a chord can never land on GOAT itself.
- click_named(): "click Subscribe" finds the control by its accessible name
  through Windows UI Automation (the same tree screen readers use) and
  clicks its centre. No screenshot, no model. If nothing matches it returns
  DEFER and the turn goes to the brain, which can look.

Every function returns a short result string for the reflex log; "ERROR: …"
means it failed (spoken as a failure), "DEFER: …" means "not mine — let the
brain take it".
"""
from __future__ import annotations

import os
import re
import threading
import time

import screen_hands

DEFER = "DEFER"

BROWSERS = {"brave.exe": "Brave", "chrome.exe": "Chrome", "msedge.exe": "Edge",
            "firefox.exe": "Firefox", "opera.exe": "Opera", "vivaldi.exe": "Vivaldi"}
# Top-level windows that are part of the desktop, not something he means.
_SHELL_TITLES = {"program manager", "windows input experience", "settings",
                 "microsoft text input application", "nvidia geforce overlay"}


def _proc_name(pid: int) -> str:
    try:
        import psutil
        return psutil.Process(pid).name().lower()
    except Exception:  # noqa: BLE001 — a vanished pid is just "not a match"
        return ""


def target_window(browser: bool = False) -> dict | None:
    """The window he is talking about: the topmost real window that isn't
    GOAT. EnumWindows walks the z-order front to back, so the first hit is
    what he last used — even when GOAT's own window has the focus because he
    was just looking at it."""
    own = os.getpid()
    for w in screen_hands.windows():
        if w["pid"] == own or w["minimized"]:
            continue
        if w["width"] < 200 or w["height"] < 150:
            continue
        if w["title"].strip().lower() in _SHELL_TITLES:
            continue
        name = _proc_name(w["pid"])
        if name in ("explorer.exe", "textinputhost.exe", "shellexperiencehost.exe") \
                and w["title"].strip().lower() in ("", "program manager"):
            continue
        if browser and name not in BROWSERS:
            continue
        w["proc"] = name
        w["app"] = BROWSERS.get(name) or os.path.splitext(name)[0] or w["title"]
        return w
    return None


def _focus(w: dict) -> bool:
    try:
        return bool(screen_hands.focus_window(str(w["hwnd"])).get("focused"))
    except Exception:  # noqa: BLE001
        return False


def keys(combo: str, times: int = 1, browser: bool = False,
         what: str = "") -> str:
    """Focus the window he means, press the chord, say where it went."""
    w = target_window(browser=browser)
    if w is None:
        return (f"{DEFER}: no browser window open" if browser
                else f"{DEFER}: no window to send {combo} to")
    if not _focus(w):
        return f"ERROR: couldn't bring {w['app']} to the front"
    screen_hands.press(combo, times=max(1, min(int(times), 30)))
    label = what or combo
    n = f" x{times}" if times > 1 else ""
    return f"{label}{n} in {w['app']}"


def scroll(direction: str, pages: int = 1) -> str:
    w = target_window()
    if w is None:
        return f"{DEFER}: no window to scroll"
    _focus(w)
    cx = w["left"] + w["width"] // 2
    cy = w["top"] + w["height"] // 2
    clicks = (-5 if direction == "down" else 5) * max(1, min(pages, 10))
    screen_hands.scroll(clicks, cx, cy)
    return f"scrolled {direction} in {w['app']}"


# --------------------------------------------------------------------------
# Click by name — Windows UI Automation over comtypes
# --------------------------------------------------------------------------

_uia_lock = threading.Lock()
_uia_mod = None


def _uia():
    """The UIAutomationClient type library, generated once and cached by
    comtypes. The first generation takes a moment, so warm() calls this at
    boot rather than on his first "click …"."""
    global _uia_mod
    if _uia_mod is None:
        with _uia_lock:
            if _uia_mod is None:
                import comtypes.client
                comtypes.client.GetModule("UIAutomationCore.dll")
                from comtypes.gen import UIAutomationClient as mod
                _uia_mod = mod
    return _uia_mod


def warm() -> None:
    try:
        _uia()
    except Exception:  # noqa: BLE001 — click-by-name just defers to the brain
        pass


# Control types worth clicking by name. Plain text is left out on purpose:
# "click Settings" must hit the Settings button, not a paragraph mentioning it.
_CLICKABLE = ("Button", "Hyperlink", "MenuItem", "TabItem", "ListItem",
              "CheckBox", "RadioButton", "SplitButton", "TreeItem", "ComboBox",
              "MenuBar", "Image")


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


def _score(name: str, want: str) -> int:
    """0 = no match. Exact beats prefix beats whole-word containment; a
    longer name that merely contains the words ranks lowest."""
    n = _norm(name)
    if not n:
        return 0
    if n == want:
        return 100
    if n.startswith(want + " ") or n.startswith(want):
        return 80 - min(30, len(n) - len(want)) // 2
    if re.search(r"\b" + re.escape(want) + r"\b", n):
        return 50 - min(40, len(n) - len(want)) // 3
    return 0


def find_named(label: str, hwnd: int, timeout: float = 2.5) -> dict | None:
    """Best clickable element whose accessible name matches `label`, as
    {"name", "x", "y"} in screen pixels, or None."""
    import comtypes
    import comtypes.client
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass  # already initialised on this worker thread
    mod = _uia()
    uia = comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation)
    root = uia.ElementFromHandle(hwnd)
    types = [getattr(mod, f"UIA_{t}ControlTypeId") for t in _CLICKABLE
             if hasattr(mod, f"UIA_{t}ControlTypeId")]
    conds = [uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, t) for t in types]
    cond = uia.CreateOrConditionFromArray(conds)
    onscreen = uia.CreatePropertyCondition(mod.UIA_IsOffscreenPropertyId, False)
    cond = uia.CreateAndCondition(cond, onscreen)
    # One cross-process round trip for every name + rectangle, instead of two
    # per element — the difference between ~0.2s and several seconds on a
    # busy web page.
    cache = uia.CreateCacheRequest()
    cache.AddProperty(mod.UIA_NamePropertyId)
    cache.AddProperty(mod.UIA_BoundingRectanglePropertyId)
    want = _norm(label)
    if not want:
        return None
    t0 = time.monotonic()
    best, best_score = None, 0
    found = root.FindAllBuildCache(mod.TreeScope_Descendants, cond, cache)
    for i in range(found.Length):
        if time.monotonic() - t0 > timeout:
            break
        el = found.GetElement(i)
        name = el.CachedName or ""
        sc = _score(name, want)
        if sc <= best_score:
            continue
        r = el.CachedBoundingRectangle
        w, h = r.right - r.left, r.bottom - r.top
        if w <= 1 or h <= 1:
            continue
        best, best_score = {"name": name.strip(), "x": r.left + w // 2,
                            "y": r.top + h // 2}, sc
        if sc == 100:
            break
    return best


# --------------------------------------------------------------------------
# Click by visible text — Windows' own OCR engine
# --------------------------------------------------------------------------
# Web pages in his everyday Brave don't expose their content to UI
# Automation (renderer accessibility is off unless a screen reader asks), so
# "click Subscribe" on YouTube finds nothing there. Windows ships an OCR
# engine; one window grab + recognition measured ~200ms on this laptop.

_ocr_engine = None


def _ocr(x: int, y: int, w: int, h: int):
    """OcrResult for one screen rectangle (real pixels)."""
    import asyncio
    import mss
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
    global _ocr_engine
    if _ocr_engine is None:
        _ocr_engine = OcrEngine.try_create_from_user_profile_languages()
    with mss.mss() as s:
        img = s.grab({"left": x, "top": y, "width": w, "height": h})
    dw = DataWriter()
    dw.write_bytes(bytes(img.bgra))
    bmp = SoftwareBitmap.create_copy_from_buffer(
        dw.detach_buffer(), BitmapPixelFormat.BGRA8, img.width, img.height)

    async def run():
        return await _ocr_engine.recognize_async(bmp)
    return asyncio.run(run())


def find_text(label: str, hwnd: int) -> dict | None:
    """Where `label` is written in the window, as {"name", "x", "y"}. A line
    that IS the label (a button) beats a sentence that merely contains it."""
    want = _norm(label).split()
    if not want:
        return None
    x, y, w, h = screen_hands.client_rect(hwnd)
    if w <= 0 or h <= 0:
        return None
    res = _ocr(x, y, w, h)
    best, best_score = None, 0
    for line in res.lines:
        words = list(line.words)
        norm = [_norm(wd.text) for wd in words]
        n = len(want)
        for i in range(len(words) - n + 1):
            if norm[i:i + n] != want:
                continue
            extra = len(words) - n
            sc = 100 - min(60, extra * 8)
            if sc <= best_score:
                continue
            rs = [words[j].bounding_rect for j in range(i, i + n)]
            left = min(r.x for r in rs)
            top = min(r.y for r in rs)
            right = max(r.x + r.width for r in rs)
            bottom = max(r.y + r.height for r in rs)
            best, best_score = {"name": " ".join(wd.text for wd in words[i:i + n]),
                                "x": int(x + (left + right) / 2),
                                "y": int(y + (top + bottom) / 2)}, sc
    return best


def click_named(label: str) -> str:
    w = target_window()
    if w is None:
        return f"{DEFER}: no window to click in"
    _focus(w)
    hit, how = None, ""
    try:
        hit, how = find_named(label, w["hwnd"]), "ui"
    except Exception:  # noqa: BLE001 — UIA hiccup: OCR can still see it
        hit = None
    if hit is None:
        try:
            time.sleep(0.12)   # let the window we just raised finish painting
            hit, how = find_text(label, w["hwnd"]), "ocr"
        except Exception as e:  # noqa: BLE001 — no OCR: the brain can look
            return f"{DEFER}: ocr failed ({e})"
    if hit is None:
        return f"{DEFER}: no '{label}' in {w['app']}"
    screen_hands.click(hit["x"], hit["y"])
    return f"clicked '{hit['name'][:60]}' in {w['app']} ({how})"
