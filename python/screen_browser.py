"""GOAT's grip on the browser — tabs as objects, not as pixels.

Clicking a tab strip by coordinate works right up until the strip scrolls, the
favicons shift, or there are twenty tabs. The browser already exposes the real
thing over the Chrome DevTools Protocol: a list of tabs with titles and URLs,
and verbs to open, close, focus, navigate and run JavaScript in any of them.
That is what this module speaks (Giorgi's order 2026-09-14, item 3).

Two layers, because his everyday browser is not debuggable:

1. CDP (this file's main body). Chrome 136+ refuses --remote-debugging-port on
   the DEFAULT profile — a deliberate cookie-theft mitigation — so GOAT drives
   its own profile directory instead. `launch` starts it, and everything after
   that is structured: tabs(), open_tab(), close_tab(), activate_tab(),
   navigate(), eval_js(), page_text(). Signed-in sessions in that profile
   persist between launches, so it is a real browser, not a throwaway.

2. The live-profile fallback (tab_search). His own already-running window can
   still be steered by keyboard: Ctrl+Shift+A is the browser's own tab search.
   Type a title fragment, press Enter, the tab comes forward — no debugging
   port, no relaunch, nothing lost.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

# Probed in order. 9222 is the convention; 9333 keeps a GOAT-launched browser
# clear of anything he started himself with the standard flag.
DEFAULT_PORTS = (9333, 9222, 9223, 9224)
GOAT_PORT = 9333
PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                           "goat-browser")

BROWSERS = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "brave": [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
}


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _url(url: str) -> str:
    """Add https:// to a bare host, but leave a real scheme alone.

    "://" is not the test: data:, file:, about: and chrome: have no slashes,
    and prefixing them produced "https://data:text/html,..." - an invalid URL
    the browser rejected outright (2026-09-14).
    """
    url = str(url).strip()
    return url if _SCHEME_RE.match(url) else "https://" + url


class BrowserError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# HTTP side of CDP — enough on its own for list / open / close / activate
# --------------------------------------------------------------------------

def _http(port: int, path: str, method: str = "GET", timeout: float = 2.0) -> str:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _ports_to_try() -> list[int]:
    env = os.environ.get("GOAT_CDP_PORT", "").strip()
    ports = [int(env)] if env.isdigit() else []
    ports += [p for p in DEFAULT_PORTS if p not in ports]
    return ports


def discover() -> dict:
    """Find a debuggable browser. {'running': False} when there is none."""
    for port in _ports_to_try():
        try:
            info = json.loads(_http(port, "/json/version", timeout=0.6))
        except Exception:  # noqa: BLE001 — a closed port is the normal case
            continue
        return {"running": True, "port": port,
                "browser": info.get("Browser", "?"),
                "protocol": info.get("Protocol-Version", "?")}
    return {"running": False, "port": None,
            "hint": "no debuggable browser - use action 'launch' to start one"}


def _port() -> int:
    d = discover()
    if not d["running"]:
        raise BrowserError(
            "no browser is listening for DevTools. Run the browser action "
            "'launch' first (it starts Chrome/Brave/Edge on GOAT's own profile "
            "at port %d); his everyday window cannot be attached to, because "
            "Chrome refuses remote debugging on the default profile." % GOAT_PORT)
    return d["port"]


def tabs(port: int | None = None) -> list[dict]:
    """Every open page, newest activation first as the browser reports it."""
    port = port or _port()
    raw = json.loads(_http(port, "/json/list"))
    out = []
    for t in raw:
        if t.get("type") != "page":
            continue  # service workers, extension background pages: not tabs
        out.append({"id": t["id"], "title": t.get("title", ""),
                    "url": t.get("url", ""),
                    "ws": t.get("webSocketDebuggerUrl", "")})
    return out


def _find(match: str, port: int) -> dict:
    """Resolve a tab by id, exact title, title fragment, or URL fragment."""
    found = tabs(port)
    if not found:
        raise BrowserError("no open tabs")
    m = str(match).strip()
    low = m.lower()
    for t in found:
        if t["id"] == m:
            return t
    for t in found:
        if t["title"].strip().lower() == low:
            return t
    for t in found:
        if low in t["title"].lower() or low in t["url"].lower():
            return t
    titles = ", ".join(f"{t['title'][:40]!r}" for t in found[:8])
    raise BrowserError(f"no tab matching {match!r}. Open: {titles}")


def open_tab(url: str, port: int | None = None) -> dict:
    port = port or _port()
    url = _url(url)
    target = "/json/new?" + urllib.parse.quote(url, safe="")
    try:
        # Chrome 111+ rejects GET here ("unsafe HTTP verb"); older builds only
        # know GET. Try the modern verb, fall back rather than fail.
        raw = _http(port, target, method="PUT")
    except urllib.error.HTTPError:
        raw = _http(port, target, method="GET")
    t = json.loads(raw)
    return {"id": t.get("id"), "url": t.get("url", url), "title": t.get("title", "")}


def close_tab(match: str, port: int | None = None) -> dict:
    port = port or _port()
    t = _find(match, port)
    _http(port, f"/json/close/{t['id']}")
    return {"closed": t["title"] or t["url"], "id": t["id"]}


def activate_tab(match: str, port: int | None = None) -> dict:
    """Bring a tab to the front of its window (and raise the window)."""
    port = port or _port()
    t = _find(match, port)
    _http(port, f"/json/activate/{t['id']}")
    try:  # the window itself may still be behind something else
        import screen_hands
        for w in screen_hands.windows():
            if t["title"] and t["title"][:28].lower() in w["title"].lower():
                screen_hands.focus_window(str(w["hwnd"]))
                break
    except Exception:  # noqa: BLE001 — raising the window is a nicety
        pass
    return {"active": t["title"] or t["url"], "id": t["id"], "url": t["url"]}


# --------------------------------------------------------------------------
# WebSocket side of CDP — navigate, evaluate, read a page
# --------------------------------------------------------------------------

def _command(ws_url: str, method: str, params: dict | None = None,
             timeout: float = 15.0) -> dict:
    """One CDP command on one tab. Opens the socket, asks, closes."""
    import websocket  # websocket-client, sync — these calls run in a thread
    conn = websocket.create_connection(ws_url, timeout=timeout,
                                       max_size=32 * 1024 * 1024)
    try:
        msg_id = 1
        conn.send(json.dumps({"id": msg_id, "method": method,
                              "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = json.loads(conn.recv())
            if data.get("id") != msg_id:
                continue  # an event fired in between; not our answer
            if "error" in data:
                raise BrowserError(f"{method}: {data['error'].get('message')}")
            return data.get("result", {})
        raise BrowserError(f"{method}: timed out after {timeout}s")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def navigate(match: str, url: str, port: int | None = None,
             settle: float = 1.2) -> dict:
    port = port or _port()
    t = _find(match, port)
    url = _url(url)
    _command(t["ws"], "Page.navigate", {"url": url})
    time.sleep(settle)
    now = _find(t["id"], port)
    return {"id": t["id"], "url": now["url"], "title": now["title"]}


def eval_js(match: str, expression: str, port: int | None = None) -> dict:
    """Run JavaScript in a tab and bring back the value.

    This is the precise half of browser control: read a field, count results,
    click a specific element by selector instead of guessing at a coordinate.
    """
    port = port or _port()
    t = _find(match, port)
    res = _command(t["ws"], "Runtime.evaluate", {
        "expression": expression, "returnByValue": True,
        "awaitPromise": True, "userGesture": True})
    if res.get("exceptionDetails"):
        desc = res["exceptionDetails"].get("exception", {}).get("description")
        raise BrowserError(desc or "JavaScript threw")
    result = res.get("result", {})
    return {"type": result.get("type"), "value": result.get("value"),
            "description": result.get("description")}


def page_text(match: str, limit: int = 6000, port: int | None = None) -> dict:
    """Visible text of a tab — reading a page without spending image tokens."""
    out = eval_js(match, "document.body ? document.body.innerText : ''", port)
    text = (out.get("value") or "")[:limit]
    return {"chars": len(text), "text": text}


def click_selector(match: str, selector: str, port: int | None = None) -> dict:
    """Click a real DOM element. Survives layout shifts that break coordinates."""
    expr = (
        "(() => { const el = document.querySelector(%s);"
        " if (!el) return 'NOT_FOUND';"
        " el.scrollIntoView({block:'center'}); el.click();"
        " return (el.innerText || el.value || el.tagName).slice(0, 80); })()"
        % json.dumps(selector))
    out = eval_js(match, expr, port)
    if out.get("value") == "NOT_FOUND":
        raise BrowserError(f"no element matches {selector!r}")
    return {"clicked": selector, "element": out.get("value")}


def element_coords(match: str, selector: str, port: int | None = None) -> dict:
    """Where an element sits ON SCREEN, so the real mouse can go there.

    The bridge between the two halves of browser control: find the thing by
    selector (reliable), then act on it with the hands (works on anything,
    including native widgets a DOM click cannot reach — file pickers, the
    print dialog, a PDF viewer).

    The viewport's screen origin is CALIBRATED, not assumed. window.screenY is
    the browser window's top in Chrome, not the viewport's, and the tab strip
    plus omnibox above the page are a different height on every setup — doing
    that arithmetic by hand put a click 70px above its target (measured
    2026-09-14). Instead: take the window's real client rectangle from Win32,
    and note that the page fills the bottom of it. Subtracting the page's own
    size from the client area gives the offset exactly, whatever the browser
    is wearing above it.
    """
    import screen_hands
    port = port or _port()
    t = _find(match, port)
    view = eval_js(t["id"], (
        "(() => { const el = document.querySelector(%s);"
        " if (!el) return null;"
        " el.scrollIntoView({block:'center'});"
        " const r = el.getBoundingClientRect(), d = window.devicePixelRatio || 1;"
        " return {left: r.left, top: r.top, w: r.width, h: r.height, dpr: d,"
        "         iw: window.innerWidth, ih: window.innerHeight,"
        "         text: (el.innerText || el.value || el.tagName).slice(0, 60)}; })()"
        % json.dumps(selector)), port)["value"]
    if not view:
        raise BrowserError(f"no element matches {selector!r}")

    title = t["title"][:30].lower()
    win = None
    for w in screen_hands.windows():
        low = w["title"].lower()
        if "chrome" in low or "brave" in low or "edge" in low:
            if not title or title in low:
                win = w
                break
            win = win or w
    if not win:
        raise BrowserError("the browser window is not on screen — is it minimized?")

    cl, ct, cw, ch = screen_hands.client_rect(win["hwnd"])
    dpr = view["dpr"] or 1
    page_w, page_h = view["iw"] * dpr, view["ih"] * dpr
    origin_x = cl + max(0, round((cw - page_w) / 2))   # page is centred
    origin_y = ct + max(0, round(ch - page_h))         # page sits at the bottom
    return {"x": round(origin_x + (view["left"] + view["w"] / 2) * dpr),
            "y": round(origin_y + (view["top"] + view["h"] / 2) * dpr),
            "w": round(view["w"] * dpr), "h": round(view["h"] * dpr),
            "text": view["text"], "window": win["title"][:60]}


def fill_selector(match: str, selector: str, value: str,
                  port: int | None = None) -> dict:
    """Set an input's value and fire the events frameworks listen for."""
    expr = (
        "(() => { const el = document.querySelector(%s);"
        " if (!el) return 'NOT_FOUND';"
        " el.focus(); el.value = %s;"
        " el.dispatchEvent(new Event('input', {bubbles:true}));"
        " el.dispatchEvent(new Event('change', {bubbles:true}));"
        " return el.value.slice(0, 80); })()"
        % (json.dumps(selector), json.dumps(value)))
    out = eval_js(match, expr, port)
    if out.get("value") == "NOT_FOUND":
        raise BrowserError(f"no element matches {selector!r}")
    return {"filled": selector, "value": out.get("value")}


# --------------------------------------------------------------------------
# Launching
# --------------------------------------------------------------------------

def _exe(browser: str) -> str:
    for path in BROWSERS.get(browser.lower(), []):
        if os.path.isfile(path):
            return path
    found = shutil.which(browser)
    if found:
        return found
    raise BrowserError(f"{browser} is not installed where GOAT expected it")


def launch(browser: str = "chrome", port: int = GOAT_PORT,
           url: str = "about:blank", profile: str | None = None,
           wait: float = 12.0) -> dict:
    """Start a debuggable browser on GOAT's own profile and wait for the port.

    Already running? Reuse it — relaunching would orphan the first instance's
    tabs and leave two browsers fighting over the profile lock.
    """
    live = discover()
    if live["running"]:
        return {**live, "reused": True}
    exe = _exe(browser)
    profile = profile or PROFILE_DIR
    os.makedirs(profile, exist_ok=True)
    args = [exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--remote-allow-origins=*",
            url]
    # Detach AND break out of the job object. Without the breakaway the browser
    # is a child of whatever shell started it and dies with it — measured
    # 2026-09-14: CDP answered, then the port went dead the moment the parent
    # exited. Not every job allows breakaway, so fall back rather than fail.
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    try:
        subprocess.Popen(args, creationflags=detached | breakaway,
                         close_fds=True)
    except OSError:
        subprocess.Popen(args, creationflags=detached, close_fds=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.4)
        d = discover()
        if d["running"]:
            return {**d, "reused": False, "profile": profile, "exe": exe}
    raise BrowserError(f"{browser} did not open a DevTools port within {wait}s")


def close_browser(port: int | None = None) -> dict:
    """Close the GOAT-driven browser (Browser.close — a clean shutdown)."""
    port = port or _port()
    ver = json.loads(_http(port, "/json/version"))
    ws = ver.get("webSocketDebuggerUrl")
    if not ws:
        raise BrowserError("browser endpoint exposes no WebSocket to close on")
    try:
        _command(ws, "Browser.close", timeout=5)
    except Exception:  # noqa: BLE001 — it dies mid-reply; that IS the success
        pass
    return {"closed": ver.get("Browser", "browser"), "port": port}


# --------------------------------------------------------------------------
# Live-profile fallback — his own window, no debugging port needed
# --------------------------------------------------------------------------

def tab_search(query: str, window: str | None = None) -> dict:
    """Jump to a tab in the browser he is ALREADY using, by name.

    Ctrl+Shift+A is Chrome/Brave/Edge's built-in tab search. Typing a fragment
    and pressing Enter switches to that tab — the one reliable way to reach his
    live, signed-in profile, which CDP is not allowed to touch.
    """
    import screen_hands
    if window:
        screen_hands.focus_window(window)
        time.sleep(0.25)
    fg = screen_hands.foreground() or {}
    screen_hands.press("ctrl+shift+a")
    time.sleep(0.5)
    screen_hands.type_text(query)
    time.sleep(0.6)  # the list filters as it types; give it a beat to settle
    screen_hands.press("enter")
    time.sleep(0.4)
    now = screen_hands.foreground() or {}
    return {"searched": query, "window_before": fg.get("title", ""),
            "window_now": now.get("title", "")}


def browser_windows() -> list[dict]:
    """His open browser windows, by title — including the non-debuggable ones."""
    import screen_hands
    marks = ("chrome", "brave", "edge", "firefox", "opera")
    out = []
    for w in screen_hands.windows():
        title = w["title"].lower()
        if any(m in title for m in marks) or title.endswith(" — brave"):
            out.append(w)
    return out


# --------------------------------------------------------------------------
# CLI
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
    p = argparse.ArgumentParser(prog="screen_browser",
                                description="GOAT browser control over CDP")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("tabs")
    lp = sub.add_parser("launch")
    lp.add_argument("--browser", default="chrome")
    lp.add_argument("--url", default="about:blank")
    o = sub.add_parser("open")
    o.add_argument("url")
    for name in ("close", "activate"):
        s = sub.add_parser(name)
        s.add_argument("match")
    n = sub.add_parser("navigate")
    n.add_argument("match")
    n.add_argument("url")
    e = sub.add_parser("eval")
    e.add_argument("match")
    e.add_argument("expression")
    t = sub.add_parser("text")
    t.add_argument("match")
    ts = sub.add_parser("tabsearch")
    ts.add_argument("query")
    sub.add_parser("windows")
    sub.add_parser("quit")

    a = p.parse_args(argv)
    dump = lambda v: print(json.dumps(v, indent=2, ensure_ascii=False))  # noqa: E731
    if a.cmd == "status":
        dump(discover())
    elif a.cmd == "tabs":
        dump([{k: v for k, v in t.items() if k != "ws"} for t in tabs()])
    elif a.cmd == "launch":
        dump(launch(a.browser, url=a.url))
    elif a.cmd == "open":
        dump(open_tab(a.url))
    elif a.cmd == "close":
        dump(close_tab(a.match))
    elif a.cmd == "activate":
        dump(activate_tab(a.match))
    elif a.cmd == "navigate":
        dump(navigate(a.match, a.url))
    elif a.cmd == "eval":
        dump(eval_js(a.match, a.expression))
    elif a.cmd == "text":
        dump(page_text(a.match))
    elif a.cmd == "tabsearch":
        dump(tab_search(a.query))
    elif a.cmd == "windows":
        dump(browser_windows())
    elif a.cmd == "quit":
        dump(close_browser())
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
