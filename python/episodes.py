"""Episodic memory: GOAT remembers each day as a day, on its own.

Layer 3 of his 2026-10-20 ask ("give the GOAT the consciousness"): the
journal/RECENT.md notes only exist when the brain remembers to write them at
wrap-up, and they record tasks, never how the day went or how he was. This
fills that in without him or the brain doing anything: once a day is over
(days run 05:00 to 05:00 — he works past midnight), its transcript lines are
read by Haiku in one call and become

  * a "## Episode" section in workspace/journal/YYYY-MM-DD.md, and
  * one line in the "Recent days" block of workspace/self.md, which
    workspace/CLAUDE.md loads into every session.

Cost (his question, same day): one Haiku call per finished day, run in the
background — never inside a turn, never while the usage limit is hit or the
5-hour quota is above 85%.
"""
import asyncio
import datetime
import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKSPACE = os.path.join(ROOT, "workspace")
TRANSCRIPT = os.path.join(WORKSPACE, "transcript.jsonl")
JOURNAL = os.path.join(WORKSPACE, "journal")
SELF_MD = os.path.join(WORKSPACE, "self.md")
STATE = os.path.join(ROOT, ".episodes.json")
PLAN_FILE = os.path.join(ROOT, ".plan_usage.json")

MODEL = "claude-haiku-5-5"
DAY_START_H = 5          # a "day" is 05:00 -> 05:00
LOOKBACK = 3             # never digest further back than this many days
KEEP_LINES = 6           # episode lines kept in self.md
MAX_CHARS = 24000        # transcript text per digest (~7k tokens)
START = "<!-- episodes:start -->"
END = "<!-- episodes:end -->"

SYSTEM = (
    "You are GOAT, Giorgi's personal voice assistant, writing your own diary "
    "entry about one day you spent with him, from the transcript of what he "
    "said and what you answered. First person (\"I\"), plain English, no "
    "markdown beyond the format below. Only what the transcript shows: never "
    "invent events or feelings. Mood is his, inferred only from clear signals "
    "(frustration, repeating himself, excitement, late hours); if unclear, "
    "leave it out. A \"[... cut ...]\" line only means the transcript was "
    "trimmed for length: never mention it.\n\nExact format:\n"
    "LINE: <one line, max 30 words: what the day was about, and how he "
    "seemed if clear>\n"
    "- <what happened, 3 to 6 bullets, most important first>\n"
    "HOW I DID: <one honest line: where I helped, where I failed him>\n"
    "OPEN: <threads left unfinished, or none>")


def day_of(t: float) -> datetime.date:
    return (datetime.datetime.fromtimestamp(t)
            - datetime.timedelta(hours=DAY_START_H)).date()


def _load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(st: dict):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


def _transcript_by_day(days: set) -> dict:
    out: dict = {d: [] for d in days}
    try:
        with open(TRANSCRIPT, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                    d = day_of(float(e["t"]))
                except (ValueError, KeyError, TypeError):
                    continue
                if d in out:
                    out[d].append(e)
    except OSError:
        pass
    return out


def pending_days(now: float | None = None) -> list:
    """Finished days inside LOOKBACK that have talk and no episode yet."""
    today = day_of(now or time.time())
    done = set(_load_state().get("done", []))
    cand = {today - datetime.timedelta(days=i) for i in range(1, LOOKBACK + 1)}
    cand = {d for d in cand if d.isoformat() not in done}
    if not cand:
        return []
    by = _transcript_by_day(cand)
    return sorted(d for d in cand if by[d])


def render(entries: list) -> str:
    """Transcript of one day as compact text, capped at MAX_CHARS. His words
    are kept fuller than mine — they carry what the day was about."""
    rows = []
    for e in entries:
        hm = datetime.datetime.fromtimestamp(float(e["t"])).strftime("%H:%M")
        him = re.sub(r"\s+", " ", str(e.get("user", "")))[:300]
        me = re.sub(r"\s+", " ", str(e.get("reply", "")))[:160]
        rows.append(f"{hm} him: {him}\n      me: {me}")
    text = "\n".join(rows)
    if len(text) > MAX_CHARS:     # keep the start and the end of the day
        half = MAX_CHARS // 2
        text = text[:half] + "\n[... cut ...]\n" + text[-half:]
    return text


async def _ask(prompt: str, system: str = SYSTEM) -> str:
    """One tool-less Haiku turn (also used by inner_life's reflections)."""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  TextBlock, query)
    opts = ClaudeAgentOptions(
        model=MODEL, system_prompt=system, tools=[], setting_sources=[],
        strict_mcp_config=True, max_turns=1, cwd=ROOT)
    out = []
    async for m in query(prompt=prompt, options=opts):
        if isinstance(m, AssistantMessage):
            out += [b.text for b in m.content if isinstance(b, TextBlock)]
    return "".join(out).strip()


def digest(day: datetime.date, entries: list) -> str:
    prompt = (f"The day: {day:%A %Y-%m-%d} (05:00 to 05:00). "
              f"{len(entries)} exchanges.\n\n" + render(entries))
    return asyncio.run(_ask(prompt))


def _line(text: str) -> str:
    m = re.search(r"^LINE:\s*(.+)$", text, re.M)
    return (m.group(1) if m else text.splitlines()[0]).strip()[:240]


def write_journal(day: datetime.date, text: str):
    os.makedirs(JOURNAL, exist_ok=True)
    path = os.path.join(JOURNAL, f"{day.isoformat()}.md")
    body = re.sub(r"^LINE:\s*", "", text, count=1, flags=re.M)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n## Episode (my own recollection of the day)\n{body}\n")


def write_self(day: datetime.date, text: str):
    """Newest-first one-liners between the markers; the rest of self.md is
    the brain's and is never touched."""
    try:
        with open(SELF_MD, encoding="utf-8") as f:
            doc = f.read()
    except OSError:
        return
    if START not in doc or END not in doc:
        return
    head, rest = doc.split(START, 1)
    block, tail = rest.split(END, 1)
    lines = [ln for ln in block.strip().splitlines() if ln.startswith("- ")]
    key = f"- {day.isoformat()}"
    lines = [ln for ln in lines if not ln.startswith(key)]
    lines.append(f"{key} ({day:%a}): {_line(text)}")
    lines.sort(reverse=True)
    doc = (head + START + "\n" + "\n".join(lines[:KEEP_LINES]) + "\n" + END
           + tail)
    tmp = SELF_MD + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(doc)
    os.replace(tmp, SELF_MD)


def _quota_ok(app=None) -> bool:
    if app is not None:
        if getattr(app, "_asleep", False):
            return False     # quiet boot promises zero Claude usage
        hit = getattr(app, "_limit_hit_at", 0.0) or 0.0
        reset_at = getattr(app, "claude_reset_at", 0.0) or 0.0
        if hit and (not reset_at or time.time() < reset_at):
            return False
    try:
        with open(PLAN_FILE, encoding="utf-8") as f:
            return float(json.load(f).get("five_hour") or 0) < 85
    except (OSError, ValueError, TypeError):
        return True


def run_once(app=None) -> list:
    """Digest every pending day. Returns the days written."""
    wrote = []
    for day in pending_days():
        if not _quota_ok(app):
            break
        entries = _transcript_by_day({day})[day]
        try:
            text = digest(day, entries)
        except Exception as e:  # noqa: BLE001 — try again next hour
            print(f"[episodes] {day}: {type(e).__name__}: {str(e)[:160]}")
            break
        if not text or "LINE:" not in text:
            print(f"[episodes] {day}: odd reply, skipped: {text[:120]!r}")
            break
        write_journal(day, text)
        write_self(day, text)
        st = _load_state()
        st["done"] = sorted(set(st.get("done", [])) | {day.isoformat()})[-60:]
        _save_state(st)
        print(f"[episodes] {day}: {_line(text)}")
        wrote.append(day)
    return wrote


def start(app=None, delay: float = 180, every: float = 3600):
    """Background thread: first pass a few minutes after boot (never in the
    boot rush), then hourly. Never raises into GOAT."""
    def loop():
        time.sleep(delay)
        while True:
            try:
                run_once(app)
            except Exception as e:  # noqa: BLE001
                print(f"[episodes] {type(e).__name__}: {e}")
            time.sleep(every)
    threading.Thread(target=loop, daemon=True, name="episodes").start()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "pending":
        print([d.isoformat() for d in pending_days()])
    else:
        print(run_once())
