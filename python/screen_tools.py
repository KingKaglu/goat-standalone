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

import game_hands
import game_pilot
import screen_browser
import screen_hands
import screen_policy
import screen_sense

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


SHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "live-shot.png")


def _show_shot(png: bytes) -> None:
    """Hand the screenshot the brain is about to read to the Live window
    (ui_qt.LiveWindow), so Giorgi sees what it sees. Written to a temp name
    and swapped in, so the UI never loads half a file."""
    if not _emit:
        return
    try:
        tmp = SHOT_PATH + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(png)
        os.replace(tmp, SHOT_PATH)
        _emit("work_shot", SHOT_PATH)
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
            "enum": ["look", "click_mark", "click_text", "find", "read",
                     "changes", "wait_for",
                     "screenshot", "click", "double_click", "right_click",
                     "middle_click", "move", "drag", "scroll", "type", "key",
                     "windows", "focus", "window_state", "foreground",
                     "cursor", "pixel", "monitors", "wait", "log",
                     "hide_self", "show_self"],
            "description": "What to do. See the tool description.",
        },
        "mark": {"type": "integer",
                 "description": "Mark number from the last look, for click_mark."},
        "gone": {"type": "string",
                 "description": "wait_for: wait until this text is OFF screen."},
        "image": {"type": "boolean",
                  "description": "look: false = the mark list only, no picture."},
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
                   "description": "Window title fragment or hwnd (focus, "
                                  "window_state, and the sight actions — "
                                  "default: the window he's on; \"screen\" = "
                                  "whole desktop)."},
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

REAL SIGHT — never guess a pixel. The screen is read as things: UI
Automation gives every control's exact rectangle, Windows OCR every word.
THE LOOP: look -> click_mark / click_text -> read the change report that
comes back (it says what changed on screen) -> look again only if needed.

  look       - the window he's on (target=title or "screen" for all) as
               numbered MARKS on a screenshot + a list "[12] button 'Save'
               (840,512)". image=false for the list alone (cheaper).
  click_mark - mark=N: click that element's exact centre (button=, clicks=2
               for double). Replies with what changed.
  click_text - text="Sign in": find it (controls, then drawn text) and click.
  find       - text=...: exact positions of matches, no picture.
  read       - every line of text in the window, no picture.
  changes    - what changed since you last looked (areas + new text there).
  wait_for   - text="Done" (appears) / gone="Loading" / neither = until the
               screen settles; seconds=timeout (max 30). Use instead of
               wait + screenshot loops.
click / key / type also reply with what they changed.

COORDINATES ARE REAL SCREEN PIXELS everywhere. For things that aren't text
or controls (a board game cell, a map, a canvas) use screenshot: its yellow
grid labels are real coordinates. A cyan crosshair marks the mouse.

more actions:
  screenshot   - picture. region="left,top,width,height" or monitor=N to zoom in
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


def _image(png: bytes) -> dict:
    return {"type": "image", "data": base64.b64encode(png).decode("ascii"),
            "mimeType": "image/png"}


async def _sight(action: str, args: dict) -> dict | None:
    """Real-sight actions (screen_sense, 2026-10-21). None = not one of them."""
    target = args.get("target")
    if action == "look":
        try:
            r = await asyncio.to_thread(
                screen_sense.look, target, True,
                args.get("image", True) is not False,
                int(args.get("max_edge") or 1400))
        except Exception as exc:  # noqa: BLE001
            _record({"tool": "computer", "action": action, "what": "look",
                     "status": "error", "error": str(exc)})
            return _fail(f"look failed: {exc}")
        _record({"tool": "computer", "action": action, "status": "ok",
                 "what": f"look — {r['count']} marks in {r['window'][:40]!r}"})
        content = [{"type": "text", "text": r["listing"]}]
        if r["png"]:
            _show_shot(r["png"])
            content.insert(0, _image(r["png"]))
        return {"content": content}

    if action in ("click_mark", "click_text"):
        button = str(args.get("button") or "left")
        clicks = int(args.get("clicks") or 1)
        try:
            if action == "click_mark":
                if args.get("mark") is None:
                    return _fail("click_mark needs mark=N from the last look")
                m = screen_sense.mark(int(args["mark"]))
                x, y = screen_sense._center(m["rect"])
                what = f"mark {args['mark']} {m['kind']} {m['name'][:50]!r}"
            else:
                want = args.get("text") or args.get("label")
                if not want:
                    return _fail('click_text needs text="what it says"')
                hits = await asyncio.to_thread(screen_sense.find, str(want), target)
                if not hits:
                    return _fail(f"no {want!r} on screen — look to see what is there")
                h = hits[0]
                x, y, m = h["x"], h["y"], {"name": h["name"]}
                what = f"{h['kind']} {h['name'][:50]!r} ({h['src']})"
        except ValueError as exc:
            return _fail(str(exc))
        gate_args = dict(args, x=x, y=y, label=args.get("label") or m["name"])
        out = await _run("computer", action, gate_args,
                         screen_sense.act_and_report,
                         lambda: screen_hands.click(x, y, button, clicks))
        if isinstance(out, dict):
            return out
        more = ""
        if action == "click_text" and len(hits) > 1:
            more = " Other matches: " + ", ".join(
                f"{o['name'][:30]!r} ({o['x']},{o['y']})" for o in hits[1:4]) + "."
        return _text(f"clicked {what} at ({x},{y}). {out[1]}.{more}")

    if action == "find":
        want = args.get("text") or args.get("label")
        if not want:
            return _fail('find needs text="what to look for"')
        try:
            hits = await asyncio.to_thread(screen_sense.find, str(want), target, 8)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"find failed: {exc}")
        if not hits:
            return _text(f"{want!r} is not on screen")
        return _text("\n".join(
            f"{i}. {h['kind']} {h['name'][:60]!r} at ({h['x']},{h['y']}) [{h['src']}]"
            + (f" in line {h['line'][:60]!r}" if h.get("line") else "")
            for i, h in enumerate(hits, 1)))

    if action == "read":
        try:
            return _text(await asyncio.to_thread(screen_sense.read, target))
        except Exception as exc:  # noqa: BLE001
            return _fail(f"read failed: {exc}")

    if action == "changes":
        try:
            return _text(await asyncio.to_thread(screen_sense.changes, target))
        except Exception as exc:  # noqa: BLE001
            return _fail(f"changes failed: {exc}")

    if action == "wait_for":
        try:
            return _text(await asyncio.to_thread(
                screen_sense.wait_for, args.get("text"), args.get("gone"),
                not args.get("text") and not args.get("gone"),
                float(args.get("seconds") or 10), target))
        except Exception as exc:  # noqa: BLE001
            return _fail(f"wait_for failed: {exc}")
    return None


@tool("computer", COMPUTER_DESC, COMPUTER_SCHEMA)
async def computer(args: dict) -> dict:
    action = str(args.get("action", "")).strip().lower()
    if not action:
        return _fail("action is required")

    def _xy():
        x, y = args.get("x"), args.get("y")
        return (int(x), int(y)) if x is not None and y is not None else (None, None)

    sight = await _sight(action, args)
    if sight is not None:
        return sight

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
        _show_shot(png)
        r, s = meta["region"], meta["image_size"]
        _record({"tool": "computer", "action": action, "status": "ok",
                 "what": screen_policy.describe(action, args),
                 "result": f"{r['width']}x{r['height']} -> {s['width']}x{s['height']}"})
        note = (f"Screen area {r['width']}x{r['height']} at ({r['left']}, {r['top']}), "
                f"shown at {s['width']}x{s['height']} (scale {meta['scale']}). "
                f"Grid labels are REAL screen coordinates"
                + (f", every {meta['grid_step']}px. " if meta['grid_step'] else ". ")
                + f"Mouse is at ({meta['cursor']['x']}, {meta['cursor']['y']}). "
                "To click a button, link or text here, don't compute "
                "coordinates from this picture: use click_text text=... or "
                "look + click_mark (exact).")
        return {"content": [
            {"type": "image", "data": base64.b64encode(png).decode("ascii"),
             "mimeType": "image/png"},
            {"type": "text", "text": note}]}

    if action in ("click", "double_click", "right_click", "middle_click"):
        x, y = _xy()
        button = {"right_click": "right", "middle_click": "middle"}.get(
            action, str(args.get("button") or "left"))
        clicks = 2 if action == "double_click" else int(args.get("clicks") or 1)
        # A raw coordinate is a guess: land it on the real element instead.
        snapped = ""
        if x is not None and y is not None:
            try:
                hit = await asyncio.wait_for(asyncio.to_thread(
                    screen_sense.snap, x, y, args.get("label")), 2.5)
            except Exception:  # noqa: BLE001 — no snap: the guess stands
                hit = None
            if hit:
                snapped = (f" Your ({x},{y}) missed; snapped onto {hit[2]} at "
                           f"({hit[0]},{hit[1]}). Next time: look + click_mark.")
                x, y = hit[0], hit[1]
                args = dict(args, x=x, y=y)
        out = await _run("computer", action, args, screen_sense.act_and_report,
                         lambda: screen_hands.click(x, y, button, clicks))
        if isinstance(out, dict):
            return out
        return _text(f"{action} at {out[0]} ({button}, {clicks}x).{snapped} "
                     f"{out[1]}")

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
        out = await _run("computer", action, args, screen_sense.act_and_report,
                         lambda: screen_hands.type_text(str(text)))
        if isinstance(out, dict):
            return out
        return _text(f"typed {out[0]} characters. {out[1]}")

    if action == "key":
        combo = args.get("text") or args.get("label")
        if not combo:
            return _fail('key needs text, e.g. text="ctrl+s"')
        out = await _run("computer", action, args, screen_sense.act_and_report,
                         lambda: screen_hands.press(str(combo),
                                                    int(args.get("clicks") or 1)))
        if isinstance(out, dict):
            return out
        return _text(f"pressed {out[0]}. {out[1]}")

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


# --------------------------------------------------------------------------
# game (2026-10-21, his order: "I want it to be able to play games")
# --------------------------------------------------------------------------

GAME_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string",
                   "enum": ["check", "keys", "mouse", "autopilot", "status",
                            "stop", "snip", "probe"],
                   "description": "What to do. See the tool description."},
        "target": {"type": "string",
                   "description": "Game window title fragment (default: the "
                                  "window he's on)."},
        "seq": {"type": "string",
                "description": "keys: e.g. \"w:800 space*3 a+d:300 wait:200 "
                               "lclick mouse:+120,-30 down:shift\"."},
        "dx": {"type": "integer", "description": "mouse: relative move X."},
        "dy": {"type": "integer", "description": "mouse: relative move Y."},
        "x": {"type": "integer", "description": "mouse: absolute X (click)."},
        "y": {"type": "integer", "description": "mouse: absolute Y (click)."},
        "button": {"type": "string", "enum": ["left", "right", "middle", "none"]},
        "plan": {"type": "object",
                 "description": "autopilot: {fps, stop_after_s, rules:[...]}."},
        "region": {"type": "string",
                   "description": "snip/probe: \"left,top,width,height\"."},
        "name": {"type": "string", "description": "snip: template name."},
        "frame": {"type": "boolean",
                  "description": "status: include the latest game frame."},
    },
    "required": ["action"],
    "additionalProperties": False,
}

GAME_DESC = """Play games on his laptop: game-grade input + a reflex autopilot.

NEVER for online games with anti-cheat (BattlEye, Easy Anti-Cheat, Vanguard,
FACEIT…): every action checks and refuses — tell him in one line why
(cheating on real players, gets his account banned). Single-player, offline
and browser games only. His own keyboard/mouse input instantly stops play
and releases every key. Games must run windowed or borderless (exclusive
fullscreen captures black). Call computer hide_self first.

HOW TO PLAY
- Slow / turn-based (chess, cards, puzzles, strategy, menus): play it
  yourself — computer look/screenshot to see, click_mark/click, game keys.
- Real-time (platformers, runners, shooters, rhythm, clickers): you are too
  slow (seconds per turn). Write an autopilot plan of reflex rules, start it,
  then check status every so often and rewrite the rules when play goes badly.

actions:
  check     - is the game window OK to drive; its view rect in real pixels.
  keys      - seq= scancode keys games actually feel: "w:800" holds 800 ms,
              "a+d:300" chord, "space*3", "wait:200", "lclick"/"rclick:150",
              "mouse:+120,-30" (relative camera move), "down:shift"/"up:shift"
              (stays held across calls). One call < 30 s.
  mouse     - dx/dy relative (camera/aim) or x/y absolute; button= to click.
  autopilot - plan={"fps":30,"stop_after_s":120,"rules":[RULE,...]}. Each
              frame, rules are tried in order; the first that fires acts
              (add "continue":true to let later rules fire too).
              RULE = {"name":..., "if":COND, "do":ACT, "cooldown_ms":120,
                      "max_hits":N, "after_s":S, "until_s":S}
              (after_s/until_s = seconds since start: games speed up, so
              give each phase its own rule, e.g. a jump zone further ahead
              later on.) Probe colours first — themes/dark mode change them.
              COND (coordinates = real screen pixels; "in":[x,y,w,h] limits
              the area, default the game view):
                {"color":[r,g,b],"tol":30,"min_px":12,"in":[...]} -> match =
                   centre of the biggest blob of that colour
                {"template":"games/templates/x.png","thr":0.8} (see snip)
                {"pixel":[x,y],"rgb":[r,g,b],"tol":25,"not":false}
                {"change":18,"in":[...]}  mean brightness change vs last frame
                {"text":"game over","in":[...]}  OCR, at most twice a second
                {"every_ms":1000}   {"always":true}
              ACT: {"keys":"space"} | {"click":"match"|[x,y]} |
                   {"aim":"match","gain":0.5,"then_click":true} (relative,
                   from view centre) | {"move_to":"match"} | "stop"
  status    - running?, fps, hits per rule, recent events, why it stopped;
              frame=true adds the latest frame.
  stop      - stop the autopilot and release everything.
  snip      - region= + name=: save that screen crop as a template.
  probe     - region=: the main colours there (to write colour rules), or
              x,y: the exact colour of one pixel.
"""


def _game_window(args: dict) -> dict:
    win = screen_sense.target(args.get("target"))
    if not win.get("hwnd"):
        raise ValueError("name the game window (target=...)")
    return win


@tool("game", GAME_DESC, GAME_SCHEMA)
async def game(args: dict) -> dict:
    action = str(args.get("action", "")).strip().lower()
    p = game_pilot.PILOT
    try:
        if action in ("status", "stop"):
            if action == "stop":
                p.stop("stopped by GOAT")
                return _text("autopilot stopped, every key and button released")
            s, png = p.status(bool(args.get("frame")))
            text = json.dumps(s, ensure_ascii=False, default=str)
            return {"content": ([_image(png)] if png else [])
                    + [{"type": "text", "text": text}]}

        if action == "probe":
            return _text(await asyncio.to_thread(_probe, args))

        if action == "snip":
            return _text(await asyncio.to_thread(_snip, args))

        win = await asyncio.to_thread(_game_window, args)
        why = await asyncio.to_thread(game_hands.anticheat_block, win)
        if why:
            _record({"tool": "game", "action": action, "status": "gated",
                     "what": f"game {action} refused", "reason": why})
            return _fail(game_hands.REFUSAL.format(why=why))

        if action == "check":
            x, y, w, h = screen_hands.client_rect(win["hwnd"])
            return _text(f"OK to drive {win['title'][:60]!r} (hwnd {win['hwnd']}): "
                         f"view {w}x{h} at ({x},{y}). No anti-cheat found.")

        blocked = _gate(action, args)
        if blocked:
            return _fail(blocked)
        await asyncio.to_thread(screen_hands.stand_aside)

        if action == "keys":
            seq = args.get("seq") or args.get("text")
            if not seq:
                return _fail('keys needs seq="w:800 space"')
            out = await asyncio.to_thread(_play_seq, win, str(seq))
            _record({"tool": "game", "action": action, "status": "ok",
                     "what": f"game keys {str(seq)[:40]}", "result": out})
            return _text(out)

        if action == "mouse":
            out = await asyncio.to_thread(_mouse, win, args)
            _record({"tool": "game", "action": action, "status": "ok",
                     "what": "game mouse", "result": out})
            return _text(out)

        if action == "autopilot":
            plan = args.get("plan")
            if not isinstance(plan, dict):
                return _fail("autopilot needs plan={fps, stop_after_s, rules}")
            out = await asyncio.to_thread(p.start, plan, win)
            _record({"tool": "game", "action": action, "status": "ok",
                     "what": f"autopilot {len(plan.get('rules') or [])} rules",
                     "result": out})
            return _text(out)
    except PermissionError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 — a failed hand reports, never dies
        _record({"tool": "game", "action": action, "status": "error",
                 "what": f"game {action}", "error": str(exc)})
        return _fail(f"game {action} failed: {exc}")
    return _fail(f"unknown action {action!r}")


def _play_seq(win: dict, seq: str) -> str:
    """Focus the game, play the sequence; his input cuts it short."""
    import threading
    stop = threading.Event()
    screen_hands.focus_window(str(win["hwnd"]))
    time.sleep(0.1)
    watch = game_hands.HumanWatch(lambda why: stop.set())
    watch.start()
    try:
        out = game_hands.run(seq, stop)
    finally:
        watch.stop()
    if watch.fired.is_set():
        game_hands.release_all()
        return f"{out} — {watch.reason}, so I let go of everything"
    return out


def _mouse(win: dict, args: dict) -> str:
    screen_hands.focus_window(str(win["hwnd"]))
    button = str(args.get("button") or "none")
    if args.get("dx") is not None or args.get("dy") is not None:
        game_hands.mouse_rel(int(args.get("dx") or 0), int(args.get("dy") or 0))
        did = f"moved {args.get('dx') or 0},{args.get('dy') or 0}"
    elif args.get("x") is not None and args.get("y") is not None:
        screen_hands.move(int(args["x"]), int(args["y"]))
        did = f"moved to ({args['x']},{args['y']})"
    else:
        did = "stayed"
    if button != "none":
        game_hands.button_down(button)
        time.sleep(0.04)
        game_hands.button_up(button)
        did += f", {button} click"
    return did


def _region(args: dict) -> tuple:
    parts = [int(float(v)) for v in str(args.get("region") or "").split(",")]
    if len(parts) != 4:
        raise ValueError('region must be "left,top,width,height"')
    return tuple(parts)


def _snip(args: dict) -> str:
    import re as _re
    from PIL import Image
    l, t, w, h = _region(args)
    name = _re.sub(r"[^\w-]", "_", str(args.get("name") or "template"))[:40]
    raw = screen_sense._grab((l, t, w, h))
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    rel = f"games/templates/{name}.png"
    path = os.path.join(game_pilot.WORKSPACE, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)
    return f'saved {w}x{h} template: use {{"template": "{rel}"}}'


def _probe(args: dict) -> str:
    import numpy as np
    if args.get("x") is not None and args.get("y") is not None and not args.get("region"):
        r, g, b = screen_hands.pixel(int(args["x"]), int(args["y"]))
        return f"pixel ({args['x']},{args['y']}) = rgb({r},{g},{b})"
    l, t, w, h = _region(args)
    raw = screen_sense._grab((l, t, w, h))
    a = np.frombuffer(raw.bgra, np.uint8).reshape(raw.height, raw.width, 4)[..., :3]
    q = (a[..., ::-1] // 24 * 24 + 12).reshape(-1, 3)     # RGB, 24-level bins
    vals, counts = np.unique(q, axis=0, return_counts=True)
    order = np.argsort(-counts)[:8]
    total = counts.sum()
    return "main colours (rgb, share): " + ", ".join(
        f"({int(vals[i][0])},{int(vals[i][1])},{int(vals[i][2])}) "
        f"{100 * counts[i] / total:.0f}%" for i in order)


SERVER = create_sdk_mcp_server("screen", "1.0.0", [computer, browser, game])

# What the work brain must have allow-listed for the tools to be callable.
TOOL_NAMES = ("mcp__screen__computer", "mcp__screen__browser",
              "mcp__screen__game")
