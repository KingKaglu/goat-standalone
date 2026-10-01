"""Close guard: never close a window with a running session or unsaved work
without his yes.

2026-10-01: he said "could you please close this window I have opened?".
The brain picked the terminal he was running Claude Code in and ran
`(Get-Process -Id 17688).CloseMainWindow()` — his live session, gone. His
follow-up "what are you closing exactly?" arrived while it was doing it.
His order: confirm before closing any window that has a running session or
unsaved work.

One module, three doors that all ask it the same question:
- the reflex lane ("close this window") checks the window before it acts and
  asks instead of closing;
- the brain's shell tools (Bash/PowerShell) pass through a PreToolUse hook
  (goat_app) that reads the command for a close/kill aimed at a protected
  window and blocks it with "ask Giorgi first";
- screen_hands refuses alt+f4 / ctrl+f4 with a protected window in front.

His yes (YES_RE, the very next thing he says) either runs the reflex's
pending close or opens a short grant() window in which the brain's retry
goes through. Anything else drops the question.
"""
from __future__ import annotations

import ctypes
import fnmatch
import os
import re
import time

# Processes whose window IS a running session: a shell, a REPL, a Claude
# Code conversation. Closing one ends whatever runs inside it.
TERMINALS = {
    "windowsterminal.exe", "openconsole.exe", "conhost.exe", "cmd.exe",
    "powershell.exe", "pwsh.exe", "wt.exe", "mintty.exe", "alacritty.exe",
    "wezterm-gui.exe", "hyper.exe", "conemu.exe", "conemu64.exe",
    "tabby.exe", "warp.exe", "kitty.exe", "putty.exe", "mobaxterm.exe",
}
SESSIONS = {"claude.exe"}     # Claude desktop / Claude Code itself
# Apps that hold documents. WM_CLOSE on one is safe — the app asks to save —
# unless the title already says there are unsaved changes; a force-kill
# never asks, so killing one always needs his yes.
EDITORS = {
    "notepad.exe", "notepad++.exe", "code.exe", "cursor.exe", "devenv.exe",
    "sublime_text.exe", "winword.exe", "excel.exe", "powerpnt.exe",
    "idea64.exe", "pycharm64.exe", "rider64.exe", "webstorm64.exe",
    "obsidian.exe", "photoshop.exe", "blender.exe", "audacity.exe",
}
_LABEL = {"windowsterminal.exe": "Windows Terminal", "openconsole.exe": "a console",
          "conhost.exe": "a console", "cmd.exe": "Command Prompt",
          "powershell.exe": "PowerShell", "pwsh.exe": "PowerShell",
          "claude.exe": "Claude", "code.exe": "VS Code", "winword.exe": "Word",
          "excel.exe": "Excel", "powerpnt.exe": "PowerPoint",
          "notepad.exe": "Notepad"}
UNSAVED_RE = re.compile(r"^\s*[*●•]|[*●]\s*$|\s●\s|\bunsaved\b|\bnot saved\b",
                        re.IGNORECASE)

# ---- his answer ------------------------------------------------------------
YES_RE = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|yeah|yes|yep|sure|go\s+ahead|do\s+it|close\s+it|"
    r"yes\s+close\s+it|that's\s+fine|it's\s+fine|confirm(?:ed)?|კი|ჰო|ხო|"
    r"დიახ|კარგი|დახურე)\b[\s,.!]*)+(?:please|goat)?[\s.!]*$", re.IGNORECASE)
NO_RE = re.compile(
    r"^\s*(?:no|nope|don't|do\s+not|stop|cancel|wait|leave\s+it|არა|არ\s+"
    r"დახურო|მოიცა)\b", re.IGNORECASE)

PENDING_S = 120.0   # how long a question stays open
GRANT_S = 90.0      # how long his yes lets the brain's retry through

_pending: dict | None = None
_grant_until = 0.0


def ask(what: str, run=None) -> None:
    """Remember the question GOAT just asked. `run` is the close to perform
    on his yes (reflex lane); None means "let the brain retry"."""
    global _pending
    _pending = {"what": what, "run": run, "t": time.monotonic()}


def pending() -> dict | None:
    if _pending and time.monotonic() - _pending["t"] < PENDING_S:
        return _pending
    return None


def clear() -> None:
    global _pending
    _pending = None


def grant(seconds: float = GRANT_S) -> None:
    global _grant_until
    _grant_until = time.monotonic() + seconds
    clear()


def granted() -> bool:
    return time.monotonic() < _grant_until


# ---- what is protected -----------------------------------------------------
def _proc(pid: int):
    try:
        import psutil
        return psutil.Process(pid)
    except Exception:  # noqa: BLE001 — gone or unreadable: not protected
        return None


def _name(p) -> str:
    try:
        return p.name().lower()
    except Exception:  # noqa: BLE001
        return ""


def _label(name: str, title: str = "") -> str:
    lab = _LABEL.get(name) or os.path.splitext(name)[0] or "that window"
    t = (title or "").strip()
    return f"{lab} ({t[:60]})" if t and t.lower() != lab.lower() else lab


def _titles(pid: int) -> list:
    """Its window titles, biggest window first — Windows Terminal also owns
    tiny helper windows ("PopupHost") that must not name it."""
    try:
        import screen_hands
        ws = [w for w in screen_hands.windows()
              if w["pid"] == pid and w["title"].strip()
              and w["width"] >= 150 and w["height"] >= 100]
        ws.sort(key=lambda w: -(w["width"] * w["height"]))
        return [w["title"] for w in ws]
    except Exception:  # noqa: BLE001
        return []


def process_reason(pid: int, kill: bool = False, title: str = "") -> str | None:
    """Why closing/killing this process needs his yes — or None."""
    if not pid or pid == os.getpid():
        return None     # GOAT itself has its own, harder guard
    p = _proc(pid)
    if p is None:
        return None
    try:
        parents = p.parents()[:10]
    except Exception:  # noqa: BLE001
        parents = []
    name = _name(p)
    title = title or next(iter(_titles(pid)), "")
    # Documents first: a Notepad GOAT opened is still HIS once he typed in it.
    if name in EDITORS:
        if kill:
            return f"{_label(name, title)} — killing it loses anything unsaved"
        if any(UNSAVED_RE.search(t or "") for t in ([title] + _titles(pid))):
            return f"{_label(name, title)} — it has unsaved work"
        return None
    # GOAT's own tools (its engine's shells, servers it started) are GOAT's
    # to stop — even a pwsh.exe is not "his session" when GOAT spawned it.
    if any(a.pid == os.getpid() for a in parents):
        return None
    if name in TERMINALS:
        return f"{_label(name, title)} — a terminal with a running session"
    if name in SESSIONS:
        return f"{_label(name, title)} — a running Claude session"
    # A program running INSIDE one of his terminals is that session too.
    for a in parents:
        an = _name(a)
        if an in TERMINALS:
            return (f"{_label(name, title)} — it is running in your "
                    f"{_label(an)} session")
    return None


def window_reason(hwnd: int, title: str = "") -> str | None:
    pid = ctypes.c_ulong()
    try:
        ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    except Exception:  # noqa: BLE001
        return None
    return process_reason(pid.value, kill=False, title=title)


# ---- reading a shell command ----------------------------------------------
_KILL_RE = re.compile(r"\bStop-Process\b|\bspps\b|\btaskkill\b|\bkill(?:all)?\b|"
                      r"\bpkill\b|\.Kill\s*\(|TerminateProcess", re.IGNORECASE)
_CLOSE_RE = re.compile(r"CloseMainWindow|WM_CLOSE|\b0x0*10\b|PostMessage|"
                       r"SendMessage|alt\s*\+\s*f4|\.Close\s*\(\s*\)", re.IGNORECASE)
_PID_RE = re.compile(r"(?:-Id|/PID|-ProcessId|GetProcessById\s*\(|\bkill(?:\s+-9)?|"
                     r"\bpid\s*[=:])\s*['\"]?(\d{2,7})", re.IGNORECASE)
_NAME_RE = re.compile(r"(?:Get-Process|-Name|-ProcessName|/IM|\bpkill|\bkillall)"
                      r"\s+(?:-Name\s+)?['\"]?([\w.*\- ]+?)['\"]?(?=\s|$|\||\)|;|,)",
                      re.IGNORECASE)
_NAME_CMP_RE = re.compile(r"(?:ProcessName|\.Name|\bName)\s*-(?:eq|like|match)\s*"
                          r"['\"]([^'\"]+)['\"]", re.IGNORECASE)
_TITLE_RE = re.compile(r"(?:MainWindowTitle|WindowTitle|title)\s*-(?:eq|like|match)\s*"
                       r"['\"]([^'\"]+)['\"]", re.IGNORECASE)
_HWND_RE = re.compile(r"(?:PostMessage|SendMessage)\w*\s*\(\s*(?:\[IntPtr\]\s*)?"
                      r"(0x[0-9a-f]+|\d+)", re.IGNORECASE)
# Names distinctive enough to count even outside a Get-Process clause.
_LOUD_NAMES = re.compile(r"\b(WindowsTerminal|OpenConsole|wt\.exe|mintty|"
                         r"alacritty|wezterm)\b", re.IGNORECASE)


def _pids_named(pattern: str) -> list:
    pat = pattern.strip().lower()
    if not pat:
        return []
    if not pat.endswith(".exe") and "*" not in pat:
        pat_exe = pat + ".exe"
    else:
        pat_exe = pat
    out = []
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "").lower()
            if fnmatch.fnmatch(n, pat_exe) or fnmatch.fnmatch(n, pat):
                out.append(p.pid)
    except Exception:  # noqa: BLE001
        pass
    return out


def _pids_titled(pattern: str) -> list:
    pat = pattern.lower()
    glob = "*" in pat or "?" in pat
    out = []
    try:
        import screen_hands
        for w in screen_hands.windows():
            t = (w["title"] or "").lower()
            if (fnmatch.fnmatch(t, pat) if glob else pat in t):
                out.append((w["pid"], w["title"]))
    except Exception:  # noqa: BLE001
        pass
    return out


def check_command(cmd: str) -> str | None:
    """A shell command that would close or kill a protected window/process
    -> why; anything else -> None. Cheap when no close/kill verb appears."""
    if not cmd:
        return None
    kill = bool(_KILL_RE.search(cmd))
    if not kill and not _CLOSE_RE.search(cmd):
        return None
    cands: list = []                                 # (pid, title)
    cands += [(int(m), "") for m in _PID_RE.findall(cmd)]
    for raw in _NAME_RE.findall(cmd) + _NAME_CMP_RE.findall(cmd):
        for part in re.split(r"[,\s]+", raw):
            cands += [(pid, "") for pid in _pids_named(part.strip("'\""))]
    for raw in _TITLE_RE.findall(cmd):
        cands += _pids_titled(raw)
    for h in _HWND_RE.findall(cmd):
        try:
            hwnd = int(h, 16) if h.lower().startswith("0x") else int(h)
        except ValueError:
            continue
        if hwnd > 0x10:
            r = window_reason(hwnd)
            if r:
                return r
    for m in _LOUD_NAMES.findall(cmd):
        cands += [(pid, "") for pid in _pids_named(m.replace(".exe", ""))]
    seen = set()
    for pid, title in cands:
        if pid in seen:
            continue
        seen.add(pid)
        r = process_reason(pid, kill=kill, title=title)
        if r:
            return r
    return None


def block_message(reason: str) -> str:
    """What the brain is told when its close is stopped."""
    return ("BLOCKED by GOAT's close guard: that would close " + reason + ". "
            "Giorgi's order: never close a window with a running session or "
            "unsaved work without asking. Ask him out loud, naming exactly "
            "which window, and wait for his yes — then run the same close "
            "again. Do not try another way to close it.")
