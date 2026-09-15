"""Live proof that GOAT's hands actually reach another application.

test_screen.py deliberately fires nothing — it can run while Giorgi works. This
one is the opposite: it opens a small throwaway window of its own, then drives
it with the real mouse and the real keyboard and checks that the window
received exactly what was sent. Typing, chords, clicks, and scroll all have to
survive the round trip through Windows' input queue, which is the only way to
know the hands work rather than merely return without error.

It is safe because it only ever targets its own window, and it puts GOAT's
window back the way it found it. Do not run it while typing elsewhere — for
the two seconds it runs, the keyboard belongs to it.

Run: py -3.13 test_screen_live.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

import screen_hands

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

TITLE = "GOAT-HANDS-TEST"
PASS, FAIL = [], []

TARGET = r'''
import ctypes, json, sys, tkinter as tk
# Tk is not DPI aware by default: Windows then lies to it about the screen, and
# the coordinates it reports for a click come back in a scaled-down space that
# does not match the physical pixel the mouse was actually sent to. Opt in, and
# the two agree exactly.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass
out = sys.argv[1]
root = tk.Tk()
root.title("GOAT-HANDS-TEST")
root.geometry("560x260+120+120")
root.attributes("-topmost", True)
state = {"clicks": [], "scroll": [], "chords": []}
entry = tk.Entry(root, font=("Segoe UI", 14))
entry.pack(fill="x", padx=12, pady=12)
entry.focus_force()
box = tk.Label(root, text="click target", bg="#c8102e", fg="white",
               width=20, height=4)
box.pack(pady=8)
def click(e):
    state["clicks"].append([e.x_root, e.y_root])
def wheel(e):
    state["scroll"].append(e.delta)
def chord(name):
    def handler(e):
        state["chords"].append(name)
        return "break"
    return handler
box.bind("<Button-1>", click)
root.bind("<MouseWheel>", wheel)
root.bind("<Control-s>", chord("ctrl+s"))
root.bind("<Control-Shift-KeyPress-P>", chord("ctrl+shift+p"))
def finish():
    state["text"] = entry.get()
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    root.destroy()
root.after(9000, finish)
root.bind("<Escape>", lambda e: finish())
root.mainloop()
'''


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    print("=== GOAT hands, live ===\n")
    tmp = os.path.join(tempfile.gettempdir(), "goat-hands-test.json")
    script = os.path.join(tempfile.gettempdir(), "goat-hands-target.py")
    for path in (tmp, script):
        if os.path.exists(path):
            os.remove(path)
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(TARGET)

    # GOAT floats on top; a test window underneath would eat every click.
    aside = screen_hands.stand_aside()
    proc = subprocess.Popen([sys.executable, script, tmp])
    try:
        win = None
        for _ in range(40):
            time.sleep(0.25)
            win = next((w for w in screen_hands.windows()
                        if w["title"] == TITLE), None)
            if win:
                break
        if not win:
            check("the test window opened", False)
            return 1
        check("the test window opened", True,
              f"{win['width']}x{win['height']} at ({win['left']}, {win['top']})")

        time.sleep(0.6)
        screen_hands.focus_window(str(win["hwnd"]))
        time.sleep(0.4)
        fg = screen_hands.foreground() or {}
        check("focus_window brings a window to the front",
              fg.get("title") == TITLE, repr(fg.get("title", ""))[:50])

        sent = "goat hands ხელები 123"
        screen_hands.type_text(sent)
        time.sleep(0.3)
        screen_hands.press("ctrl+s")
        screen_hands.press("ctrl+shift+p")
        time.sleep(0.2)

        # The red label sits under the entry; click its middle in real pixels.
        target_x = win["left"] + win["width"] // 2
        target_y = win["top"] + 150
        screen_hands.click(target_x, target_y)
        time.sleep(0.2)
        screen_hands.scroll(2, target_x, target_y)
        time.sleep(0.2)
        screen_hands.scroll(-1, target_x, target_y)
        time.sleep(0.3)

        screen_hands.press("escape")
        proc.wait(timeout=12)

        with open(tmp, encoding="utf-8") as fh:
            got = json.load(fh)
    finally:
        if proc.poll() is None:
            proc.kill()
        screen_hands.step_back_in()

    check("typed text arrives exactly, Georgian included",
          got.get("text") == sent, f"{got.get('text')!r}")
    check("ctrl+s reaches the app", "ctrl+s" in got.get("chords", []),
          str(got.get("chords")))
    check("ctrl+shift+p reaches the app", "ctrl+shift+p" in got.get("chords", []))
    clicks = got.get("clicks", [])
    check("the click lands on the target", len(clicks) == 1, str(clicks))
    if clicks:
        dx = abs(clicks[0][0] - target_x) + abs(clicks[0][1] - target_y)
        check("the click lands at the pixel asked for", dx <= 2,
              f"off by {dx}px")
    wheel = got.get("scroll", [])
    check("every wheel notch arrives", len(wheel) == 3, f"{wheel} (sent 2 up, 1 down)")
    check("wheel direction is preserved",
          len(wheel) == 3 and wheel[0] > 0 and wheel[1] > 0 and wheel[2] < 0,
          str(wheel))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
