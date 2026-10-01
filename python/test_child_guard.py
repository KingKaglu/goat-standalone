"""Engine dies with GOAT; what the engine launched does not (2026-09-27).

Run: py -3.13 test_child_guard.py
A helper process plays GOAT: it spawns a stand-in "engine" (cmd.exe) that in
turn launches an app (ping.exe), adopts the engine into the kill-on-close
job, and is then hard-killed like Stop-Process -Force would. The engine must
die with it; the app it launched must survive (SILENT_BREAKAWAY_OK).
Also checks the screen hands refuse alt+f4 while GOAT itself has focus.
"""
import os
import subprocess
import sys
import time

import child_guard

HELPER = r'''
import subprocess, sys, time
sys.path.insert(0, {here!r})
import child_guard
child_guard.ENGINE_NAMES = {{"cmd.exe"}}
eng = subprocess.Popen("cmd /c ping -n 60 127.0.0.1 >nul", creationflags=0x08000000)
time.sleep(1.0)
print("adopted", child_guard.adopt_once(), flush=True)
print("engine", eng.pid, flush=True)
time.sleep(60)
'''


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout
    return str(pid) in out


def _children(pid: int, name: str) -> list[int]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-c",
         f"(Get-CimInstance Win32_Process -Filter \"ParentProcessId={pid} "
         f"AND Name='{name}'\").ProcessId"],
        capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def test_engine_dies_with_goat():
    here = os.path.dirname(os.path.abspath(__file__))
    h = subprocess.Popen([sys.executable, "-c", HELPER.format(here=here)],
                         stdout=subprocess.PIPE, text=True)
    adopted = h.stdout.readline().strip()
    engine = int(h.stdout.readline().split()[1])
    assert adopted == f"adopted [{engine}]", adopted
    time.sleep(0.5)
    app = _children(engine, "PING.EXE") or _children(engine, "ping.exe")
    assert app, "engine never launched its app"
    assert _alive(engine)
    subprocess.run(["taskkill", "/F", "/PID", str(h.pid)], capture_output=True)
    time.sleep(1.0)
    try:
        assert not _alive(engine), "ORPHAN: engine outlived GOAT"
        assert _alive(app[0]), "app launched by the engine was killed too"
    finally:
        for p in (engine, *app):
            subprocess.run(["taskkill", "/F", "/PID", str(p)], capture_output=True)
    print("ok  engine killed with GOAT, its app kept running")


def test_alt_f4_refused_on_self():
    import screen_hands
    real = screen_hands._user32.GetWindowThreadProcessId

    def fake(hwnd, pid_ref):
        pid_ref._obj.value = os.getpid()
        return 1
    screen_hands._user32.GetWindowThreadProcessId = fake
    try:
        for combo in ("alt+f4", "f4+alt", "ctrl+a alt+f4", "ctrl+f4"):
            try:
                screen_hands._refuse_closing_self(combo)
            except ValueError as e:
                assert "GOAT itself" in str(e)
            else:
                raise AssertionError(f"{combo} was not refused on GOAT's own window")
        screen_hands._refuse_closing_self("ctrl+s")  # harmless keys pass
    finally:
        screen_hands._user32.GetWindowThreadProcessId = real

    def other(hwnd, pid_ref):
        pid_ref._obj.value = 4
        return 1
    # The close guard looks at the REAL foreground window (when this runs
    # from a terminal, that's a terminal). Pin its verdict per case.
    import close_guard
    real_reason = close_guard.window_reason
    screen_hands._user32.GetWindowThreadProcessId = other
    try:
        close_guard.window_reason = lambda hwnd, title="": None
        screen_hands._refuse_closing_self("alt+f4")  # another app: allowed
        close_guard.window_reason = lambda hwnd, title="": "Windows Terminal — a terminal"
        close_guard.clear()
        try:
            screen_hands._refuse_closing_self("alt+f4")
        except ValueError as e:
            assert "ask" in str(e).lower() and close_guard.pending() is not None
        else:
            raise AssertionError("alt+f4 on a protected window was not refused")
        close_guard.grant()
        screen_hands._refuse_closing_self("alt+f4")  # after his yes: allowed
        close_guard._grant_until = 0.0
    finally:
        screen_hands._user32.GetWindowThreadProcessId = real
        close_guard.window_reason = real_reason
    print("ok  alt+f4 refused on GOAT's own window and on a live session "
          "until he says yes, allowed on others")


if __name__ == "__main__":
    test_alt_f4_refused_on_self()
    test_engine_dies_with_goat()
    print("ALL PASS")
