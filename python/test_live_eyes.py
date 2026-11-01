"""Live proof of live_eyes: a throwaway window of our own changes its text and
title on its own; the eyes must see it open, follow its text live, coalesce
the ticking title, serve a fresh cache, and see it close. Touches no input.

Run: py -3.13 test_live_eyes.py
"""
from __future__ import annotations

import subprocess
import sys
import time

import psutil

import live_eyes
import screen_hands

TITLE = "live-eyes-test-window"
CHILD = r"""
import tkinter as tk
r = tk.Tk(); r.title("%s"); r.geometry("520x260+300+200")
r.attributes("-topmost", True)
lab = tk.Label(r, text="tick 0", font=("Segoe UI", 28)); lab.pack(expand=True)
tk.Button(r, text="Approve order").pack()
n = [0]
def step():
    n[0] += 1
    lab.config(text="tick %%d" %% n[0]); r.title("%s %%d" %% n[0])
    if n[0] < 40: r.after(600, step)
    else: r.destroy()
r.after(600, step); r.mainloop()
""" % (TITLE, TITLE)

fails = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not ok:
        fails.append(name)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    screen_hands.dpi_init()
    eyes = live_eyes.LiveEyes()
    eyes.skip_pids |= {w["pid"] for w in screen_hands.windows()
                       if w["title"].strip() == "GOAT"}
    eyes.start()
    time.sleep(1.0)
    proc = psutil.Process()
    proc.cpu_percent()
    child = subprocess.Popen([sys.executable, "-c", CHILD])
    try:
        e = eyes.wait_event(lambda e: TITLE in e["text"] and
                            e["kind"] in ("opened", "focus"), 6)
        check("sees the window open", e is not None, e and e["text"])
        t0 = time.monotonic()
        while time.monotonic() - t0 < 5 and \
                TITLE not in eyes.front.get("title", ""):
            time.sleep(0.1)
        check("tracks it as the front window", TITLE in eyes.front.get("title", ""),
              eyes.front.get("title", ""))
        took = eyes.wait_text("tick 8", timeout=10)
        check("reads its changing text live", took is not None,
              f"{took:.1f}s" if took else "timeout")
        got = eyes.fresh(eyes.front.get("hwnd"), eyes.box)
        check("serves a fresh cache", got is not None and any(
            "tick" in ln["name"] for ln in got[1]),
              got and f"{len(got[0])} ui, {len(got[1])} lines")
        # Tk exposes almost nothing to UI Automation; OCR must still see it.
        check("cache has the button text", got is not None and any(
            "Approve order" in u["name"] for u in got[0] + got[1]))
        time.sleep(3)
        tit = [e for e in eyes.since() if e["kind"] == "title" and TITLE in e["text"]]
        check("ticking title coalesced into one event",
              len(tit) == 1 and tit[0].get("n", 1) > 3,
              tit and f"{len(tit)} events, n={tit[-1].get('n')}")
        txt = [e for e in eyes.since() if e["kind"] == "text"]
        check("text-change events logged", bool(txt), txt and txt[-1]["text"][:80])
        cpu = proc.cpu_percent()
        check("cpu under 15% of one core while watching a changing window",
              cpu < 15, f"{cpu:.1f}%")
        e = eyes.wait_event(lambda e: e["kind"] == "closed" and TITLE in e["text"], 30)
        check("sees the window close", e is not None, e and e["text"])
        print(eyes.digest(seconds=60, limit=8))
    finally:
        if child.poll() is None:
            child.kill()
        eyes.stop()
    print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
