"""The screen organ, wired into GOAT's tool belt.

Everything in screen_hands / screen_browser / screen_policy is plumbing. This
file is the socket: it publishes two tools — `computer` and `browser` — through
an in-process MCP server, so they appear in the working brain's tool list right
next to Bash and Read, are called in exactly the same shape as every other
tool, and need no separate process, port, or config file (Giorgi's order
2026-09-14, item 5).

One tool per organ, with an `action` field, rather than twenty small tools:
that is the shape the model already knows from computer-use, and it keeps the
schema — which is re-sent on every single turn — small.

Two cross-cutting jobs live here too:
- The confirm gate (item 4). A confirm-tier action does not silently run: the
  tool hands back what it wants to do and why it is gated, and Giorgi's answer
  comes back as confirm=true on the retry. Nothing is refused, only slowed.
- The ledger (item 6). Every action, allowed or gated, lands in a JSONL log and
  is pushed to the app's left panel as it happens, so a misfire is visible
  instead of mysterious.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import traceback
from collections import deque

from claude_agent_sdk import create_sdk_mcp_server, tool

import screen_browser
import screen_hands
import screen_policy

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "screen-actions.jsonl")
RECENT = deque(maxlen=120)

_emit = None          # set by goat_app: pushes a line onto the work ledger
_confirm_hook = None  # optional: ask Giorgi live instead of bouncing the call


def set_emit(fn) -> None:
    """Let the app show screen actions on the left panel as they happen."""
    global _emit
    _emit = fn


def set_confirm_hook(fn) -> None:
    """Install an interactive confirmer: fn(action, reason, args) -> bool|None.

    None means "no answer available" and the call falls back to the
    confirm=true retry. Nothing here blocks forever waiting for a human.
    """
    global _confirm_hook
    _confirm_hook = fn


def _record(entry: dict) -> None:
    entry["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    RECENT.append(entry)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # a log that cannot be written must never break the hand
    if _emit:
        try:
            mark = {"ok": "", "gated": "needs ok: ", "error": "failed: "}.get(
                entry.get("status", "ok"), "")
            _emit("step", f"screen — {mark}{entry.get('what', '')}")
        except Exception:  # noqa: BLE001 — the UI is not allowed to break a tool
            pass


def _text(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}]}


def _fail(msg: str) -> dict:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


def _gate(action: str, args: dict) -> str | None:
    """Return a refusal-to-proceed message, or None when the action may run."""
    try:
        fg = screen_hands.foreground() or {}
    except Exception:  # noqa: BLE001
        fg = {}
    tier, reason = screen_policy.classify(action, args, fg.get("title", ""))
    what = screen_policy.describe(action, args)
    if tier == screen_policy.AUTO or args.get("confirm") is True:
        if tier != screen_policy.AUTO:
            _record({"tool": "gate", "action": action, "what": what,
                     "status": "ok", "confirmed": True, "reason": reason})
        return None
    if _confirm_hook:
        try:
            answer = _confirm_hook(action, reason, args)
        except Exception:  # noqa: BLE001
            answer = None
        if answer is True:
            _record({"tool": "gate", "action": action, "what": what,
                     "status": "ok", "confirmed": "live", "reason": reason})
            return None
        if answer is False:
            _record({"tool": "gate", "action": action, "what": what,
                     "status": "gated", "confirmed": False, "reason": reason})
            return f"Giorgi said no to: {what}"
    _record({"tool": "gate", "action": action, "what": what,
             "status": "gated", "reason": reason})
    return (f"CONFIRM FIRST — {what}\nWhy it is gated: {reason}.\n"
            f"Ask Giorgi in one short line, and if he agrees repeat this exact "
            f"call with confirm=true. Nothing has happened yet.")


async def _run(tool_name: str, action: str, args: dict, fn, *fn_args, **fn_kw):
    """Gate, run off the event loop, log, and shape the result."""
    blocked = _gate(action, args)
    if blocked:
        return _fail(blocked)
    what = screen_policy.describe(action, args)
    started = time.time()
    try:
        result = await asyncio.to_thread(fn, *fn_args, **fn_kw)
    except Exception as exc:  # noqa: BLE001 — a failed hand reports, never dies
        _record({"tool": tool_name, "action": action, "what": what,
                 "status": "error", "error": str(exc)})
        detail = traceback.format_exc(limit=2) if os.environ.get("GOAT_DEBUG") else ""
        return _fail(f"{action} failed: {exc}\n{detail}".strip())
    _record({"tool": tool_name, "action": action, "what": what, "status": "ok",
             "ms": int((time.time() - started) * 1000),
             "result": str(result)[:200]})
    return result


# --------------------------------------------------------------------------
# computer
# --------------------------------------------------------------------------

# Written out as real JSON Schema rather than the shorthand {name: type} form:
# the shorthand marks every field REQUIRED, and a model dutifully filling in
# x=0, y=0 to satisfy it turns "click where the mouse is" into a click on the
# top-left corner of the screen. Only `action` is required here; everything
# absent stays absent (caught by the first end-to-end probe, 2026-09-14).
COMPUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["screenshot", "click", "double_click", "right_click",
                     "middle_click", "move", "drag", "scroll", "type", "key",
                     "windows", "focus", "window_state", "foreground",
                     "cursor", "pixel", "monitors", "wait", "log",
                     "hide_self", "show_self"],
            "description": "What to do. See the tool description.",
        },
        "x": {"type": "integer", "description": "Real screen pixel X."},
        "y": {"type": "integer", "description": "Real screen pixel Y."},
        "x2": {"type": "integer", "description": "Drag end X."},
        "y2": {"type": "integer", "description": "Drag end Y."},
        "text": {"type": "string",
                 "description": "Text to type, or the chord for `key`."},
        "clicks": {"type": "integer",
                   "description": "Click count, scroll notches (+up/-down), "
                                  "key repeats, or log length."},
        "button": {"type": "string",
                   "enum": ["left", "right", "middle", "horizontal"],
                   "description": "Mouse button; 'horizontal' for sideways scroll."},
        "region": {"type": "string",
                   "description": 'Screenshot area "left,top,width,height" in '
                                  "real pixels. Zoom in rather than squinting."},
        "monitor": {"type": "integer", "description": "Capture one monitor, 1-based."},
        "target": {"type": "string",
                   "description": "Window title fragment or hwnd, for focus / "
                                  "window_state."},
        "state": {"type": "string", "enum": ["minimize", "maximize", "restore"]},
        "label": {"type": "string",
                  "description": "What the thing being clicked SAYS. Pass it on "
                                 "anything consequential - it is what lets the "
                                 "safety gate read the intent."},
        "seconds": {"type": "number", "description": "For wait, max 30."},
        "max_edge": {"type": "integer",
                     "description": "Longest screenshot edge, default 1400."},
        "grid": {"type": "boolean",
                 "description": "Coordinate grid on screenshots. Default true."},
        "confirm": {"type": "boolean",
                    "description": "Set true ONLY after Giorgi has said yes to a "
                                   "gated action."},
    },
    "required": ["action"],
    "additionalProperties": False,
}

COMPUTER_DESC = """See the screen and drive the mouse and keyboard directly.

Use this for anything that only exists as pixels — a GUI app, a settings
toggle, a dialog, a web page you must interact with. Prefer Bash/PowerShell
when a command can do the same job; use this when it cannot.

THE LOOP: screenshot -> read the grid numbers -> act -> screenshot to verify.
Never fire a blind click, and never assume an action worked.

COORDINATES ARE REAL SCREEN PIXELS. Screenshots are downscaled, but the yellow
grid labels printed on them are already in real coordinates — read the numbers
off the picture and pass those. A cyan crosshair marks the mouse.

actions:
  screenshot   - look. region="left,top,width,height" or monitor=N to zoom in
                 on part of the screen; grid=false for a clean image.
  click        - click at x,y. button=left|right|middle, clicks=2 for double.
  double_click / right_click / middle_click - shorthands for the same.
  move         - hover at x,y (tooltips, menus that open on hover).
  drag         - press at x,y, glide to x2,y2, release.
  scroll       - clicks=+up/-down at x,y; horizontal via button="horizontal".
  type         - type text literally at the focused caret. Unicode and
                 Georgian both work; \\n is Enter.
  key          - a chord: "enter", "ctrl+s", "alt+tab", "win+shift+s".
                 Several in a row: "ctrl+a ctrl+c".
  windows      - list open windows with titles and real-pixel rects.
  focus        - bring a window to the front by title fragment or hwnd
                 (target=...). ALWAYS focus a window before typing into it.
  window_state - target=... state=minimize|maximize|restore.
  foreground   - which window has focus right now.
  cursor       - where the mouse is.
  pixel        - RGB at x,y, to confirm a colour change cheaply.
  monitors     - screen count and geometry.
  wait         - seconds=N, to let an app finish drawing.
  log          - the last screen actions taken, for debugging a misfire.
  hide_self    - GOAT's own window floats on top of everything. Call this
                 BEFORE driving another app, or clicks aimed at that app land
                 on GOAT instead and screenshots show GOAT instead of the work.
  show_self    - put GOAT's window back exactly as it was. Always finish with
                 this once the screen work is done.

Pass label="Pay now" (whatever the thing you are clicking says) when clicking
anything consequential — it is what lets the safety gate see the intent.
Destructive or money-shaped actions come back asking for confirmation: relay
the question to Giorgi in one line, then repeat the call with confirm=true.
"""


@tool("computer", COMPUTER_DESC, COMPUTER_SCHEMA)
async def computer(args: dict) -> dict:
    action = str(args.get("action", "")).strip().lower()
    if not action:
        return _fail("action is required")

    def _xy():
        x, y = args.get("x"), args.get("y")
        return (int(x), int(y)) if x is not None and y is not None else (None, None)

    if action == "screenshot":
        blocked = _gate(action, args)
        if blocked:
            return _fail(blocked)
        region = None
        if args.get("region"):
            try:
                parts = [int(float(v)) for v in str(args["region"]).replace(
                    " ", "").split(",")]
                if len(parts) != 4:
                    raise ValueError
                region = tuple(parts)
            except ValueError:
                return _fail('region must be "left,top,width,height"')
        try:
            png, meta = await asyncio.to_thread(
                screen_hands.capture, region, args.get("monitor"),
                int(args.get("max_edge") or 1400),
                args.get("grid", True) is not False)
        except Exception as exc:  # noqa: BLE001
            _record({"tool": "computer", "action": action, "what": "screenshot",
                     "status": "error", "error": str(exc)})
            return _fail(f"screenshot failed: {exc}")
        r, s = meta["region"], meta["image_size"]
        _record({"tool": "computer", "action": action, "status": "ok",
                 "what": screen_policy.describe(action, args),
                 "result": f"{r['width']}x{r['height']} -> {s['width']}x{s['height']}"})
        note = (f"Screen area {r['width']}x{r['height']} at ({r['left']}, {r['top']}), "
                f"shown at {s['width']}x{s['height']} (scale {meta['scale']}). "
                f"Grid labels are REAL screen coordinates"
                + (f", every {meta['grid_step']}px. " if meta['grid_step'] else ". ")
                + f"Mouse is at ({meta['cursor']['x']}, {meta['cursor']['y']}).")
        return {"content": [
            {"type": "image", "data": base64.b64encode(png).decode("ascii"),
             "mimeType": "image/png"},
            {"type": "text", "text": note}]}

    if action in ("click", "double_click", "right_click", "middle_click"):
        x, y = _xy()
        button = {"right_click": "right", "middle_click": "middle"}.get(
            action, str(args.get("button") or "left"))
        clicks = 2 if action == "double_click" else int(args.get("clicks") or 1)
        out = await _run("computer", action, args, screen_hands.click,
                         x, y, button, clicks)
        return out if isinstance(out, dict) else _text(
            f"{action} at {out} ({button}, {clicks}x)")

    if action == "move":
        x, y = _xy()
        if x is None:
            return _fail("move needs x and y")
        out = await _run("computer", action, args, screen_hands.move, x, y)
        return out if isinstance(out, dict) else _text(f"mouse at {out}")

    if action == "drag":
        try:
            x1, y1 = int(args["x"]), int(args["y"])
            x2, y2 = int(args["x2"]), int(args["y2"])
        except (KeyError, TypeError, ValueError):
            return _fail("drag needs x, y, x2, y2")
        out = await _run("computer", action, args, screen_hands.drag,
                         x1, y1, x2, y2, str(args.get("button") or "left"))
        return out if isinstance(out, dict) else _text(
            f"dragged ({x1}, {y1}) -> ({x2}, {y2}), released at {out}")

    if action == "scroll":
        x, y = _xy()
        horizontal = str(args.get("button", "")).lower() == "horizontal"
        clicks = int(args.get("clicks") or -3)
        out = await _run("computer", action, args, screen_hands.scroll,
                         clicks, x, y, horizontal)
        return out if isinstance(out, dict) else _text(
            f"scrolled {clicks} notch(es) at {out}")

    if action == "type":
        text = args.get("text")
        if not text:
            return _fail("type needs text")
        out = await _run("computer", action, args, screen_hands.type_text, str(text))
        return out if isinstance(out, dict) else _text(f"typed {out} characters")

    if action == "key":
        combo = args.get("text") or args.get("label")
        if not combo:
            return _fail('key needs text, e.g. text="ctrl+s"')
        out = await _run("computer", action, args, screen_hands.press,
                         str(combo), int(args.get("clicks") or 1))
        return out if isinstance(out, dict) else _text(f"pressed {out}")

    if action == "windows":
        out = await _run("computer", action, args, screen_hands.windows)
        if isinstance(out, dict):
            return out
        lines = [f"{w['hwnd']}  {w['title'][:62]!r}  "
                 f"{w['width']}x{w['height']} at ({w['left']}, {w['top']})"
                 + ("  [minimized]" if w["minimized"] else "")
                 for w in out]
        return _text(f"{len(lines)} open windows:\n" + "\n".join(lines))

    if action == "focus":
        target = args.get("target") or args.get("text")
        if not target:
            return _fail('focus needs target="window title fragment"')
        out = await _run("computer", action, args, screen_hands.focus_window,
                         str(target))
        if isinstance(out, dict) and "content" in out:
            return out
        ok = "now in front" if out.get("focused") else "raised (focus contested)"
        return _text(f"{out['title'][:60]!r} {ok} — "
                     f"{out['width']}x{out['height']} at ({out['left']}, {out['top']})")

    if action == "window_state":
        target, state = args.get("target"), str(args.get("state") or "")
        if not target or not state:
            return _fail("window_state needs target and state")
        out = await _run("computer", action, args, screen_hands.window_state,
                         str(target), state)
        if isinstance(out, dict) and "content" in out:
            return out
        return _text(f"{out['title'][:60]!r} {state}d")

    if action == "foreground":
        out = await _run("computer", action, args, screen_hands.foreground)
        if isinstance(out, dict) and "content" in out:
            return out
        if not out:
            return _text("nothing has focus")
        return _text(f"{out['title'][:70]!r} (hwnd {out['hwnd']}, pid {out['pid']}) "
                     f"{out['width']}x{out['height']} at ({out['left']}, {out['top']})")

    if action == "hide_self":
        out = await _run("computer", action, args, screen_hands.stand_aside,
                         args.get("grid", True) is not False)
        if isinstance(out, dict) and "content" in out:
            return out
        if not out.get("aside"):
            return _text(out.get("reason", "GOAT's window was not found"))
        return _text("GOAT is out of the way — the screen below is clear now. "
                     "Call show_self when the work is done.")

    if action == "show_self":
        out = await _run("computer", action, args, screen_hands.step_back_in)
        if isinstance(out, dict) and "content" in out:
            return out
        return _text("GOAT is back on screen"
                     if out.get("restored") else
                     out.get("reason", "GOAT's window was not found"))

    if action == "cursor":
        return _text(f"mouse at {screen_hands.cursor_pos()}")

    if action == "pixel":
        x, y = _xy()
        if x is None:
            return _fail("pixel needs x and y")
        out = await _run("computer", action, args, screen_hands.pixel, x, y)
        return out if isinstance(out, dict) else _text(f"RGB at ({x}, {y}): {out}")

    if action == "monitors":
        out = await _run("computer", action, args, screen_hands.monitors)
        return out if isinstance(out, dict) else _text(json.dumps(out))

    if action == "wait":
        seconds = min(float(args.get("seconds") or 1.0), 30.0)
        await asyncio.sleep(seconds)
        return _text(f"waited {seconds}s")

    if action == "log":
        n = int(args.get("clicks") or 15)
        recent = list(RECENT)[-n:]
        if not recent:
            return _text("no screen actions yet this session")
        return _text("\n".join(
            f"{e['at']}  {e.get('status', 'ok'):5}  {e.get('what', e.get('action'))}"
            + (f"  ({e['reason']})" if e.get("reason") else "")
            for e in recent))

    return _fail(f"unknown action {action!r}. See the tool description for the list.")


# --------------------------------------------------------------------------
# browser
# --------------------------------------------------------------------------

BROWSER_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["status", "launch", "tabs", "open", "activate", "close",
                     "navigate", "text", "eval", "click", "fill", "coords", "quit",
                     "windows", "tabsearch"],
            "description": "What to do. See the tool description.",
        },
        "match": {"type": "string",
                  "description": "Which tab: id, title fragment, or URL fragment."},
        "url": {"type": "string", "description": "Page to open or navigate to."},
        "expression": {"type": "string", "description": "JavaScript to evaluate."},
        "selector": {"type": "string", "description": "CSS selector of an element."},
        "value": {"type": "string", "description": "Value for `fill`."},
        "browser": {"type": "string", "enum": ["chrome", "brave", "edge"],
                    "description": "Which browser to launch. Default chrome."},
        "limit": {"type": "integer",
                  "description": "Character cap for `text`. Default 6000."},
        "confirm": {"type": "boolean",
                    "description": "Set true ONLY after Giorgi has said yes to a "
                                   "gated action."},
    },
    "required": ["action"],
    "additionalProperties": False,
}

BROWSER_DESC = """Drive the browser as tabs and elements, not as pixels.

Far more reliable than clicking a tab strip: tabs are addressed by title or URL
fragment, and page elements by CSS selector, so nothing breaks when the layout
shifts.

This talks to a browser GOAT starts on its own profile (Chrome refuses remote
debugging on the everyday profile — a security rule, not a bug). Sign-ins in
that profile persist, so it is a real browser you can keep using.

actions:
  status  - is a debuggable browser running, and on which port.
  launch  - start one. browser=chrome|brave|edge, url=optional first page.
  tabs    - list open tabs with titles and URLs.
  open    - url=... opens a new tab.
  activate- match=title-or-url fragment: bring that tab to the front.
  close   - match=...: close that tab.
  navigate- match=... url=...: point an existing tab somewhere else.
  text    - match=...: the visible text of a page (cheaper than a screenshot).
  eval    - match=... expression=...: run JavaScript, get the value back.
  click   - match=... selector=...: click a real element.
  fill    - match=... selector=... value=...: set an input and fire its events.
  coords  - match=... selector=...: where that element is ON SCREEN, so the
            `computer` tool can click or drag it. Use this for what the DOM
            cannot do - native file pickers, drag and drop, a PDF viewer.
            Call computer hide_self first so GOAT is not covering the page.
  quit    - close the GOAT-driven browser.
  windows - his OWN browser windows (including non-debuggable ones).
  tabsearch - match=...: jump to a tab in the browser HE is already using, via
            the browser's own Ctrl+Shift+A tab search. Use this when he means
            a tab in his everyday window rather than GOAT's.
"""


@tool("browser", BROWSER_DESC, BROWSER_SCHEMA)
async def browser(args: dict) -> dict:
    action = str(args.get("action", "")).strip().lower()
    if not action:
        return _fail("action is required")
    b = screen_browser

    try:
        if action == "status":
            d = await asyncio.to_thread(b.discover)
            return _text(json.dumps(d, ensure_ascii=False))

        if action == "launch":
            out = await _run("browser", action, args, b.launch,
                             str(args.get("browser") or "chrome"),
                             b.GOAT_PORT, str(args.get("url") or "about:blank"))
            if isinstance(out, dict) and "content" in out:
                return out
            verb = "already running" if out.get("reused") else "started"
            return _text(f"{out.get('browser')} {verb} on port {out['port']}")

        if action == "tabs":
            out = await _run("browser", action, args, b.tabs)
            if isinstance(out, dict) and "content" in out:
                return out
            if not out:
                return _text("no open tabs")
            return _text(f"{len(out)} tabs:\n" + "\n".join(
                f"- {t['title'][:64]!r}  {t['url'][:90]}" for t in out))

        if action == "open":
            if not args.get("url"):
                return _fail("open needs url")
            out = await _run("browser", action, args, b.open_tab, str(args["url"]))
            return out if "content" in out else _text(f"opened {out['url']}")

        if action in ("activate", "focus"):
            if not args.get("match"):
                return _fail("activate needs match")
            out = await _run("browser", action, args, b.activate_tab,
                             str(args["match"]))
            return out if "content" in out else _text(f"on {out['active'][:70]!r}")

        if action == "close":
            if not args.get("match"):
                return _fail("close needs match")
            out = await _run("browser", "close_tab", args, b.close_tab,
                             str(args["match"]))
            return out if "content" in out else _text(f"closed {out['closed'][:70]!r}")

        if action == "navigate":
            if not (args.get("match") and args.get("url")):
                return _fail("navigate needs match and url")
            out = await _run("browser", action, args, b.navigate,
                             str(args["match"]), str(args["url"]))
            return out if "content" in out else _text(
                f"{out['title'][:60]!r} is now at {out['url']}")

        if action == "text":
            if not args.get("match"):
                return _fail("text needs match")
            out = await _run("browser", action, args, b.page_text,
                             str(args["match"]), int(args.get("limit") or 6000))
            return out if "content" in out else _text(out["text"] or "(page is empty)")

        if action in ("eval", "evaluate"):
            if not (args.get("match") and args.get("expression")):
                return _fail("eval needs match and expression")
            out = await _run("browser", action, args, b.eval_js,
                             str(args["match"]), str(args["expression"]))
            return out if "content" in out else _text(
                json.dumps(out.get("value"), ensure_ascii=False, default=str)
                if out.get("value") is not None
                else str(out.get("description") or out.get("type")))

        if action in ("click", "click_selector"):
            if not (args.get("match") and args.get("selector")):
                return _fail("click needs match and selector")
            out = await _run("browser", "click_selector", args, b.click_selector,
                             str(args["match"]), str(args["selector"]))
            return out if "content" in out else _text(
                f"clicked {out['clicked']} ({out['element']!r})")

        if action in ("fill", "fill_selector", "type"):
            if not (args.get("match") and args.get("selector")):
                return _fail("fill needs match, selector and value")
            out = await _run("browser", "fill_selector", args, b.fill_selector,
                             str(args["match"]), str(args["selector"]),
                             str(args.get("value") or ""))
            return out if "content" in out else _text(
                f"{out['filled']} = {out['value']!r}")

        if action == "coords":
            if not (args.get("match") and args.get("selector")):
                return _fail("coords needs match and selector")
            out = await _run("browser", action, args, b.element_coords,
                             str(args["match"]), str(args["selector"]))
            return out if "content" in out else _text(
                f"{out['text']!r} is at ({out['x']}, {out['y']}), "
                f"{out['w']}x{out['h']} on screen — in {out['window']!r}")

        if action == "quit":
            out = await _run("browser", "close_browser", args, b.close_browser)
            return out if "content" in out else _text(f"closed {out['closed']}")

        if action == "windows":
            out = await _run("browser", action, args, b.browser_windows)
            if isinstance(out, dict) and "content" in out:
                return out
            if not out:
                return _text("no browser windows open")
            return _text("\n".join(f"{w['hwnd']}  {w['title'][:80]!r}" for w in out))

        if action in ("tabsearch", "tab_search"):
            q = args.get("match") or args.get("value")
            if not q:
                return _fail("tabsearch needs match")
            out = await _run("browser", action, args, b.tab_search, str(q))
            return out if "content" in out else _text(
                f"tab search {q!r} — window is now {out['window_now'][:70]!r}")

        return _fail(f"unknown action {action!r}. See the tool description.")
    except screen_browser.BrowserError as exc:
        _record({"tool": "browser", "action": action, "status": "error",
                 "what": f"browser {action}", "error": str(exc)})
        return _fail(str(exc))


SERVER = create_sdk_mcp_server("screen", "1.0.0", [computer, browser])

# What the work brain must have allow-listed for the two tools to be callable.
TOOL_NAMES = ("mcp__screen__computer", "mcp__screen__browser")
