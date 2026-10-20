"""Real sight: the screen as things, not as a picture to guess at.

His order, 2026-10-21: "I want the GOAT to actually see my screen, not see
only with screenshots and then guess where to click." The old loop was
screenshot -> read grid numbers off a downscaled image -> click a pixel ->
screenshot again to find out whether it hit. Every step a guess.

Now the screen is read the way the OS itself knows it:

  * UI Automation  — every button, link, field, tab, menu item, checkbox in
                     the window, with its exact rectangle, name and value.
  * Windows OCR    — every line and word of text actually drawn, for what UIA
                     can't see (web pages in his everyday browser, games,
                     custom-drawn apps), with exact word boxes.

look()     both merged into numbered MARKS drawn on one screenshot plus a
           text list ("[12] button 'Save' (840,512)"). The brain clicks
           mark 12 and lands on the element's exact centre.
find()     text query -> exact element/word positions, no image at all.
read()     everything written in the window, no image.
changes()  what changed since GOAT last looked (areas + the new text).
wait_for() poll until text appears/disappears or the screen settles,
           instead of blind waits and repeated screenshots.
act_and_report() runs a click/keypress and says what it changed, so an
           action verifies itself without another screenshot.
"""
from __future__ import annotations

import asyncio
import io
import re
import threading
import time

import numpy as np

import screen_hands

MAX_MARKS = 220
FRAME_TTL_S = 180          # marks older than this are refused (screen moved on)
_lock = threading.RLock()
_frame: dict = {}          # last look: id -> mark, plus geometry for checks
_last_small = {}           # region key -> small grey frame, for changes()


# --------------------------------------------------------------- targets --

def target(target_spec=None) -> dict:
    """The window to look at, as {hwnd,title,app,left,top,width,height}.
    None = the window he's using (topmost that isn't GOAT); "screen" = the
    whole desktop; otherwise a title fragment or hwnd."""
    import fast_hands
    if target_spec in (None, "", "window", "front"):
        w = fast_hands.target_window()
        if w is None:
            raise ValueError("no window to look at")
    elif str(target_spec).lower() in ("screen", "desktop", "all"):
        vx, vy, vw, vh = screen_hands.virtual_screen()
        return {"hwnd": 0, "title": "whole screen", "app": "screen",
                "left": vx, "top": vy, "width": vw, "height": vh}
    else:
        w = dict(screen_hands._match(str(target_spec)))
        w.setdefault("app", w["title"][:30])
    x, y, cw, ch = screen_hands.client_rect(w["hwnd"])
    if cw > 50 and ch > 50:
        w.update(left=x, top=y, width=cw, height=ch)
    return w


def _clip(rect, box):
    l, t, r, b = rect
    bl, bt, bw, bh = box
    return max(l, bl), max(t, bt), min(r, bl + bw), min(b, bt + bh)


# ------------------------------------------------------------------- UIA --

_INTERACTIVE = ("Button", "Hyperlink", "MenuItem", "TabItem", "ListItem",
                "CheckBox", "RadioButton", "SplitButton", "TreeItem",
                "ComboBox", "Edit", "Slider", "Spinner", "DataItem",
                "Thumb", "ScrollBar", "Document", "Image", "MenuBar")
_TEXTUAL = ("Text", "Edit", "Document", "DataItem", "ListItem", "Hyperlink")
_type_names: dict = {}


def _uia():
    import comtypes
    import comtypes.client
    import fast_hands
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass
    mod = fast_hands._uia()
    if not _type_names:
        for n in dir(mod):
            m = re.fullmatch(r"UIA_(\w+)ControlTypeId", n)
            if m:
                _type_names[getattr(mod, n)] = m.group(1)
    uia = comtypes.client.CreateObject(mod.CUIAutomation,
                                       interface=mod.IUIAutomation)
    return mod, uia


def uia_elements(win: dict, kinds=_INTERACTIVE, limit: int = 400,
                 timeout: float = 1.5) -> list[dict]:
    """Elements of `kinds` inside the window, on screen, with exact rects.
    One cross-process round trip (cache request) for every property."""
    if not win.get("hwnd"):
        return []
    mod, uia = _uia()
    root = uia.ElementFromHandle(win["hwnd"])
    ids = [getattr(mod, f"UIA_{k}ControlTypeId") for k in kinds
           if hasattr(mod, f"UIA_{k}ControlTypeId")]
    cond = uia.CreateOrConditionFromArray(
        [uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, i)
         for i in ids])
    cond = uia.CreateAndCondition(cond, uia.CreatePropertyCondition(
        mod.UIA_IsOffscreenPropertyId, False))
    cache = uia.CreateCacheRequest()
    for p in ("Name", "BoundingRectangle", "ControlType", "IsEnabled",
              "ValueValue", "HasKeyboardFocus", "ToggleToggleState"):
        pid = getattr(mod, f"UIA_{p}PropertyId", None)
        if pid is not None:
            cache.AddProperty(pid)
    found = root.FindAllBuildCache(mod.TreeScope_Descendants, cond, cache)
    box = (win["left"], win["top"], win["width"], win["height"])
    out, t0 = [], time.monotonic()
    for i in range(min(found.Length, 3000)):
        if time.monotonic() - t0 > timeout or len(out) >= limit:
            break
        el = found.GetElement(i)
        r = el.CachedBoundingRectangle
        l, t, rr, b = _clip((r.left, r.top, r.right, r.bottom), box)
        if rr - l < 4 or b - t < 4:
            continue
        kind = _type_names.get(el.CachedControlType, "Element")
        name = (el.CachedName or "").strip()
        value = ""
        try:
            v = el.GetCachedPropertyValue(mod.UIA_ValueValuePropertyId)
            value = v.strip() if isinstance(v, str) else ""
        except Exception:  # noqa: BLE001
            pass
        if kind in ("Document", "Image", "ScrollBar", "Thumb", "MenuBar") \
                and not name:
            continue          # a nameless container says nothing
        if kind == "Document" and (rr - l) * (b - t) > 0.5 * box[2] * box[3]:
            continue          # the page itself: its text comes from OCR
        e = {"src": "ui", "kind": kind.lower(), "name": name[:120],
             "rect": (l, t, rr, b)}
        if value and value != name:
            e["value"] = value[:120]
        try:
            if not el.CachedIsEnabled:
                e["disabled"] = True
            if el.CachedHasKeyboardFocus:
                e["focused"] = True
            ts = el.GetCachedPropertyValue(mod.UIA_ToggleToggleStatePropertyId)
            if ts in (0, 1):
                e["checked"] = bool(ts)
        except Exception:  # noqa: BLE001
            pass
        out.append(e)
    return out


# ------------------------------------------------------------------- OCR --

_ocr_engine = None


def _grab(box):
    import mss
    with mss.mss() as s:
        img = s.grab({"left": int(box[0]), "top": int(box[1]),
                      "width": max(1, int(box[2])),
                      "height": max(1, int(box[3]))})
    return img


def ocr_lines(box) -> list[dict]:
    """Lines of drawn text inside box (left, top, width, height), each with
    exact word boxes in real screen pixels. ~150-300 ms for a window."""
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
    global _ocr_engine
    if _ocr_engine is None:
        _ocr_engine = OcrEngine.try_create_from_user_profile_languages()
    img = _grab(box)
    dw = DataWriter()
    dw.write_bytes(bytes(img.bgra))
    bmp = SoftwareBitmap.create_copy_from_buffer(
        dw.detach_buffer(), BitmapPixelFormat.BGRA8, img.width, img.height)

    async def run():
        return await _ocr_engine.recognize_async(bmp)
    res = asyncio.run(run())
    ox, oy = int(box[0]), int(box[1])
    out = []
    for line in res.lines:
        words = []
        for w in line.words:
            r = w.bounding_rect
            words.append({"text": w.text,
                          "rect": (ox + int(r.x), oy + int(r.y),
                                   ox + int(r.x + r.width),
                                   oy + int(r.y + r.height))})
        if not words:
            continue
        rect = (min(w["rect"][0] for w in words), min(w["rect"][1] for w in words),
                max(w["rect"][2] for w in words), max(w["rect"][3] for w in words))
        out.append({"src": "ocr", "kind": "text", "name": line.text[:160],
                    "rect": rect, "words": words})
    return out


# ----------------------------------------------------------------- marks --

def _inside(pt, rect, pad=2):
    return (rect[0] - pad <= pt[0] <= rect[2] + pad
            and rect[1] - pad <= pt[1] <= rect[3] + pad)


def _center(rect):
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


def merge(ui: list, ocr: list, area: float) -> list:
    """One list: UIA controls first-class; an OCR line inside a control is
    its caption — it names the control if UIA left it nameless (Tk, many
    games' launchers), and is dropped as a duplicate otherwise. Sorted in
    reading order."""
    def size(e):
        return (e["rect"][2] - e["rect"][0]) * (e["rect"][3] - e["rect"][1])
    small = sorted((e for e in ui if size(e) < 0.2 * area), key=size)
    keep = list(ui)
    for ln in ocr:
        c = _center(ln["rect"])
        host = next((e for e in small if _inside(c, e["rect"])), None)
        if host is None:
            keep.append(ln)
        elif not host["name"] or host.get("ocr_named"):
            host["name"] = (host["name"] + " " + ln["name"]).strip()[:120]
            host["ocr_named"] = True
    keep.sort(key=lambda e: (e["rect"][1] // 14, e["rect"][0]))
    return keep[:MAX_MARKS]


def _describe(i: int, e: dict) -> str:
    x, y = _center(e["rect"])
    s = f"[{i}] {e['kind']}"
    if e.get("name"):
        s += f" {e['name'][:70]!r}"
    if e.get("value"):
        s += f" value={e['value'][:50]!r}"
    for flag in ("focused", "disabled"):
        if e.get(flag):
            s += f" {flag}"
    if "checked" in e:
        s += " checked" if e["checked"] else " unchecked"
    return s + f" ({x},{y})"


def _font(size):
    from PIL import ImageFont
    for f in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


_COLORS = [(255, 59, 48), (0, 122, 255), (52, 199, 89), (255, 149, 0),
           (175, 82, 222), (255, 45, 85), (0, 199, 190), (255, 204, 0)]


def render(box, marks: list, max_edge: int = 1400) -> tuple[bytes, float]:
    """Screenshot of box with every mark boxed and numbered (set-of-marks)."""
    from PIL import Image, ImageDraw
    raw = _grab(box)
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    scale = 1.0
    if max(img.size) > max_edge:
        scale = max_edge / max(img.size)
        img = img.resize((int(img.size[0] * scale), int(img.size[1] * scale)),
                         resample=Image.BILINEAR)
    d = ImageDraw.Draw(img)
    font = _font(12)
    ox, oy = box[0], box[1]
    for i, e in enumerate(marks, 1):
        col = _COLORS[i % len(_COLORS)]
        l, t, r, b = [(v - o) * scale for v, o in
                      zip(e["rect"], (ox, oy, ox, oy))]
        d.rectangle((l, t, r, b), outline=col, width=1 if e["src"] == "ocr" else 2)
        tag = str(i)
        tw = d.textlength(tag, font=font)
        ty = t - 13 if t >= 13 else t
        d.rectangle((l, ty, l + tw + 4, ty + 13), fill=col)
        d.text((l + 2, ty), tag, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), scale


def look(target_spec=None, ocr: bool = True, image: bool = True,
         max_edge: int = 1400) -> dict:
    """Marks for the window he's on (or target_spec). Returns
    {"png": bytes|None, "listing": str, "count": n, "window": title}."""
    win = target(target_spec)
    box = (win["left"], win["top"], win["width"], win["height"])
    t0 = time.monotonic()
    # Marks taken while the window is still laying itself out point at
    # buttons that then move (Calculator, 2026-10-21: "=" landed on the
    # decimal point). Wait up to 1 s for two frames 120 ms apart to agree.
    try:
        prev = _small(box)
        while time.monotonic() - t0 < 1.0:
            time.sleep(0.12)
            cur = _small(box)
            if not diff_boxes(prev, cur, box):
                break
            prev = cur
    except Exception:  # noqa: BLE001
        pass
    ui, lines, notes = [], [], []
    try:
        ui = uia_elements(win)
    except Exception as e:  # noqa: BLE001 — OCR can still see
        notes.append(f"ui automation unavailable ({type(e).__name__})")
    if ocr:
        try:
            lines = ocr_lines(box)
        except Exception as e:  # noqa: BLE001
            notes.append(f"ocr unavailable ({type(e).__name__})")
    marks = merge(ui, lines, box[2] * box[3])
    with _lock:
        _frame.clear()
        _frame.update(t=time.time(), box=box, hwnd=win.get("hwnd"),
                      title=win.get("title", ""),
                      marks={i: m for i, m in enumerate(marks, 1)})
        _last_small[_key(box)] = _small(box)
    png = None
    if image:
        png, _ = render(box, marks, max_edge)
    ms = int((time.monotonic() - t0) * 1000)
    head = (f"{win.get('app', '')} — {win.get('title', '')[:70]!r}, area "
            f"{box[2]}x{box[3]} at ({box[0]},{box[1]}). {len(marks)} marks "
            f"({len(ui)} controls, {len(lines)} text lines) in {ms} ms. "
            "Act with click_mark mark=N (exact centre) or click_text; "
            "coordinates in () are real screen pixels.")
    if notes:
        head += " Note: " + "; ".join(notes) + "."
    listing = head + "\n" + "\n".join(_describe(i, m)
                                      for i, m in enumerate(marks, 1))
    return {"png": png, "listing": listing[:12000], "count": len(marks),
            "window": win.get("title", "")}


def mark(n: int) -> dict:
    """The mark from the last look, refusing stale frames and moved windows."""
    with _lock:
        if not _frame:
            raise ValueError("no marks yet: call look first")
        if time.time() - _frame["t"] > FRAME_TTL_S:
            raise ValueError("those marks are stale (over 3 minutes): look again")
        m = _frame["marks"].get(int(n))
        if m is None:
            raise ValueError(f"no mark {n} (last look had {len(_frame['marks'])})")
        hwnd, box = _frame.get("hwnd"), _frame["box"]
    if hwnd:
        try:
            x, y, w, h = screen_hands.client_rect(hwnd)
            if (abs(x - box[0]) > 8 or abs(y - box[1]) > 8
                    or abs(w - box[2]) > 8 or abs(h - box[3]) > 8):
                raise ValueError("the window moved or resized since the last "
                                 "look: look again")
        except OSError:
            pass
    return m


# ------------------------------------------------------------ find / read --

def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


def find(query: str, target_spec=None, limit: int = 5) -> list[dict]:
    """Where `query` is on screen: controls by accessible name and drawn
    text by OCR word runs. Best first, each {"name","kind","x","y","src"}."""
    want = _norm(query)
    if not want:
        return []
    ww = want.split()
    win = target(target_spec)
    box = (win["left"], win["top"], win["width"], win["height"])
    hits = []
    try:
        for e in uia_elements(win, kinds=_INTERACTIVE + ("Text",)):
            n = _norm(e["name"])
            if not n:
                continue
            if n == want:
                sc = 100
            elif n.startswith(want):
                sc = 85 - min(30, len(n) - len(want)) // 2
            elif re.search(r"\b" + re.escape(want) + r"\b", n):
                sc = 60 - min(40, len(n) - len(want)) // 3
            else:
                continue
            if e["kind"] == "text":
                sc -= 15              # a label loses to the control itself
            hits.append((sc, e))
    except Exception:  # noqa: BLE001
        pass
    try:
        for ln in ocr_lines(box):
            norm = [_norm(w["text"]) for w in ln["words"]]
            for i in range(len(norm) - len(ww) + 1):
                if norm[i:i + len(ww)] != ww:
                    continue
                rs = [w["rect"] for w in ln["words"][i:i + len(ww)]]
                rect = (min(r[0] for r in rs), min(r[1] for r in rs),
                        max(r[2] for r in rs), max(r[3] for r in rs))
                extra = len(norm) - len(ww)
                hits.append((90 - min(50, extra * 6),
                             {"src": "ocr", "kind": "text", "rect": rect,
                              "name": " ".join(w["text"] for w in
                                               ln["words"][i:i + len(ww)]),
                              "line": ln["name"]}))
    except Exception:  # noqa: BLE001
        pass
    hits.sort(key=lambda h: (-h[0], h[1]["rect"][1], h[1]["rect"][0]))
    out, seen = [], []
    for sc, e in hits:
        c = _center(e["rect"])
        if any(abs(c[0] - s[0]) < 10 and abs(c[1] - s[1]) < 10 for s in seen):
            continue
        seen.append(c)
        out.append({"name": e["name"], "kind": e["kind"], "src": e["src"],
                    "x": c[0], "y": c[1], "score": sc,
                    **({"line": e["line"]} if e.get("line") else {})})
        if len(out) >= limit:
            break
    return out


def read(target_spec=None, limit: int = 8000) -> str:
    """Everything written in the window in reading order: OCR lines, plus
    field values UIA knows that aren't drawn as plain text."""
    win = target(target_spec)
    box = (win["left"], win["top"], win["width"], win["height"])
    lines = ocr_lines(box)
    lines.sort(key=lambda e: (e["rect"][1] // 12, e["rect"][0]))
    rows, cur, cur_y = [], [], None
    for ln in lines:
        y = ln["rect"][1] // 12
        if cur_y is not None and y != cur_y:
            rows.append("   ".join(cur))
            cur = []
        cur.append(ln["name"])
        cur_y = y
    if cur:
        rows.append("   ".join(cur))
    extra, known = [], []
    seen = _norm(" ".join(rows))
    try:
        for e in uia_elements(win, kinds=("Edit", "ComboBox", "Text")):
            if e.get("value"):
                extra.append(f"{e['kind']} {e['name'] or '(unnamed)'!r} = "
                             f"{e['value']!r}")
            elif e["kind"] == "text" and e["name"] and _norm(e["name"]) not in seen:
                # Text the app reports but OCR couldn't read (thin display
                # fonts, Calculator's result: "Display is 12").
                known.append(e["name"])
    except Exception:  # noqa: BLE001
        pass
    text = "\n".join(rows)
    if known:
        text += "\n\n[text the app reports]\n" + "\n".join(known[:40])
    if extra:
        text += "\n\n[fields]\n" + "\n".join(extra)
    return (f"{win.get('title', '')[:70]!r}:\n" + text)[:limit]


# --------------------------------------------------------------- changes --

def _key(box):
    return tuple(int(v) for v in box)


def _small(box, factor: int = 4):
    """Grey, downscaled frame for cheap change detection (~15 ms)."""
    raw = _grab(box)
    a = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(raw.height, raw.width, 4)
    g = (a[..., 0].astype(np.uint16) + a[..., 1] + a[..., 2]) // 3
    return g[::factor, ::factor].astype(np.uint8)


def diff_boxes(a, b, box, factor: int = 4, thr: int = 24,
               min_px: int = 12) -> list:
    """Changed areas between two small frames, as real-pixel rects,
    biggest first."""
    import cv2
    if a is None or b is None or a.shape != b.shape:
        return [(box[0], box[1], box[0] + box[2], box[1] + box[3])]
    d = cv2.absdiff(a, b)
    _, m = cv2.threshold(d, thr, 255, cv2.THRESH_BINARY)
    m = cv2.dilate(m, np.ones((5, 5), np.uint8), iterations=2)
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if w * h < min_px:
            continue
        out.append((box[0] + x * factor, box[1] + y * factor,
                    box[0] + (x + w) * factor, box[1] + (y + h) * factor))
    out.sort(key=lambda r: -(r[2] - r[0]) * (r[3] - r[1]))
    return out


def _describe_change(boxes: list, box, ocr: bool = True) -> str:
    if not boxes:
        return "nothing on screen changed"
    total = box[2] * box[3]
    area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in boxes)
    pct = min(100, int(100 * area / max(1, total)))
    parts = [f"{len(boxes)} area(s) changed, ~{pct}% of the view"]
    if ocr and pct < 90:
        texts = []
        for r in boxes[:3]:
            w, h = r[2] - r[0], r[3] - r[1]
            if w < 8 or h < 8:
                continue
            # Pad the crop: OCR can't read one big glyph cut out tight
            # (Calculator's display digit came back empty).
            pad = 40
            ox, oy = max(box[0], r[0] - pad), max(box[1], r[1] - pad)
            ow = min(box[0] + box[2], r[2] + pad) - ox
            oh = min(box[1] + box[3], r[3] + pad) - oy
            try:
                got = " | ".join(ln["name"] for ln in
                                 ocr_lines((ox, oy, ow, oh)))[:160]
            except Exception:  # noqa: BLE001
                got = ""
            texts.append(f"({r[0]},{r[1]} {w}x{h})"
                         + (f" now reads: {got!r}" if got else ""))
        if texts:
            parts.append("; ".join(texts))
    elif pct >= 90:
        parts.append("the whole view redrew (new page or screen)")
    return ". ".join(parts)


def changes(target_spec=None) -> str:
    """What changed in the window since GOAT last looked at it."""
    win = target(target_spec)
    box = (win["left"], win["top"], win["width"], win["height"])
    now = _small(box)
    prev = _last_small.get(_key(box))
    _last_small[_key(box)] = now
    if prev is None:
        return ("first look at this window from here, nothing to compare; "
                "ask again after something happens")
    return _describe_change(diff_boxes(prev, now, box), box)


def act_and_report(fn, settle: float = 0.35, target_spec=None) -> tuple:
    """Run an action and describe what it changed (fn's result, report).
    The check that used to need a whole new screenshot."""
    try:
        win = target(target_spec)
        box = (win["left"], win["top"], win["width"], win["height"])
        before = _small(box)
        title = win.get("title", "")
    except Exception:  # noqa: BLE001
        return fn(), ""
    result = fn()
    time.sleep(settle)
    try:
        fg = screen_hands.foreground() or {}
        if fg and fg.get("title") and fg.get("title") != title \
                and fg.get("pid") != __import__("os").getpid():
            return result, f"a different window is in front now: {fg['title'][:70]!r}"
        # Some apps redraw late (Calculator's "=" landed after 0.35 s and
        # was reported as "nothing changed", 2026-10-21): keep watching a
        # little longer before calling it no-op. Each check costs ~15 ms.
        after = _small(box)
        boxes = diff_boxes(before, after, box)
        deadline = time.monotonic() + 0.8
        while not boxes and time.monotonic() < deadline:
            time.sleep(0.1)
            after = _small(box)
            boxes = diff_boxes(before, after, box)
        _last_small[_key(box)] = after
        return result, _describe_change(boxes, box)
    except Exception as e:  # noqa: BLE001
        return result, f"(couldn't check the result: {e})"


def wait_for(text: str | None = None, gone: str | None = None,
             stable: bool = False, timeout: float = 10.0,
             target_spec=None) -> str:
    """Block until `text` is on screen, `gone` is off it, or (stable) the
    view stops changing for 0.6 s. Polls; returns what happened."""
    timeout = max(0.5, min(float(timeout), 30.0))
    t0 = time.monotonic()
    win = target(target_spec)
    box = (win["left"], win["top"], win["width"], win["height"])
    prev, still_since = _small(box), time.monotonic()
    while time.monotonic() - t0 < timeout:
        if text and not target_spec:
            # "wait for Calculator" right after launching it: the app shows
            # up as a new window, not as text inside the old one (2026-10-21,
            # GOAT's brain timed out on exactly this).
            want = _norm(text)
            hit = next((w for w in screen_hands.windows()
                        if want in _norm(w["title"])), None)
            if hit:
                return (f"window {hit['title'][:60]!r} is open after "
                        f"{time.monotonic() - t0:.1f}s (hwnd {hit['hwnd']})")
        if text or gone:
            body = _norm(" ".join(ln["name"] for ln in ocr_lines(box)))
            if text and _norm(text) in body:
                return f"'{text}' appeared after {time.monotonic() - t0:.1f}s"
            if gone and _norm(gone) not in body:
                return f"'{gone}' is gone after {time.monotonic() - t0:.1f}s"
            time.sleep(0.3)
            continue
        time.sleep(0.15)
        cur = _small(box)
        if diff_boxes(prev, cur, box):
            still_since = time.monotonic()
        elif stable and time.monotonic() - still_since >= 0.6:
            return f"screen settled after {time.monotonic() - t0:.1f}s"
        prev = cur
    what = (f"'{text}' never appeared" if text else
            f"'{gone}' is still there" if gone else "screen kept changing")
    return f"timed out after {timeout:.0f}s: {what}"


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    screen_hands.dpi_init()
    r = look(sys.argv[1] if len(sys.argv) > 1 else None, image=False)
    print(r["listing"][:3000])
