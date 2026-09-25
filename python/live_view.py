"""What is happening on his laptop RIGHT NOW, as one short note.

His complaint, 2026-09-23: asked about his screen, GOAT answered "I can't see
the screen directly — that's the working side's job, but I can check which
terminal processes are running." He wants GOAT to perceive everything on the
machine instantly. A screenshot is real sight but costs a tool round trip and
image tokens; most questions ("what's open", "what am I watching", "what is
eating my CPU", "close that") only need the desktop's STATE, and that is
cheap: EnumWindows is ~2ms, a psutil pass ~30ms.

So every turn carries this note, built fresh when he speaks. The brain
answers from it directly, and reaches for a screenshot only when it needs
pixels (what an error says, what's inside a page).
"""
import ctypes
import datetime
import os
import time

import psutil

import screen_hands

_ME = os.getpid()
# CPU % in psutil is "since the last call on this process object", so the
# objects are kept between turns: the number means "since he last spoke".
_procs: dict = {}
_primed_at = [0.0]

# Titles that are never his content.
_SKIP_TITLES = {"Program Manager", "Windows Input Experience", "GOAT"}


def _cpu_top(n: int = 5) -> list:
    seen = set()
    rows = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        pid = p.info["pid"]
        seen.add(pid)
        proc = _procs.get(pid)
        if proc is None:
            _procs[pid] = p
            try:
                p.cpu_percent(None)          # prime; first reading is 0
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            continue
        try:
            cpu = proc.cpu_percent(None) / (psutil.cpu_count() or 1)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        mem = (p.info.get("memory_info").rss if p.info.get("memory_info")
               else 0) // (1024 * 1024)
        name = (p.info.get("name") or "?").removesuffix(".exe")
        if pid in (0, 4) or name == "System Idle Process":
            continue
        rows.append((cpu, mem, name))
    for pid in list(_procs):
        if pid not in seen:
            _procs.pop(pid, None)
    rows.sort(reverse=True)
    return rows[:n]


def prime():
    """Call once at boot so the first turn's CPU numbers are real."""
    _cpu_top()
    _primed_at[0] = time.monotonic()


def _front_title() -> str:
    u = ctypes.windll.user32
    hwnd = u.GetForegroundWindow()
    n = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def note(max_windows: int = 12) -> str:
    """One compact block. Never raises — sight failing must not cost a turn."""
    parts = []
    try:
        wins = screen_hands.windows()
        pid_name = {}
        for w in wins:
            try:
                pid_name[w["pid"]] = psutil.Process(w["pid"]).name().removesuffix(
                    ".exe")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pid_name[w["pid"]] = "?"
        front = _front_title()
        # The front window, ignoring GOAT's own (he is talking to it).
        mine = [w for w in wins if w["pid"] != _ME
                and w["title"] not in _SKIP_TITLES]
        top = next((w for w in mine if not w["minimized"]), None)
        if front and front not in _SKIP_TITLES:
            parts.append(f"front window: {front}")
        elif top:
            parts.append(f"front window (under GOAT): {top['title']} "
                         f"[{pid_name.get(top['pid'], '?')}]")
        listed = [f"{w['title'][:70]} [{pid_name.get(w['pid'], '?')}"
                  + (", minimized" if w["minimized"] else "") + "]"
                  for w in mine[:max_windows]]
        if listed:
            parts.append("open windows, top first: " + " | ".join(listed))
    except Exception as e:  # noqa: BLE001
        parts.append(f"(window list unavailable: {e})")
    try:
        rows = _cpu_top()
        if rows:
            parts.append("busiest processes since he last spoke: " + ", ".join(
                f"{name} {cpu:.0f}% cpu {mem}MB" for cpu, mem, name in rows))
        vm = psutil.virtual_memory()
        bat = psutil.sensors_battery()
        sysline = f"RAM {vm.percent:.0f}% used"
        if bat is not None:
            sysline += (f", battery {bat.percent:.0f}%"
                        + (" charging" if bat.power_plugged else " on battery"))
        parts.append(sysline)
    except Exception as e:  # noqa: BLE001
        parts.append(f"(process list unavailable: {e})")
    now = datetime.datetime.now()
    parts.append(f"time {now:%H:%M}")
    return "[live desktop, captured as he spoke — "  + "; ".join(parts) + "]"


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    prime()
    time.sleep(0.5)
    t = time.perf_counter()
    s = note()
    print(f"{(time.perf_counter() - t) * 1000:.0f}ms\n{s}")
