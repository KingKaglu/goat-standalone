"""The voice's own hands (2026-10-25, his order: "I want the listening engine
to have the same abilities as Claude has over my phone or searching any
files ... and make sure to finish up the job").

His afternoon on the phone, from the transcript: "check my mobile, why my
eSIM data is not working" got "I am unable to check your mobile settings";
"search my laptop's files for the barbershop business idea" opened a guessed
path; "what's the terminal Claude doing" got "idle" while it was working —
every one of them either a hand-off to Claude (out of usage that afternoon)
or a refusal. These are the things the voice can now do itself, at once,
with no Claude usage:

  find_files     names AND contents ("the barbershop idea" is
                 workspace/side-hustle/business-plan.md — no "barber" in it)
  read_file      text, markdown, code, html, docx
  list_folder
  open_path      a file or folder in its own app (never a program/script)
  phone          the same phone hands Claude uses (phone_hands.py ops)
  terminal_claude  what the Claude Code sessions in his terminal are doing,
                 and continue one that has stopped
  web_search     current facts (Live's own Google Search is refused on his
                 free tier)

execute(name, args) -> short text result for the model. Blocking: run it in
a thread. Everything returns a string, never raises.
"""
from __future__ import annotations

import html
import json
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile

from goat_paths import GOAT_ROOT

HOME = os.path.expanduser("~")
# Where his things live. Program/system trees and dependency folders are
# skipped: nobody asks the voice for a file inside node_modules.
ROOTS = [HOME] + [d for d in ("D:\\", "E:\\") if os.path.isdir(d)]
SKIP_DIRS = {
    "appdata", "node_modules", ".git", ".venv", "venv", "env", "__pycache__",
    "site-packages", "dist", "build", ".next", ".cache", ".gradle", ".npm",
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "msocache", ".self-backup",
    "riot games", "steamapps", "windowsapps", "recovery", "perflogs",
    ".android", ".nuget", ".dotnet", ".expo", ".u2net", ".ollama",
    "android-tools", "clipforge-chrome-profile", "fasmetri-chrome-profile",
    ".qtwebengineprocess", "stt-debug", "tool-results",
}
INDEX_CAP = 400_000
INDEX_TTL_S = 900.0
TEXT_EXT = {".md", ".txt", ".html", ".htm", ".py", ".js", ".ts", ".tsx", ".json",
            ".csv", ".log", ".yml", ".yaml", ".toml", ".ini", ".xml", ".css",
            ".ps1", ".bat", ".sql", ".docx"}
# Content search looks where his own writing is, newest first.
CONTENT_ROOTS = [os.path.join(GOAT_ROOT, "workspace"),
                 os.path.join(HOME, "Desktop"), os.path.join(HOME, "Documents"),
                 os.path.join(HOME, "Downloads"), os.path.join(HOME, "OneDrive")]
READ_CAP = 6000            # what the voice can usefully read out from
NEVER_OPEN = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".js", ".reg",
              ".scr", ".com", ".jar", ".hta", ".wsf", ".psm1"}
STOP = {"the", "a", "an", "my", "me", "file", "files", "folder", "about", "and",
        "of", "on", "in", "for", "to", "laptop", "computer", "pc", "find", "search",
        "open", "with", "that", "this", "is", "it", "goat", "please", "idea"}

# His documents' text, lower-cased, read once in the background: reading
# 1,118 files (23 MB) from disk took 17.7 s cold (2026-10-25), so a search
# that read them per question ran out of time before reaching the file.
DOC_EXT = {".md", ".txt", ".html", ".htm", ".docx", ".csv", ".rtf"}
DOC_CAP = 200_000          # chars kept per document
_docs: dict = {}           # path -> (mtime, lower text)
_index: list = []          # [(lower path, path, mtime)]
_index_t = 0.0
_index_lock = threading.Lock()
_building = threading.Event()


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def _build_index():
    rows = []
    for root in ROOTS:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in SKIP_DIRS
                           and not (d.startswith(".") and d.lower() != ".claude")]
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                rows.append(full)
            if len(rows) >= INDEX_CAP:
                break
        if len(rows) >= INDEX_CAP:
            break
    out = []
    for p in rows:
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = 0.0
        out.append((p.lower(), p, mt))
    return out


def warm():
    """Build the file index in the background (GOAT boot)."""
    def run():
        global _index, _index_t
        if _building.is_set():
            return
        _building.set()
        try:
            idx = _build_index()
            with _index_lock:
                _index, _index_t = idx, time.time()
            print(f"[voice-hands] file index: {len(idx)} entries", flush=True)
            _fill_docs(idx)
        except Exception as e:  # noqa: BLE001
            print(f"[voice-hands] index failed: {e!r}", flush=True)
        finally:
            _building.clear()
    threading.Thread(target=run, daemon=True, name="voice-file-index").start()


def _words(q: str) -> list:
    ws = [w for w in re.findall(r"[\w\-]+", (q or "").lower())
          if len(w) >= 2 and w not in STOP]
    # "barbershop" also finds "barber", "business-plan" also "business plan"
    return ws


def _fmt(p: str, mt: float) -> str:
    when = time.strftime("%Y-%m-%d", time.localtime(mt)) if mt else "?"
    kind = "folder" if os.path.isdir(p) else "file"
    return f"{p}  ({kind}, changed {when})"


def _name_hits(words: list, limit: int = 8) -> list:
    with _index_lock:
        idx = _index
    if not idx:
        return []
    scored = []
    for low, p, mt in idx:
        base = os.path.basename(low)
        in_path = sum(1 for w in words if w in low)
        if in_path < max(1, len(words) - (1 if len(words) > 2 else 0)):
            continue
        score = in_path + 2 * sum(1 for w in words if w in base)
        scored.append((score, mt, p))
    scored.sort(key=lambda r: (-r[0], -r[1]))
    return [(p, mt) for _, mt, p in scored[:limit]]


def _read_text(path: str, cap: int) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return html.unescape(re.sub(r"<[^>]+>", "", xml))[:cap]
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = f.read(cap)
    if ext in (".html", ".htm"):
        data = re.sub(r"(?is)<(script|style).*?</\1>", " ", data)
        data = html.unescape(re.sub(r"<[^>]+>", " ", data))
        data = re.sub(r"[ \t]+", " ", data)
    return data


def _doc_paths(idx: list) -> list:
    roots = tuple(os.path.normcase(r).lower() + os.sep for r in CONTENT_ROOTS)
    rows = [(mt, p) for low, p, mt in idx
            if low.startswith(roots) and os.path.splitext(low)[1] in DOC_EXT
            and "\\reports\\" not in low and not low.endswith("file-index.json")]
    rows.sort(reverse=True)
    return rows


def _fill_docs(idx: list):
    t0 = time.monotonic()
    n = 0
    for mt, p in _doc_paths(idx):
        old = _docs.get(p)
        if old is not None and old[0] == mt:
            continue
        try:
            if os.path.getsize(p) > 2_000_000:
                continue
            _docs[p] = (mt, _read_text(p, DOC_CAP).lower())
            n += 1
        except Exception:  # noqa: BLE001
            continue
    print(f"[voice-hands] read {n} documents into memory "
          f"({len(_docs)} kept, {time.monotonic() - t0:.1f}s)", flush=True)


def _content_hits(words: list, limit: int = 6) -> list:
    """Documents whose TEXT has the words (from the in-memory copy)."""
    stems = [w[:max(4, len(w) - 3)] if len(w) > 6 else w for w in words]
    need = max(1, len(stems) - (1 if len(stems) > 2 else 0))
    hits = []
    for p, (mt, text) in list(_docs.items()):
        n = sum(1 for s_ in stems if s_ in text)
        if n >= need:
            hits.append((n, text.count(stems[0]) if stems else 0, mt, p))
    hits.sort(key=lambda r: (-r[0], -r[1], -r[2]))
    return [(p, mt) for _, _, mt, p in hits[:limit]]


def find_files(query: str) -> str:
    words = _words(query)
    if not words:
        return "ERROR: say what to look for (a name or a few words that are in it)"
    if not _index and not _building.is_set():
        warm()
    if not _index:
        for _ in range(60):                 # first search right after boot
            if _index:
                break
            time.sleep(0.25)
    elif time.time() - _index_t > INDEX_TTL_S:
        warm()                              # refresh in the background
    names = _name_hits(words)
    lines = []
    if names:
        lines.append("By name:")
        lines += ["  " + _fmt(p, mt) for p, mt in names]
    inside = []
    if len(names) < 3:
        # few or no names match: look inside his documents too
        inside = [h for h in _content_hits(words) if h[0] not in {p for p, _ in names}]
    if inside:
        lines.append("Mentioned inside:")
        lines += ["  " + _fmt(p, mt) for p, mt in inside]
    if not lines:
        return (f"nothing found for {query!r} (searched names everywhere on his "
                "drives, and the contents of his documents)")
    return "\n".join(lines)


def read_file(path: str) -> str:
    path = (path or "").strip().strip('"')
    if not os.path.isfile(path):
        return f"ERROR: no such file: {path}"
    ext = os.path.splitext(path)[1].lower()
    if ext not in TEXT_EXT:
        return (f"ERROR: {ext or 'that'} isn't a text file I can read out; "
                "open_path shows it to him instead")
    try:
        text = _read_text(path, READ_CAP + 1)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    if len(text) > READ_CAP:
        text = text[:READ_CAP] + "\n…[more in the file]"
    return text.strip() or "[empty file]"


def list_folder(path: str) -> str:
    path = (path or "").strip().strip('"') or os.path.join(HOME, "Desktop")
    try:
        names = sorted(os.listdir(path), key=str.lower)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    rows = [("[folder] " if os.path.isdir(os.path.join(path, n)) else "") + n
            for n in names[:120]]
    more = f"\n…and {len(names) - 120} more" if len(names) > 120 else ""
    return (f"{path}:\n" + "\n".join(rows) + more) if rows else f"{path} is empty"


def open_path(path: str) -> str:
    path = (path or "").strip().strip('"')
    if not os.path.exists(path):
        return f"ERROR: no such file or folder: {path}"
    if os.path.splitext(path)[1].lower() in NEVER_OPEN:
        return "ERROR: that's a program or script — running it is Claude's job"
    try:
        os.startfile(path)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return f"opened {path}"


# ---------------------------------------------------------------------------
# phone (the same hands Claude uses: phone_server's /phone/do)
# ---------------------------------------------------------------------------

SETS = {"wifi": "WIFI", "bluetooth": "BLUETOOTH", "display": "DISPLAY",
        "sound": "SOUND", "battery": "BATTERY_SAVER", "apps": "APPLICATION",
        "location": "LOCATION_SOURCE", "airplane": "AIRPLANE_MODE",
        "data": "DATA_ROAMING", "mobile": "DATA_ROAMING", "sim": "DATA_ROAMING",
        "network": "WIRELESS"}


def _intent(action, data=None, **kw):
    d = {"op": "intent", "action": action}
    if data:
        d["data"] = data
    d.update(kw)
    return d


def _phone_call(cmd: dict, timeout: float = 25.0) -> dict:
    tok = open(os.path.join(GOAT_ROOT, ".phone", "token.txt"), encoding="utf-8").read().strip()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        "https://127.0.0.1:8797/phone/do", json.dumps(cmd).encode(),
        {"Cookie": "goat_k=" + tok, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, context=ctx, timeout=timeout))


SEND_LABELS = {"send", "send message", "send a message", "გაგზავნა", "senden", "отправить"}


def _press_send(text: str) -> str:
    """After typing a message: press the app's own Send button and check.

    2026-10-25: "text my girlfriend that I love her" failed three times in
    Messenger. type(submit) presses the keyboard's Enter, which Messenger
    ignores — the text sat unsent in the box while the voice went home and
    said it was sent. So: find the Send button on screen, tap it, and look
    again: sent = the text is no longer in an input field."""
    try:
        scr = _phone_call({"op": "screen"})
        items = scr.get("items") or []
        btn = next((it for it in items
                    if (it.get("t") or "").strip().lower() in SEND_LABELS), None)
        if btn is not None and "x" in btn:
            _phone_call({"op": "tap", "x": btn["x"], "y": btn["y"]})
        else:
            r = _phone_call({"op": "tap_text", "text": "Send"})
            if not r.get("ok", True):
                return ("typed, but NOT sent: no Send button on screen — read the "
                        "screen and tap the app's send button yourself")
        time.sleep(0.6)
        after = _phone_call({"op": "screen"}).get("items") or []
    except Exception as e:  # noqa: BLE001
        return f"typed; pressing Send failed: {e}"
    still = any(it.get("edit") and text[:20] and text[:20] in (it.get("t") or "")
                for it in after)
    if still:
        return "typed, but NOT sent: the text is still in the box — tap the send button"
    return "sent (pressed the app's Send button; the box is empty now)"


def phone(args: dict) -> str:
    op = str(args.get("op") or "").strip().lower()
    a = str(args.get("arg") or "").strip()
    b = str(args.get("arg2") or "").strip()
    q = urllib.parse.quote
    try:
        if op in ("screen", "back", "home", "recents", "notifications", "apps"):
            cmd = {"op": op}
        elif op == "open":
            cmd = {"op": "open", "app": a}
        elif op == "tap_text":
            cmd = {"op": "tap_text", "text": a}
        elif op == "type":
            cmd = {"op": "type", "text": a, "submit": b.lower() in ("submit", "send", "yes", "true")}
        elif op == "scroll":
            cmd = {"op": "scroll", "up": a.lower() == "up"}
        elif op == "volume":
            cmd = {"op": "volume", "action": a or "up", "percent": int(b or 50)}
        elif op == "media":
            cmd = {"op": "media", "action": a or "play_pause"}
        elif op == "torch":
            cmd = {"op": "torch", "on": a.lower() != "off"}
        elif op == "alarm":
            hh, _, mm = a.replace(".", ":").partition(":")
            cmd = _intent("android.intent.action.SET_ALARM", extras={
                "android.intent.extra.alarm.HOUR": int(hh),
                "android.intent.extra.alarm.MINUTES": int(mm or 0),
                "android.intent.extra.alarm.MESSAGE": b or "GOAT",
                "android.intent.extra.alarm.SKIP_UI": True})
        elif op == "timer":
            cmd = _intent("android.intent.action.SET_TIMER", extras={
                "android.intent.extra.alarm.LENGTH": int(float(a)),
                "android.intent.extra.alarm.MESSAGE": b or "GOAT",
                "android.intent.extra.alarm.SKIP_UI": True})
        elif op == "dial":
            cmd = _intent("android.intent.action.DIAL", "tel:" + a)
        elif op == "sms":
            cmd = _intent("android.intent.action.SENDTO", "smsto:" + a,
                          extras={"sms_body": b})
        elif op == "url":
            cmd = _intent("android.intent.action.VIEW", a)
        elif op == "maps":
            # A ready link (google.navigation:, geo:, a maps URL) goes as it
            # is — the voice sent those and they were wrapped a second time.
            # Words are a Maps search, which Maps runs around HIS phone's GPS:
            # "car wash open now" lists the open ones near him (his 2026-10-25
            # "you are not searching anything well on the map").
            if re.match(r"^(google\.navigation:|geo:|https?://)", a, re.I):
                cmd = _intent("android.intent.action.VIEW", a)
            else:
                cmd = _intent("android.intent.action.VIEW", "geo:0,0?q=" + q(a))
        elif op == "search":
            cmd = _intent("android.intent.action.VIEW",
                          "https://www.google.com/search?q=" + q(a))
        elif op == "settings":
            key = SETS.get(a.lower())
            cmd = _intent("android.settings." + (key + "_SETTINGS" if key else "SETTINGS"))
        else:
            return (f"ERROR: unknown phone op {op!r} (screen, open, tap_text, type, "
                    "scroll, back, home, notifications, volume, media, torch, alarm, "
                    "timer, dial, sms, url, maps, search, settings)")
        res = _phone_call(cmd)
    except urllib.error.URLError as e:
        return f"ERROR: GOAT's phone link is down ({e.reason})"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"
    if not res.get("ok", True) and res.get("error"):
        return f"ERROR: {res['error']}"
    if op == "type" and cmd.get("submit") and res.get("ok", True):
        return _press_send(a)
    if op == "screen" and res.get("items") is not None:
        lines = [f"phone screen, app {res.get('app')}:"]
        for it in res["items"][:80]:
            t = (it.get("t") or "").strip()
            if t:
                lines.append(("[field] " if it.get("edit") else "")
                             + ("[button] " if it.get("c") else "") + t)
        return "\n".join(lines)
    return "done: " + json.dumps(res, ensure_ascii=False)[:600]


# ---------------------------------------------------------------------------
# the Claude Code sessions in his terminal
# ---------------------------------------------------------------------------

JOBS_DIR = os.path.join(HOME, ".claude", "jobs")
WORKSPACE = os.path.normcase(os.path.join(GOAT_ROOT, "workspace"))


def _ago(t: float) -> str:
    s = max(0, int(time.time() - t))
    return f"{s} s ago" if s < 90 else f"{s // 60} min ago" if s < 5400 else f"{s // 3600} h ago"


def _sessions() -> list:
    """Claude Code sessions he runs himself (GOAT's own worker excluded)."""
    try:
        out = subprocess.run(["claude", "agents", "--json"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=40, shell=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        rows = json.loads(out[out.index("["):]) if "[" in out else []
    except Exception:  # noqa: BLE001
        rows = []
    return [r for r in rows
            if os.path.normcase(str(r.get("cwd") or "")) != WORKSPACE]


def _job_state(short: str) -> dict:
    try:
        with open(os.path.join(JOBS_DIR, short, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _recent_steps(short: str, n: int = 5) -> list:
    """The session's last few distinct step lines (newest last)."""
    try:
        with open(os.path.join(JOBS_DIR, short, "timeline.jsonl"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            tail = f.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return []
    steps = []
    for ln in tail:
        try:
            d = (json.loads(ln).get("detail") or "").strip()
        except ValueError:
            continue
        if d and (not steps or steps[-1] != d):
            steps.append(d)
    return steps[-n:]


def terminal_claude(args: dict) -> str:
    action = str(args.get("action") or "status").lower()
    rows = _sessions()
    if not rows:
        return "no Claude Code session is open in his terminal right now"
    if action == "status":
        out = []
        for r in rows:
            st = _job_state(str(r.get("id") or "")) if r.get("id") else {}
            line = (f"- {r.get('name') or r.get('sessionId', '')[:8]} "
                    f"({r.get('kind')}, in {r.get('cwd')}): "
                    f"{'WORKING' if r.get('status') == 'busy' else r.get('status')}")
            if st.get("detail"):
                line += f" — right now: {st['detail']}"
            if st.get("updatedAt"):
                try:
                    import calendar
                    t = calendar.timegm(time.strptime(st["updatedAt"][:19], "%Y-%m-%dT%H:%M:%S"))
                    line += f" (updated {_ago(t)})"
                except Exception:  # noqa: BLE001
                    pass
            # Its last few steps say what it's on. (The job's first request
            # is NOT that: a session runs for days — on 2026-10-25 the voice
            # read yesterday's "small tasks like open, close, email" as now.)
            steps = _recent_steps(str(r.get("id") or ""))
            if steps:
                line += "; its last steps: " + " / ".join(steps)
            out.append(line)
        return "Claude Code sessions in his terminal:\n" + "\n".join(out)
    if action == "continue":
        msg = str(args.get("message") or "Continue the task you were working on.").strip()
        idle = [r for r in rows if r.get("status") != "busy" and r.get("sessionId")]
        if not idle:
            return "the terminal Claude is still working — nothing to continue; it's on it"
        r = idle[0]
        try:
            subprocess.Popen(["claude", "--bg", "--resume", r["sessionId"], msg],
                             cwd=r.get("cwd") or HOME, shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:  # noqa: BLE001
            return f"ERROR: couldn't resume it: {e}"
        return f"resumed {r.get('name') or r['sessionId'][:8]} in the background with: {msg[:120]}"
    return "ERROR: action is status or continue"


# ---------------------------------------------------------------------------
# where he is (2026-10-25: "Do you see my location?" got "I do not")
# ---------------------------------------------------------------------------
# GOAT's phone app has no location access and Windows location is switched
# off on the laptop, so this is the laptop's internet connection, looked up
# (ip-api.com, free, no key): the city, not the street. Phone and laptop
# share his Wi-Fi in Essen, so it is where he is when he's home.

def where_am_i() -> str:
    try:
        with urllib.request.urlopen(
                "http://ip-api.com/json/?fields=status,city,regionName,country,zip,timezone",
                timeout=8) as r:
            d = json.load(r)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: location lookup failed ({e})"
    if d.get("status") != "success":
        return "ERROR: location lookup found nothing"
    return (f"approximately {d.get('city')}, {d.get('regionName')}, {d.get('country')} "
            f"(postcode area {d.get('zip')}, time zone {d.get('timezone')}) — from the "
            "laptop's internet connection: the city, not the exact street")


# ---------------------------------------------------------------------------

def execute(name: str, args: dict) -> str:
    args = args or {}
    try:
        if name == "find_files":
            return find_files(str(args.get("query") or ""))
        if name == "read_file":
            return read_file(str(args.get("path") or ""))
        if name == "list_folder":
            return list_folder(str(args.get("path") or ""))
        if name == "open_path":
            return open_path(str(args.get("path") or ""))
        if name == "phone":
            return phone(args)
        if name == "terminal_claude":
            return terminal_claude(args)
        if name == "where_am_i":
            return where_am_i()
        if name == "web_search":
            import local_hands
            q = str(args.get("query") or "")
            hits = local_hands._web_search(q)
            if args.get("read_first") and not hits.startswith(("ERROR", "no results")):
                m = re.search(r"\n  (https?://\S+)", hits)
                if m:
                    hits += ("\n\nTop result's page:\n"
                             + local_hands._fetch_url(m.group(1))[:3500])
            return hits
    except Exception as e:  # noqa: BLE001 — a hand never kills the voice
        return f"ERROR: {type(e).__name__}: {e}"
    return f"ERROR: unknown tool {name}"


NAMES = {"find_files", "read_file", "list_folder", "open_path", "phone",
         "terminal_claude", "web_search", "where_am_i"}

DECLS = [
    {"name": "find_files",
     "description": "Search his laptop for files or folders: by name everywhere on his "
                    "drives AND by what is written inside his documents. Use it "
                    "yourself for any 'find / where is / search my files for' — "
                    "then read_file or open_path the hit.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "A few key words: a name, or "
                                                    "words the file contains."}},
         "required": ["query"]}},
    {"name": "read_file",
     "description": "Read a text file (md, txt, html, code, json, docx) so you can tell "
                    "him what's in it.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "list_folder",
     "description": "List what's in a folder on his laptop.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "open_path",
     "description": "Open a file or folder on his laptop's screen in its own app "
                    "(documents, pages, pictures, folders — never programs).",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}},
    {"name": "phone",
     "description": "His Android phone, directly (the same hands Claude uses). ops: "
                    "screen (read what's on it), open (arg=app name), tap_text "
                    "(arg=visible text), type (arg=text, arg2='submit' also presses the app's Send "
                    "button and says whether it really went), scroll "
                    "(arg='up'|'down'), back, home, notifications, volume "
                    "(arg=up|down|mute|set, arg2=percent), media (arg=play_pause|next|"
                    "previous), torch (arg=on|off), alarm (arg='HH:MM', arg2=label), "
                    "timer (arg=seconds), dial (arg=number), sms (arg=number, "
                    "arg2=text — opens the message, he presses send), url, maps, "
                    "search, settings (arg=wifi|bluetooth|mobile|sim|network|battery|"
                    "display|sound|location|apps). To check or change a phone "
                    "setting: settings, then screen, then tap_text — step by step. "
                    "To text someone: open the app, tap_text their name, tap_text "
                    "the message box, type with arg2='submit'. Say it's sent only "
                    "when type answers 'sent'. maps arg = what to search ('car wash "
                    "open now') to show him the places on the map himself.",
     "parameters": {"type": "object", "properties": {
         "op": {"type": "string"}, "arg": {"type": "string"},
         "arg2": {"type": "string"}}, "required": ["op"]}},
    {"name": "web_search",
     "description": "Search the web yourself for current facts (opening hours, "
                    "news, prices, weather, what a word means). read_first=true also "
                    "reads the top page.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string"},
         "read_first": {"type": "boolean"}}, "required": ["query"]}},
    {"name": "where_am_i",
     "description": "Roughly where he is right now (city, region, country, "
                    "postcode area) — for 'where am I', 'near me', local hours.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "terminal_claude",
     "description": "The Claude Code sessions in his terminal (not GOAT's own Claude): "
                    "action 'status' says which are WORKING and on what, 'continue' "
                    "resumes one that has stopped (message = what to do).",
     "parameters": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["status", "continue"]},
         "message": {"type": "string"}}, "required": ["action"]}},
]
