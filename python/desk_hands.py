"""The voice's desk hands (2026-10-31, his order: "Make sure the talking voice
never tells me I can't do this, I can't do that. Everything should be done by
you as well if I ask you to do so ... you should try to do it").

What the transcript showed while Claude was out of usage (2026-10-27..30):
"create a notepad and save the previous two days" -> "that requires Claude's
active brain"; "fill this notepad with our latest chat logs" -> the same;
"today I worked about 10 hours, fill out the Excel" -> "my tools for editing
spreadsheets require Claude"; "how many hours this week" -> 152 h (the month
total, read off the screen). Each is a few lines of work, done here with no
Claude usage:

  write_file     make or add to a text file (Desktop by default) and open it
  save_chat_log  GOAT's own chat with him, by day, into a text file
  work_hours     his Work-Hours-Pay.xlsx: read the week/month, or log hours
                 for a day (through Excel itself, so an open workbook updates
                 live and nothing fights over the file lock)

Every function returns a short string for the model and never raises.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess

HOME = os.path.expanduser("~")
_HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(os.path.dirname(_HERE), "workspace")
TRANSCRIPT = os.path.join(WORKSPACE, "transcript.jsonl")


def _desktop() -> str:
    for d in (os.path.join(HOME, "OneDrive", "Desktop"), os.path.join(HOME, "Desktop")):
        if os.path.isdir(d):
            return d
    return HOME


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------

# Text he'd want to open in Notepad or an editor. Anything else (a program,
# a script that could run, an Office file) is Claude's job.
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".log", ".xml",
            ".yaml", ".yml", ".ini"}
_NO_WRITE = ("\\windows\\", "\\program files", "\\programdata\\", "\\appdata\\",
             "\\goat-standalone\\python\\")
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _target(path: str) -> str:
    """A bare name lands on his Desktop; a name without an extension is .txt."""
    path = os.path.expandvars(os.path.expanduser((path or "").strip().strip('"')))
    if not path:
        path = f"GOAT note {datetime.datetime.now():%Y-%m-%d %H%M}.txt"
    if not os.path.isabs(path):
        head, tail = os.path.split(path)
        low = head.lower().replace("/", "\\")
        base = {"": _desktop(), "desktop": _desktop(),
                "documents": os.path.join(HOME, "Documents"),
                "downloads": os.path.join(HOME, "Downloads")}.get(low)
        path = os.path.join(base, tail) if base else os.path.join(_desktop(), path)
    if not os.path.splitext(path)[1]:
        path += ".txt"
    return os.path.normpath(path)


def _free_name(path: str) -> str:
    """'note.txt' taken -> 'note (2).txt': never overwrite his file."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(2, 100):
        p = f"{stem} ({i}){ext}"
        if not os.path.exists(p):
            return p
    return f"{stem} ({datetime.datetime.now():%H%M%S}){ext}"


def _open(path: str):
    try:
        os.startfile(path)
    except OSError:
        subprocess.Popen(["notepad.exe", path])


def write_file(args: dict) -> str:
    content = str(args.get("content") or "")
    mode = str(args.get("mode") or "new").lower()
    path = _target(str(args.get("path") or ""))
    if _BAD_NAME.search(os.path.basename(path)):
        return f"ERROR: bad file name {os.path.basename(path)!r}"
    if os.path.splitext(path)[1].lower() not in TEXT_EXT:
        return ("ERROR: I only write text files (txt, md, csv, json, html); "
                "anything else goes to Claude")
    if any(b in path.lower() for b in _NO_WRITE):
        return "ERROR: that folder is off limits for the voice — Claude's job"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if mode == "append" and os.path.exists(path):
            with open(path, "a", encoding="utf-8") as f:
                if content and not content.startswith("\n"):
                    f.write("\n")
                f.write(content)
            what = f"added {len(content)} characters to {path}"
        else:
            path = _free_name(path)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            what = f"wrote {len(content)} characters to {path}"
        if args.get("open", True):
            _open(path)
            what += " and opened it on his screen"
        return what
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# save_chat_log
# ---------------------------------------------------------------------------

def _day(s: str, today: datetime.date) -> datetime.date | None:
    s = (s or "").strip().lower()
    if not s:
        return None
    if s == "today":
        return today
    if s == "yesterday":
        return today - datetime.timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m", "%d/%m", "%b %d", "%d %b",
                "%B %d", "%d %B"):
        try:
            d = datetime.datetime.strptime(s, fmt).date()
            if "%Y" not in fmt:
                d = d.replace(year=today.year)
                if d > today:
                    d = d.replace(year=today.year - 1)
            return d
        except ValueError:
            pass
    return None


def chat_lines(first: datetime.date, last: datetime.date, path: str = "") -> list:
    """GOAT's chat with him between two days, as readable lines. The
    limit-retry spam ("[event: Claude's job failed]" every 20 minutes for
    two days) is folded into one line per run."""
    out, prev_ev, run = [], "", 0
    try:
        with open(path or TRANSCRIPT, encoding="utf-8", errors="replace") as f:
            rows = f.read().splitlines()
    except OSError:
        return []
    day = None
    for raw in rows:
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        t = datetime.datetime.fromtimestamp(float(d.get("t") or 0))
        if not first <= t.date() <= last:
            continue
        user = str(d.get("user") or "").strip()
        reply = str(d.get("reply") or "").strip()
        if reply == "[[quiet]]":
            reply = "(stayed quiet)"
        if user.startswith("[event"):
            if user == prev_ev:
                run += 1
                continue
            if run:
                out.append(f"        (... the same {run} more time(s))")
            prev_ev, run = user, 0
        elif run:
            out.append(f"        (... the same {run} more time(s))")
            prev_ev, run = "", 0
        else:
            prev_ev = ""
        if t.date() != day:
            day = t.date()
            out.append("")
            out.append(f"===== {day:%A %d %B %Y} =====")
        who = "Event" if user.startswith("[event") else "Giorgi"
        out.append(f"{t:%H:%M}  {who}: {user}")
        if reply:
            out.append(f"       GOAT: {reply}")
    if run:
        out.append(f"        (... the same {run} more time(s))")
    return out


def save_chat_log(args: dict) -> str:
    today = datetime.date.today()
    try:
        days = max(1, min(30, int(args.get("days") or 2)))
    except (TypeError, ValueError):
        days = 2
    last = _day(str(args.get("to") or ""), today) or today
    first = _day(str(args.get("from") or ""), today) or (
        last - datetime.timedelta(days=days - 1))
    if first > last:
        first, last = last, first
    lines = chat_lines(first, last)
    if not lines:
        return f"ERROR: no chat with him between {first} and {last} in the log"
    head = (f"GOAT chat log — {first:%d %b} to {last:%d %b %Y}\n"
            f"(saved {datetime.datetime.now():%d %b %Y %H:%M})\n")
    name = str(args.get("path") or "") or (
        f"GOAT chat {first:%Y-%m-%d}" + (f" to {last:%Y-%m-%d}" if last != first else ""))
    res = write_file({"path": name, "content": head + "\n".join(lines) + "\n",
                      "open": args.get("open", True)})
    n = sum(1 for x in lines if x[:2].isdigit())
    return res if res.startswith("ERROR") else f"{n} exchanges: {res}"


# ---------------------------------------------------------------------------
# work_hours  (workspace/work-hours/build_workbook.py made the workbook:
# one sheet per month named "Oct 2026", row 5+day, column D = hours as a
# fraction of a day, column C = week number, I12/I13/I18/I20/I21 totals)
# ---------------------------------------------------------------------------

WORKBOOK = os.environ.get("GOAT_WORK_HOURS") or os.path.join(_desktop(), "Work-Hours-Pay.xlsx")


def _hours(s) -> float | None:
    """'10', '8:30', '8.5', '8h 15m', '7 and a half' -> hours."""
    s = str(s or "").strip().lower().replace(",", ".")
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60
    m = re.fullmatch(r"(\d{1,2}(?:\.\d+)?)\s*(?:h|hours?)?\s*(?:(\d{1,2})\s*(?:m|min|minutes?))?", s)
    if m:
        return float(m.group(1)) + (int(m.group(2)) / 60 if m.group(2) else 0)
    m = re.fullmatch(r"(\d{1,2})\s*(?:hours?)?\s*and\s*a\s*half", s)
    if m:
        return int(m.group(1)) + 0.5
    return None


def _hm(h: float) -> str:
    mins = int(round(h * 60))
    return f"{mins // 60}:{mins % 60:02d}"


class _Book:
    """The workbook through Excel. Open in his Excel: that copy is used (he
    sees it change). Closed: opened hidden, saved, closed again."""

    def __enter__(self):
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        self.mine = False
        self.app = None
        self.wb = None
        try:
            self.app = win32com.client.GetActiveObject("Excel.Application")
            for wb in self.app.Workbooks:
                if os.path.normcase(wb.FullName) == os.path.normcase(WORKBOOK):
                    self.wb = wb
                    break
        except Exception:  # noqa: BLE001 — Excel not running
            self.app = None
        if self.wb is None:
            if not os.path.exists(WORKBOOK):
                raise FileNotFoundError(WORKBOOK)
            import win32com.client as wc
            self.app = wc.DispatchEx("Excel.Application")
            self.app.Visible = False
            self.app.DisplayAlerts = False
            self.wb = self.app.Workbooks.Open(WORKBOOK)
            self.mine = True
        return self

    def sheet(self, d: datetime.date):
        name = d.strftime("%b %Y")
        for s in self.wb.Worksheets:
            if s.Name == name:
                return s
        return None

    def __exit__(self, *exc):
        import pythoncom
        try:
            if self.mine and self.wb is not None:
                self.wb.Close(SaveChanges=False)
                self.app.Quit()
        finally:
            self.wb = self.app = None
            pythoncom.CoUninitialize()
        return False


def work_hours(args: dict) -> str:
    action = str(args.get("action") or "read").lower()
    today = datetime.date.today()
    d = _day(str(args.get("date") or ""), today) or today
    try:
        with _Book() as book:
            if action in ("log", "set", "add"):
                h = _hours(args.get("hours"))
                if h is None or not 0 <= h <= 24:
                    return f"ERROR: couldn't read hours from {args.get('hours')!r}"
                s = book.sheet(d)
                if s is None:
                    return f"ERROR: the workbook has no sheet for {d:%B %Y}"
                cell = s.Range(f"D{5 + d.day}")
                old = float(cell.Value2 or 0) * 24
                new = old + h if action == "add" else h
                cell.Value2 = new / 24
                book.wb.Save()
                was = f" (was {_hm(old)})" if old else ""
                return (f"logged {_hm(new)} for {d:%A %d %B}{was}; saved. "
                        + _summary(book, d, today))
            return _summary(book, d, today, detail=True)
    except FileNotFoundError:
        return f"ERROR: work-hours workbook not found at {WORKBOOK}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: Excel: {type(e).__name__}: {e}"


def _summary(book: _Book, d: datetime.date, today: datetime.date, detail=False) -> str:
    s = book.sheet(d)
    if s is None:
        return f"no sheet for {d:%B %Y}"
    # Monday to Sunday, across two month sheets when the week straddles them
    monday = d - datetime.timedelta(days=d.weekday())
    week_h, days = 0.0, []
    for i in range(7):
        x = monday + datetime.timedelta(days=i)
        sx = book.sheet(x)
        if sx is None:
            continue
        v = float(sx.Range(f"D{5 + x.day}").Value2 or 0) * 24
        week_h += v
        if v and x <= today:
            days.append(f"{x:%a %d} {_hm(v)}")
    total = float(s.Range("I13").Value2 or 0) * 24
    out = (f"Week {monday:%d %b}-{monday + datetime.timedelta(days=6):%d %b}: "
           f"{_hm(week_h)} h" + (f" ({', '.join(days)})" if detail and days else "")
           + f". {d:%B} so far: {_hm(total)} h over {int(s.Range('I12').Value2 or 0)} days, "
           f"gross {float(s.Range('I18').Value2 or 0):.2f} EUR, net after housing "
           f"{float(s.Range('I20').Value2 or 0):.2f} EUR, "
           f"{_hm(float(s.Range('I21').Value2 or 0) * 24)} h left before the higher rate.")
    return out


NAMES = {"write_file", "save_chat_log", "work_hours"}

DECLS = [
    {"name": "write_file",
     "description": "Make a text file (a note, list, letter, roadmap, Notepad file) "
                    "or add to one, and open it on his screen. A bare name goes "
                    "on his Desktop; never overwrites (a taken name gets ' (2)'). "
                    "Use it for 'create a notepad / write this down / save this'.",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "file name or full path; "
                                                   "empty = 'GOAT note <time>.txt'"},
         "content": {"type": "string", "description": "the full text to write"},
         "mode": {"type": "string", "enum": ["new", "append"]},
         "open": {"type": "boolean", "description": "open it after (default true)"}},
         "required": ["content"]}},
    {"name": "save_chat_log",
     "description": "Save GOAT's own chat with him (what he said, what you answered) "
                    "for some days into a text file on his Desktop and open it.",
     "parameters": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "how many days back to today, default 2"},
         "from": {"type": "string", "description": "first day: YYYY-MM-DD, 'yesterday', '28.10'"},
         "to": {"type": "string", "description": "last day, default today"},
         "path": {"type": "string"}}}},
    {"name": "work_hours",
     "description": "His work-hours & pay workbook (Work-Hours-Pay.xlsx). "
                    "action 'read' = this week's and this month's hours and pay "
                    "(date picks another week); 'log' = set the hours for a day "
                    "(date default today); 'add' = add hours to that day. The "
                    "week is Monday to Sunday — never quote the month total as "
                    "the week.",
     "parameters": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["read", "log", "add"]},
         "hours": {"type": "string", "description": "'10', '8:30', '7.5'"},
         "date": {"type": "string", "description": "'today', 'yesterday', "
                                                   "YYYY-MM-DD or '29.10'"}},
         "required": ["action"]}},
]


def execute(name: str, args: dict) -> str:
    args = args or {}
    try:
        if name == "write_file":
            return write_file(args)
        if name == "save_chat_log":
            return save_chat_log(args)
        if name == "work_hours":
            return work_hours(args)
    except Exception as e:  # noqa: BLE001 — a hand never kills the voice
        return f"ERROR: {type(e).__name__}: {e}"
    return f"ERROR: unknown tool {name}"
