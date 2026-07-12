"""Local brain's hands — machine + internet tools the local model calls
through Ollama tool-calling.

v1 (2026-07-11) was a closed whitelist. v2 (2026-07-12) opened files, shell,
and web. v3 (2026-07-12, Giorgi's explicit order: "i dont want my local Ai
to have any guardlines ... full access ... do not escalate until i say so")
drops the last block. His machine, his sole account, his call: every command
the model forms runs. NOTHING here refuses or escalates on its own anymore.

FOOTGUN (owner is aware, chose this): the local model is a 4B and is known
to hallucinate. With the wall gone, a hallucinated `format C:` / Defender
disable / shadow-copy delete will EXECUTE. If that bites, re-enable a typed
human confirm on nuclear-tier commands (git history has the old
DESTRUCTIVE_RE) — that's a confirm, not a refusal, so it honors "do what I
say" while catching the model's own mistakes.
"""
import ctypes
import html
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
import webbrowser

# ---- tool schemas (Ollama /api/chat "tools" format) ----
TOOLS = [
    {"type": "function", "function": {
        "name": "open_url",
        "description": "Open a website in the default browser. Use for any "
                       "site: youtube, google, github, fasmetri, etc.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description":
                    "Full URL, e.g. https://youtube.com"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "open_app",
        "description": "Open a desktop application by name. Known apps: "
                       "notepad, calculator, explorer, browser, terminal, "
                       "settings, vscode, spotify, task manager.",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string"}}, "required": ["app"]}}},
    {"type": "function", "function": {
        "name": "volume",
        "description": "Change system volume: up, down, or mute toggle.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["up", "down", "mute"]},
            "steps": {"type": "integer", "description":
                      "1-25 key presses (each ~2%), default 5"}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "media",
        "description": "Control media playback: play_pause, next, prev, stop.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string",
                       "enum": ["play_pause", "next", "prev", "stop"]}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "brightness",
        "description": "Set screen brightness to a percent (0-100).",
        "parameters": {"type": "object", "properties": {
            "percent": {"type": "integer"}}, "required": ["percent"]}}},
    {"type": "function", "function": {
        "name": "lock_screen",
        "description": "Lock the Windows session (screen lock).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the live internet. ALWAYS use this for "
                       "current facts, news, prices, versions, weather — "
                       "never guess from memory.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Download a web page and return its readable text. "
                       "Use after web_search to read a result.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file from disk (up to 50 KB).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description":
                     "Absolute Windows path, e.g. C:\\Users\\user\\notes.txt"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write text to a file (creates folders; an existing "
                       "file is backed up to <name>.goat-bak first).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files and folders in a directory, with real "
                       "file sizes. Use this for 'biggest/smallest file' "
                       "questions — read the sizes it returns, never guess.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "delete_file",
        "description": "Delete a file or folder (folders go recursively). "
                       "Use this when he asks to delete/remove something — "
                       "call it, then report the tool's result.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description":
                     "Absolute Windows path of the file or folder"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a PowerShell command on this Windows machine "
                       "and return its output (30 s limit). Use for "
                       "anything the other tools don't cover.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "set_ui_color",
        "description": "Recolor GOAT's OWN interface live. Use when he asks "
                       "to change your text, accent, or background color "
                       "(e.g. 'make your text blue'). This is your own app — "
                       "never code, never escalate for it.",
        "parameters": {"type": "object", "properties": {
            "part": {"type": "string", "enum":
                     ["text", "accent", "background"], "description":
                     "text = main reply text, accent = highlight color, "
                     "background = window background"},
            "color": {"type": "string", "description":
                      "CSS color name (blue, crimson, teal) or hex #1e90ff"}},
            "required": ["part", "color"]}}},
    {"type": "function", "function": {
        "name": "resize_interface",
        "description": "Resize GOAT's OWN app window/interface (fonts and "
                       "controls). Use when he asks to make YOUR UI, text, "
                       "or icons bigger or smaller — this is your own app, "
                       "not Windows settings. Either set an absolute percent "
                       "(150 = 150%) or a relative change (bigger:1.5 = 50% "
                       "bigger, bigger:0.8 = 20% smaller).",
        "parameters": {"type": "object", "properties": {
            "percent": {"type": "integer", "description":
                        "Absolute size, 70-250. Omit if using 'bigger'."},
            "bigger": {"type": "number", "description":
                       "Relative multiplier, e.g. 1.5 for 50% bigger."}}}}},
]

# Set by goat_app so the UI tools can reach the Qt window.
_ui_scale_cb = None
_ui_color_cb = None


def set_ui_scale_callback(cb):
    global _ui_scale_cb
    _ui_scale_cb = cb


def set_ui_color_callback(cb):
    global _ui_color_cb
    _ui_color_cb = cb

# No command wall (his order 2026-07-12: "i dont want my local Ai to have any
# guardlines ... full access"). Every command the model forms runs. See the
# module docstring for the 4B-hallucination footgun and how to re-add a
# human confirm (not a refusal) if he ever wants one.

READ_CAP = 50_000
OUT_CAP = 4_000
FETCH_CAP = 8_000
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GOAT/1.0"}

# Closed executable map — names only, no user-supplied paths ever launch.
_APPS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe", "calc": "calc.exe",
    "explorer": "explorer.exe", "files": "explorer.exe",
    "browser": None,  # default browser via about:blank
    "terminal": "wt.exe", "cmd": "cmd.exe", "powershell": "powershell.exe",
    "settings": "ms-settings:",
    "task manager": "taskmgr.exe", "taskmgr": "taskmgr.exe",
    "vscode": "code", "code": "code",
    "spotify": "spotify:",
}

_VK = {"up": 0xAF, "down": 0xAE, "mute": 0xAD,
       "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2}


def _press(vk: int, times: int = 1):
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP


# ---- pure resolvers (testable, no side effects) ----
def resolve_url(url: str) -> str | None:
    """http(s) only — file://, shell:, javascript: etc. never open."""
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    # Bare domain like "youtube.com" — the model does this often. The ":"
    # check keeps other schemes (javascript:, file:, shell:) from smuggling
    # through as "domains".
    if "." in url and ":" not in url and not url.startswith("/"):
        return "https://" + url
    return None


def resolve_app(app: str) -> str | None:
    return _APPS.get((app or "").strip().lower())


def execute(name: str, args: dict) -> str:
    """Run one whitelisted tool call; returns a short result string for the
    model. Unknown tool/app/URL returns an error string — the model is told
    to ESCALATE in that case."""
    args = args or {}
    try:
        if name == "open_url":
            url = resolve_url(str(args.get("url", "")))
            if not url:
                return "ERROR: only http/https URLs can be opened"
            webbrowser.open(url)
            return f"opened {url}"
        if name == "open_app":
            target = resolve_app(str(args.get("app", "")))
            if target is None and str(args.get("app", "")).strip().lower() != "browser":
                return ("ERROR: unknown app — not in the safe list; "
                        "this needs the working brain")
            if target is None:
                webbrowser.open("about:blank")
                return "opened the browser"
            if target.endswith(":"):
                os.startfile(target)  # URI scheme (ms-settings:, spotify:)
            elif target == "code":
                subprocess.Popen("code", shell=True)  # .cmd shim needs shell
            else:
                subprocess.Popen([target])
            return f"opened {args.get('app')}"
        if name == "volume":
            action = str(args.get("action", ""))
            if action not in ("up", "down", "mute"):
                return "ERROR: volume action must be up/down/mute"
            steps = 1 if action == "mute" else max(
                1, min(25, int(args.get("steps") or 5)))
            _press(_VK[action], steps)
            return f"volume {action}" + ("" if action == "mute"
                                         else f" by ~{steps * 2}%")
        if name == "media":
            action = str(args.get("action", ""))
            if action not in ("play_pause", "next", "prev", "stop"):
                return "ERROR: media action must be play_pause/next/prev/stop"
            _press(_VK[action])
            return f"media {action} sent"
        if name == "brightness":
            pct = max(0, min(100, int(args.get("percent") or 50)))
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-WmiObject -Namespace root/WMI -Class "
                 "WmiMonitorBrightnessMethods).WmiSetBrightness(1,"
                 f"{pct})"],
                capture_output=True, timeout=10)
            return (f"brightness set to {pct}%" if r.returncode == 0
                    else "ERROR: brightness control not available")
        if name == "lock_screen":
            ctypes.windll.user32.LockWorkStation()
            return "screen locked"
        if name == "web_search":
            return _web_search(str(args.get("query", "")))
        if name == "fetch_url":
            return _fetch_url(str(args.get("url", "")))
        if name == "read_file":
            return _read_file(str(args.get("path", "")))
        if name == "write_file":
            return _write_file(str(args.get("path", "")),
                               str(args.get("content", "")))
        if name == "list_dir":
            return _list_dir(str(args.get("path", "")))
        if name == "delete_file":
            return _delete_file(str(args.get("path", "")))
        if name == "run_command":
            return _run_command(str(args.get("command", "")))
        if name == "set_ui_color":
            if _ui_color_cb is None:
                return "ERROR: UI not available"
            part = str(args.get("part", "")).strip().lower()
            color = str(args.get("color", "")).strip()
            if part not in ("text", "accent", "background"):
                return "ERROR: part must be text, accent, or background"
            if not color:
                return "ERROR: no color given"
            ok = _ui_color_cb(part, color)
            return (f"{part} color set to {color}" if ok
                    else f"ERROR: '{color}' isn't a color I recognize")
        if name == "resize_interface":
            if _ui_scale_cb is None:
                return "ERROR: UI not available"
            if args.get("bigger") is not None:
                _ui_scale_cb("*" + str(float(args["bigger"])))
                return f"interface scaled by {args['bigger']}x"
            pct = int(args.get("percent") or 100)
            _ui_scale_cb(str(pct / 100.0))
            return f"interface size set to {pct}%"
        return f"ERROR: unknown tool {name}"
    except Exception as e:  # noqa: BLE001 — a failed hand must not kill the turn
        return f"ERROR: {e}"


# ---- internet ----
def _web_search(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "ERROR: empty search query"
    # DuckDuckGo HTML endpoint — no key. Must be POST: the GET form now
    # returns a JS shell with no results (measured 2026-07-12).
    body = urllib.parse.urlencode({"q": query}).encode()
    try:
        req = urllib.request.Request(
            "https://html.duckduckgo.com/html/", data=body, headers=_UA)
        with urllib.request.urlopen(req, timeout=15) as r:
            page = r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return f"ERROR: search failed ({e})"
    hits = re.findall(
        r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snips = re.findall(
        r'result__snippet[^>]*>(.*?)</a>', page, re.S)
    if not hits:
        return "no results found"
    out = []
    for i, (href, title) in enumerate(hits[:6]):
        t = html.unescape(re.sub("<.*?>", "", title)).strip()
        s = (html.unescape(re.sub("<.*?>", "", snips[i])).strip()
             if i < len(snips) else "")
        if "uddg=" in href:
            u = urllib.parse.unquote(re.sub(r"^.*uddg=", "", href).split("&")[0])
        else:
            u = ("https:" + href) if href.startswith("//") else href
        out.append(f"- {t}\n  {s}\n  {u}")
    return "\n".join(out)


def _fetch_url(url: str) -> str:
    url = resolve_url(url)
    if not url:
        return "ERROR: only http/https URLs can be fetched"
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(1_500_000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return f"ERROR: fetch failed ({e})"
    raw = re.sub(r"(?is)<(script|style|head|nav|footer).*?</\1>", " ", raw)
    text = html.unescape(re.sub("<.*?>", " ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:FETCH_CAP] or "ERROR: page had no readable text"


# ---- filesystem ----
def _read_file(path: str) -> str:
    path = (path or "").strip().strip('"')
    if not path:
        return "ERROR: no path"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(READ_CAP + 1)
    except FileNotFoundError:
        return f"ERROR: no such file: {path}"
    except IsADirectoryError:
        return f"ERROR: that's a folder — use list_dir: {path}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    if len(data) > READ_CAP:
        return data[:READ_CAP] + "\n…[truncated at 50 KB]"
    return data or "[empty file]"


def _write_file(path: str, content: str) -> str:
    path = (path or "").strip().strip('"')
    if not path:
        return "ERROR: no path"
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if os.path.exists(path):
            try:
                os.replace(path, path + ".goat-bak")
            except OSError:
                pass  # backup is best-effort, never blocks the write
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return f"wrote {len(content)} chars to {path}"


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def _list_dir(path: str) -> str:
    path = (path or "").strip().strip('"') or "."
    try:
        entries = sorted(os.listdir(path))
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    if not entries:
        return "[empty folder]"
    # Real sizes in the listing — measured 2026-07-12: without them the 4B
    # "answers" size questions by reading digits out of the filename.
    rows = []
    for e in entries[:200]:
        full = os.path.join(path, e)
        if os.path.isdir(full):
            rows.append(f"[dir]  {e}")
        else:
            try:
                size = _fmt_size(os.path.getsize(full))
            except OSError:
                size = "?"
            rows.append(f"{size:>10}  {e}")
    extra = "" if len(entries) <= 200 else f"\n…and {len(entries) - 200} more"
    return "\n".join(rows) + extra


def _delete_file(path: str) -> str:
    """Deletes exactly what he asked — his order, no second-guessing. A
    folder goes recursively. The honest result string is the whole point:
    the 4B used to SAY 'deleted' without doing anything (measured
    2026-07-12); now it has a real tool and reports its real outcome."""
    path = (path or "").strip().strip('"')
    if not path:
        return "ERROR: no path"
    try:
        if os.path.isdir(path):
            import shutil
            shutil.rmtree(path)
            return f"deleted folder {path}"
        os.remove(path)
        return f"deleted {path}"
    except FileNotFoundError:
        return f"ERROR: no such file or folder: {path}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


# ---- shell ----
def _run_command(command: str) -> str:
    command = (command or "").strip()
    if not command:
        return "ERROR: empty command"
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out at 30 s"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
    out = out.strip() or f"(no output, exit {r.returncode})"
    return out[:OUT_CAP] + ("\n…[truncated]" if len(out) > OUT_CAP else "")
