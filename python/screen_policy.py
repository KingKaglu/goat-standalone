"""Where GOAT's screen hands stop and ask first.

Giorgi's rule for the machine has been "full access" since 2026-07-10, and
nothing here walks that back: no action is forbidden. But a mouse is a blunter
instrument than a shell command — a click at the wrong coordinate can send
money, wipe an account, or post something in his name, and unlike `rm` there is
no path to read back and sanity-check first. So screen actions get ONE gate
(his order 2026-09-14, item 4): the irreversible and the outward-facing ones
need a yes before they fire; everything else just runs.

Two independent triggers, because either alone has a blind spot:

- WHAT is being done — a label or keystroke that means delete, pay, send.
- WHERE it is being done — the foreground window. Inside a bank, a checkout,
  or a payment page, EVERY click and keystroke is confirm-tier, whatever it
  claims to be aimed at. This is the trigger that catches the case the model
  did not realize was dangerous.

The lists are plain data on purpose: Giorgi can add a word without reading a
line of logic.
"""
from __future__ import annotations

import re

AUTO = "auto"
CONFIRM = "confirm"

# Irreversible or outward-facing intent, in both languages he works in. Matched
# against the caller's own `label` for a click and against typed text.
RISKY_WORDS = [
    # destructive
    "delete", "remove", "uninstall", "erase", "wipe", "format", "reset",
    "factory", "revoke", "deactivate", "terminate", "drop database",
    "წაშლა", "წაშალე", "წაიშალოს", "გაუქმება", "წაშლის",
    # money
    "pay", "payment", "buy", "purchase", "order", "checkout", "subscribe",
    "donate", "transfer", "withdraw", "send money", "confirm payment",
    "place order", "გადახდა", "გადაიხადე", "ყიდვა", "შეკვეთა", "შეძენა",
    "გადარიცხვა", "ჩარიცხვა",
    # outward / identity
    "publish", "post", "tweet", "send email", "send message", "share",
    "submit", "sign out", "log out", "delete account",
    "გამოქვეყნება", "გაგზავნა", "გაზიარება", "გამოსვლა",
]

# Windows where the surroundings alone make everything sensitive.
RISKY_WINDOWS = [
    "bank", "ბანკი", "tbc", "bog", "liberty", "paypal", "stripe", "checkout",
    "payment", "billing", "wallet", "crypto", "binance", "metamask",
    "internet banking", "გადახდა", "საბანკო",
]

# Chords that destroy or escape in one press.
RISKY_KEYS = {
    "ctrl+shift+delete", "ctrl+shift+del",  # clear browsing data
    "shift+delete", "shift+del",            # permanent delete, no recycle bin
    "alt+f4",                               # closes whatever has focus
    "win+l",                                # locks him out mid-task
    "ctrl+alt+delete",
}

# A card number the model is about to type. 13-19 digits with optional spacing
# is the shape; this catches a paste into a payment form that no label mentions.
_CARD_RE = re.compile(r"(?:\d[ -]?){13,19}")


def _hit(text: str, words: list[str]) -> str | None:
    low = (text or "").lower()
    for w in words:
        if w in low:
            return w
    return None


def classify(action: str, args: dict, window_title: str = "") -> tuple[str, str]:
    """Return (tier, reason). tier is AUTO or CONFIRM; reason explains why.

    Never raises and never refuses — the caller decides what a CONFIRM means.
    """
    args = args or {}
    action = (action or "").lower()

    # Looking is always free. This is the half that must never be gated, or
    # GOAT loses the ability to check what it just did.
    if action in ("screenshot", "cursor", "pixel", "windows", "monitors",
                  "foreground", "wait", "log", "move", "hide_self",
                  "show_self", "coords"):
        return AUTO, ""

    where = _hit(window_title, RISKY_WINDOWS)
    if where and action in ("click", "double_click", "right_click", "drag",
                            "type", "key", "scroll", "middle_click"):
        return CONFIRM, (f"the active window looks like banking or checkout "
                         f"({where!r} in {window_title[:60]!r}) - every action "
                         f"there is confirmed first")

    if action == "key":
        combo = str(args.get("text") or args.get("combo") or "").lower()
        for chord in combo.split():
            if chord in RISKY_KEYS:
                return CONFIRM, f"{chord} is destructive or locks the session"
        return AUTO, ""

    if action == "type":
        text = str(args.get("text") or "")
        if _CARD_RE.search(text.replace(" ", " ")):
            return CONFIRM, "the text looks like a card number"
        word = _hit(text, RISKY_WORDS)
        if word:
            return CONFIRM, f"the text contains {word!r}"
        return AUTO, ""

    if action in ("click", "double_click", "right_click", "middle_click", "drag",
                  "click_selector", "fill_selector"):
        target = " ".join(str(args.get(k) or "") for k in
                          ("label", "selector", "value", "target", "description"))
        word = _hit(target, RISKY_WORDS)
        if word:
            return CONFIRM, f"the target mentions {word!r}"
        return AUTO, ""

    if action in ("close_browser", "close_tab", "window_state"):
        return AUTO, ""

    return AUTO, ""


def describe(action: str, args: dict) -> str:
    """One human line for the ledger and the log: what actually happened."""
    args = args or {}
    xy = ""
    if args.get("x") is not None and args.get("y") is not None:
        xy = f" ({args['x']}, {args['y']})"
    label = args.get("label") or args.get("selector") or args.get("target") or ""
    if action == "type":
        body = str(args.get("text", ""))
        return f"type {body[:48]!r}" + ("…" if len(body) > 48 else "")
    if action == "key":
        return f"key {args.get('text') or args.get('combo')}"
    if action == "screenshot":
        r = args.get("region")
        return "screenshot" + (f" of region {r}" if r else " of the screen")
    if action == "scroll":
        return f"scroll {args.get('clicks', 0)}{xy}"
    if action == "drag":
        return (f"drag ({args.get('x')}, {args.get('y')}) -> "
                f"({args.get('x2')}, {args.get('y2')})")
    return f"{action}{xy}" + (f" [{str(label)[:40]}]" if label else "")
