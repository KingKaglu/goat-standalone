"""Tests for GOAT's screen organ — eyes, hands, browser grip, safety gate.

Written to be safe to run while Giorgi is using the machine: nothing here
clicks, types, or presses a key on the real desktop. The hands are exercised
through the policy layer and through gated calls that are expected NOT to fire.
The one thing that does touch the real machine is a screenshot, which is
harmless by construction.

Run: py -3.13 test_screen.py
"""
from __future__ import annotations

import asyncio
import io
import json
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import screen_browser
import screen_hands
import screen_policy
import screen_tools

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


# --------------------------------------------------------------------------
# eyes
# --------------------------------------------------------------------------

def test_capture() -> None:
    png, meta = screen_hands.capture(max_edge=320)
    check("capture returns a PNG", png[:8] == b"\x89PNG\r\n\x1a\n", f"{len(png)} bytes")
    check("capture reports the real region",
          meta["region"]["width"] > 0 and meta["region"]["height"] > 0,
          str(meta["region"]))
    check("capture honours max_edge", max(meta["image_size"].values()) <= 320,
          str(meta["image_size"]))
    check("capture reports a usable scale", 0 < meta["scale"] <= 1.0,
          str(meta["scale"]))
    from PIL import Image
    img = Image.open(io.BytesIO(png))
    check("PNG decodes to the advertised size",
          img.size == (meta["image_size"]["width"], meta["image_size"]["height"]),
          str(img.size))

    region = (0, 0, 200, 120)
    png2, meta2 = screen_hands.capture(region=region, max_edge=0, grid=False)
    check("region capture is exactly the region asked for",
          meta2["image_size"] == {"width": 200, "height": 120}, str(meta2["image_size"]))
    check("region capture with max_edge=0 is unscaled", meta2["scale"] == 1.0)

    mons = screen_hands.monitors()
    check("monitors are enumerated", len(mons) >= 1, f"{len(mons)} found")
    check("monitor geometry is sane", all(m["width"] > 0 for m in mons))
    try:
        screen_hands.capture(monitor=99)
        check("capture rejects a monitor that is not there", False)
    except ValueError:
        check("capture rejects a monitor that is not there", True)


def test_grid_step() -> None:
    # The grid must stay readable at every zoom: ~90 drawn pixels per line.
    check("grid step grows as the shot shrinks",
          screen_hands._auto_step(1.0) <= screen_hands._auto_step(0.2),
          f"{screen_hands._auto_step(1.0)} vs {screen_hands._auto_step(0.2)}")
    check("grid step is a round number",
          screen_hands._auto_step(0.73) in (50, 100, 200, 250, 500, 1000))


def test_geometry() -> None:
    vx, vy, vw, vh = screen_hands.virtual_screen()
    check("virtual screen has size", vw > 0 and vh > 0, f"{vw}x{vh}")
    # The corners of the virtual desktop must map to the ends of the 0..65535
    # range, or every click lands offset — this is the multi-monitor trap.
    check("top-left maps to 0,0", screen_hands._abs_coords(vx, vy) == (0, 0))
    check("bottom-right maps to 65535,65535",
          screen_hands._abs_coords(vx + vw - 1, vy + vh - 1) == (65535, 65535))
    check("out-of-range coordinates clamp",
          screen_hands._abs_coords(vx - 5000, vy - 5000) == (0, 0))
    x, y = screen_hands.cursor_pos()
    check("cursor position reads back", isinstance(x, int) and isinstance(y, int),
          f"({x}, {y})")


# --------------------------------------------------------------------------
# hands (resolution only — nothing is actually pressed)
# --------------------------------------------------------------------------

def test_keys() -> None:
    check("named keys resolve", screen_hands._vk("enter") == 0x0D)
    check("letters resolve", screen_hands._vk("a") == ord("A"))
    check("function keys resolve", screen_hands._vk("f5") == 0x74)
    check("case and spacing are forgiven", screen_hands._vk(" CtRl ") == 0x11)
    try:
        screen_hands._vk("nope")
        check("an unknown key is rejected", False)
    except ValueError:
        check("an unknown key is rejected", True)
    # Arrows and Delete MUST carry the extended flag or the app receives the
    # numpad twin instead — the classic silent SendInput bug.
    for key in ("left", "right", "up", "down", "delete", "home", "end"):
        if screen_hands._vk(key) not in screen_hands._EXTENDED:
            check(f"{key} is marked extended", False)
            break
    else:
        check("arrows/delete/home/end are marked extended", True)
    check("unicode splits into UTF-16 units",
          len(screen_hands._utf16_units("ა")) == 1
          and len(screen_hands._utf16_units("😀")) == 2)


def test_self_window() -> None:
    """GOAT has to be able to find itself before it can step out of the way."""
    w = screen_hands.self_window()
    check("GOAT's own window is findable", w is not None,
          (w or {}).get("title", "not found"))
    if w:
        check("topmost state reads back",
              isinstance(screen_hands.is_topmost(w["hwnd"]), bool))
        before = screen_hands.is_topmost(w["hwnd"])
        aside = screen_hands.stand_aside(minimize=False)
        check("stand_aside drops always-on-top",
              aside["aside"] and not screen_hands.is_topmost(w["hwnd"]))
        back = screen_hands.step_back_in()
        check("step_back_in restores the on-top state exactly",
              back["restored"] and screen_hands.is_topmost(w["hwnd"]) == before)


def test_windows_list() -> None:
    wins = screen_hands.windows()
    check("windows are enumerated", len(wins) >= 1, f"{len(wins)} visible")
    check("every window has a title and a rect",
          all(w["title"] and w["width"] >= 0 for w in wins))
    try:
        screen_hands._match("this window does not exist at all")
        check("a missing window is an error", False)
    except ValueError:
        check("a missing window is an error", True)
    if wins:
        exact = wins[0]["title"]
        check("exact titles win over fragments",
              screen_hands._match(exact)["hwnd"] == wins[0]["hwnd"])
        check("hwnd lookup works",
              screen_hands._match(str(wins[0]["hwnd"]))["hwnd"] == wins[0]["hwnd"])


# --------------------------------------------------------------------------
# the safety gate
# --------------------------------------------------------------------------

def test_policy() -> None:
    auto, confirm = screen_policy.AUTO, screen_policy.CONFIRM
    cases = [
        # looking is never gated, whatever is on screen
        (("screenshot", {}, "TBC Bank - payment"), auto),
        (("cursor", {}, "PayPal checkout"), auto),
        (("move", {"x": 5, "y": 5}, "PayPal checkout"), auto),
        # ordinary work runs
        (("click", {"x": 10, "y": 10, "label": "Save"}, "Notepad"), auto),
        (("type", {"text": "hello world"}, "Notepad"), auto),
        (("key", {"text": "ctrl+s"}, "Notepad"), auto),
        # intent triggers
        (("click", {"label": "Pay now"}, "Notepad"), confirm),
        (("click", {"label": "Delete account"}, "Notepad"), confirm),
        (("click", {"label": "წაშლა"}, "Notepad"), confirm),
        (("fill_selector", {"selector": "#x", "value": "გადახდა"}, "x"), confirm),
        (("type", {"text": "4111 1111 1111 1111"}, "Notepad"), confirm),
        (("key", {"text": "ctrl+shift+delete"}, "Chrome"), confirm),
        (("key", {"text": "alt+f4"}, "Notepad"), confirm),
        # location triggers, even when the action looks innocent
        (("click", {"x": 1, "y": 1}, "TBC Bank | Internet Banking"), confirm),
        (("type", {"text": "hello"}, "Stripe Checkout"), confirm),
        (("click", {"x": 1, "y": 1}, "ბანკი - გადახდა"), confirm),
    ]
    bad = []
    for (action, args, window), want in cases:
        got, reason = screen_policy.classify(action, args, window)
        if got != want:
            bad.append(f"{action}/{args}/{window!r}: want {want}, got {got}")
        if got == confirm and not reason:
            bad.append(f"{action}: gated with no reason given")
    check(f"policy classifies all {len(cases)} cases", not bad, "; ".join(bad[:3]))

    check("describe names the action",
          "type" in screen_policy.describe("type", {"text": "hi"})
          and "(10, 20)" in screen_policy.describe("click", {"x": 10, "y": 20}))
    long = screen_policy.describe("type", {"text": "x" * 200})
    check("describe truncates long text", len(long) < 80, f"{len(long)} chars")


# --------------------------------------------------------------------------
# the tool surface
# --------------------------------------------------------------------------

async def test_tools() -> None:
    computer, browser = screen_tools.computer.handler, screen_tools.browser.handler

    r = await computer({"action": "screenshot", "max_edge": 300})
    kinds = [c["type"] for c in r["content"]]
    check("computer.screenshot returns an image and a note",
          kinds == ["image", "text"], str(kinds))
    check("the note explains the coordinate space",
          "REAL screen coordinates" in r["content"][1]["text"])

    r = await computer({"action": "windows"})
    check("computer.windows lists windows", "open windows" in r["content"][0]["text"])

    r = await computer({"action": "cursor"})
    check("computer.cursor reports a position", "mouse at" in r["content"][0]["text"])

    r = await computer({"action": "nonsense"})
    check("an unknown action is an error, not a crash", r.get("isError") is True)

    # Not fired: a bare click presses wherever the mouse already is, which is a
    # real click on his desktop. Assert the shape through the policy layer.
    check("a click with no coordinates is allowed (clicks where the mouse is)",
          screen_policy.classify("click", {}, "Notepad")[0] == screen_policy.AUTO)

    r = await computer({"action": "drag", "x": 1, "y": 2})
    check("a drag missing its endpoint is an error", r.get("isError") is True)

    # The gate must stop the hand BEFORE it moves. If this ever regresses, the
    # mouse jumps to 5,5 and clicks — which is exactly what must not happen.
    before = screen_hands.cursor_pos()
    r = await computer({"action": "click", "x": 5, "y": 5, "label": "Pay now"})
    after = screen_hands.cursor_pos()
    check("a gated click does not run", r.get("isError") is True and before == after,
          f"{before} -> {after}")
    check("the gate says what it wants and how to proceed",
          "CONFIRM FIRST" in r["content"][0]["text"]
          and "confirm=true" in r["content"][0]["text"])

    r = await computer({"action": "key", "text": "ctrl+shift+delete"})
    check("a gated keystroke does not run", r.get("isError") is True)

    r = await computer({"action": "log"})
    text = r["content"][0]["text"]
    check("the log records what was gated", "gated" in text, text[-120:])

    r = await computer({"action": "screenshot", "region": "not,a,region"})
    check("a malformed region is rejected cleanly", r.get("isError") is True)

    # Standing aside and coming back must never be gated - they are how GOAT
    # gets out of the way of its own work.
    for act in ("hide_self", "show_self"):
        check(f"{act} is never gated",
              screen_policy.classify(act, {}, "TBC Bank")[0] == screen_policy.AUTO)

    r = await computer({"action": "hide_self"})
    check("hide_self answers", isinstance(r.get("content"), list),
          r["content"][0]["text"][:60])
    r = await computer({"action": "show_self"})
    check("show_self answers", isinstance(r.get("content"), list),
          r["content"][0]["text"][:60])

    r = await browser({"action": "coords"})
    check("coords without a selector is an error", r.get("isError") is True)

    r = await browser({"action": "status"})
    check("browser.status answers", "running" in r["content"][0]["text"])

    r = await browser({"action": "open"})
    check("browser.open without a url is an error", r.get("isError") is True)

    r = await browser({"action": "nonsense"})
    check("an unknown browser action is an error", r.get("isError") is True)


def test_confirm_hook() -> None:
    """A live 'yes' from Giorgi must let the same call straight through."""
    asked = []

    def hook(action, reason, args):
        asked.append((action, reason))
        return True

    screen_tools.set_confirm_hook(hook)
    try:
        blocked = screen_tools._gate("click", {"x": 1, "y": 1, "label": "Pay now"})
        check("a live yes clears the gate", blocked is None)
        check("the hook was told what and why",
              asked and asked[0][0] == "click" and "pay" in asked[0][1].lower())
        screen_tools.set_confirm_hook(lambda *a: False)
        blocked = screen_tools._gate("click", {"x": 1, "y": 1, "label": "Pay now"})
        check("a live no blocks", blocked is not None and "said no" in blocked)
    finally:
        screen_tools.set_confirm_hook(None)
    blocked = screen_tools._gate("click", {"x": 1, "y": 1, "label": "Pay now",
                                           "confirm": True})
    check("confirm=true clears the gate", blocked is None)


def test_emit() -> None:
    """The left panel must see every action — that is the debugging loop."""
    seen = []
    screen_tools.set_emit(lambda kind, data: seen.append((kind, data)))
    try:
        screen_tools._record({"tool": "computer", "action": "click",
                              "what": "click (1, 2)", "status": "ok"})
    finally:
        screen_tools.set_emit(None)
    check("an action reaches the UI ledger",
          seen and seen[0][0] == "step" and "click (1, 2)" in seen[0][1],
          str(seen[:1]))


def test_browser_module() -> None:
    d = screen_browser.discover()
    check("browser discovery answers either way", "running" in d, json.dumps(d)[:90])
    if d["running"]:
        tabs = screen_browser.tabs()
        check("tabs are listed", isinstance(tabs, list), f"{len(tabs)} tabs")
        if tabs:
            check("a tab resolves by id",
                  screen_browser._find(tabs[0]["id"], d["port"])["id"] == tabs[0]["id"])
            try:
                screen_browser._find("zzz-no-such-tab-zzz", d["port"])
                check("a missing tab is an error", False)
            except screen_browser.BrowserError:
                check("a missing tab is an error", True)
    else:
        try:
            screen_browser.tabs()
            check("tabs without a browser explains how to start one", False)
        except screen_browser.BrowserError as exc:
            check("tabs without a browser explains how to start one",
                  "launch" in str(exc))
    check("a scheme-only URL is left alone",
          screen_browser._url("data:text/html,x") == "data:text/html,x"
          and screen_browser._url("file:///c:/x") == "file:///c:/x"
          and screen_browser._url("example.com") == "https://example.com")
    check("every browser GOAT knows has a path list",
          all(paths for paths in screen_browser.BROWSERS.values()))


def main() -> int:
    print("=== GOAT screen organ ===\n")
    test_capture()
    test_grid_step()
    test_geometry()
    test_keys()
    test_windows_list()
    test_self_window()
    test_policy()
    test_confirm_hook()
    test_emit()
    test_browser_module()
    asyncio.run(test_tools())
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
