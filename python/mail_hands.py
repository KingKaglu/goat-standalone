"""Mail hands: the voice writes and sends Gmail on its own (2026-10-24).

His ask: "small tasks like open, close and send me an email — if Gemini can
handle it, that would be great." Before this, every email went voice ->
Claude -> browser clicks: 20-40 s and a pile of screenshots for what is one
URL and one button.

How it works, with no API keys and no new login:
- Gmail's own compose URL (mail.google.com/mail/u/N/?view=cm&to=&su=&body=)
  opens a filled-in draft in his default browser, already signed in.
- Sending presses the draft's Send button through UI Automation (the
  accessibility tree; fast_hands.click_named), falling back to Ctrl+Enter —
  Gmail's own send shortcut — and then waits for the compose view to go
  away, so "sent" is only ever said about a draft that actually left.

His own address lives in .goat-secrets.json ("my_email") and is resolved
HERE: the voice says "me", and his address never goes into Gemini's prompt.
"gmail_account" picks the signed-in account (the N in /u/N/, default 0).

Every function returns a short result string; "ERROR: ..." means it failed.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import webbrowser

from goat_paths import GOAT_ROOT

SECRETS = os.path.join(GOAT_ROOT, ".goat-secrets.json")
ADDR_RE = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
ME_RE = re.compile(r"^(?:me|myself|mine|my\s+(?:email|inbox|mail)|him|giorgi)$",
                   re.IGNORECASE)


def _secrets() -> dict:
    try:
        with open(SECRETS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def my_address() -> str:
    return (os.environ.get("GOAT_MY_EMAIL") or _secrets().get("my_email") or "").strip()


def account() -> int:
    try:
        return int(_secrets().get("gmail_account", 0))
    except (TypeError, ValueError):
        return 0


def resolve_to(to: str) -> tuple[list, list]:
    """("me, a@b.com") -> (["his@addr", "a@b.com"], []); anything that isn't
    an address comes back in the second list instead of being guessed."""
    good, bad = [], []
    for part in re.split(r"[,;]\s*|\s+and\s+", (to or "").strip()):
        p = part.strip().strip("<>\"'")
        if not p:
            continue
        if ME_RE.match(p):
            mine = my_address()
            (good if mine else bad).append(mine or p)
        elif ADDR_RE.match(p):
            good.append(p)
        else:
            bad.append(p)
    return good, bad


def compose_url(to: list, subject: str, body: str) -> str:
    q = urllib.parse.urlencode({"view": "cm", "fs": "1", "to": ",".join(to),
                                "su": subject or "", "body": body or ""},
                               quote_via=urllib.parse.quote)
    return f"https://mail.google.com/mail/u/{account()}/?{q}"


def _compose_window(timeout: float):
    """The browser window showing a Gmail draft ("Compose Mail - ..."),
    wherever it sits in the z-order — he may be typing in another window."""
    import screen_hands
    end = time.monotonic() + timeout
    while True:
        try:
            for w in screen_hands.windows():
                if w["title"].lower().startswith("compose mail") and not w["minimized"]:
                    return w
        except Exception:  # noqa: BLE001 — enumeration hiccup: try again
            pass
        if time.monotonic() >= end:
            return None
        time.sleep(0.3)


def open_draft(to: list, subject: str, body: str) -> str:
    try:
        webbrowser.open(compose_url(to, subject, body), new=2)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: couldn't open the browser ({e})"
    # A cold Gmail takes 15-25 s (measured 2026-10-24: >15 s in Brave).
    if _compose_window(35.0) is None:
        return "ERROR: the Gmail draft didn't open (signed out, or offline?)"
    return f"draft open to {', '.join(to)}"


def send_open_draft() -> str:
    """Press Send on the open draft, and only report "sent" once the
    compose view is gone.

    Everything is bound to the draft's own window handle: his everyday
    Brave hides web content from UI Automation, so Send is found by OCR —
    and OCR of whatever window is on top could "find" the word Send in a
    terminal. The title says "Compose Mail" before Gmail has drawn the
    form, so the Send button itself is the sign the draft is ready."""
    import fast_hands
    import screen_hands
    w = _compose_window(5.0)
    if w is None:
        return "ERROR: no Gmail draft open"
    hit = None
    end = time.monotonic() + 30.0
    while time.monotonic() < end:
        if not fast_hands._focus(w):
            return "ERROR: couldn't bring the draft to the front"
        time.sleep(0.3)
        try:
            hit = fast_hands.find_text("Send", w["hwnd"])
        except Exception:  # noqa: BLE001 — no OCR: Gmail's own shortcut below
            break
        if hit is not None and hit["name"] == "Send":   # the button, not "send" in his text
            break
        hit = None
        time.sleep(0.7)
    if hit is not None:
        screen_hands.click(hit["x"], hit["y"])
        how = "clicked Send"
    else:
        fast_hands._focus(w)
        screen_hands.press("ctrl+enter")     # Gmail's send shortcut
        how = "ctrl+enter"
    end = time.monotonic() + 10.0
    while time.monotonic() < end:
        time.sleep(0.4)
        try:
            title = next((x["title"] for x in screen_hands.windows()
                          if x["hwnd"] == w["hwnd"]), "")
        except Exception:  # noqa: BLE001
            title = ""
        if not title.lower().startswith("compose mail"):
            return f"sent ({how})"
    return "ERROR: pressed send but the draft is still open"


def send(to: list, subject: str, body: str) -> str:
    got = open_draft(to, subject, body)
    if got.startswith("ERROR"):
        return got
    return send_open_draft()
