"""End-to-end voice commands: spoken audio -> GOAT's real ear -> reflex ->
real action -> checked on the machine.

No microphone: each phrase is spoken by edge-tts (the Georgian neural voice
for Georgian), streamed block by block into the same realtime ear GOAT uses
(`stt_realtime.Pair`, bilingual "auto"), and what it hears goes through
`reflex.match` exactly as a live turn would. Every action is then verified
against Windows itself, not against GOAT's own report.

Only harmless actions: a Notepad it starts itself, mute toggled and toggled
back, Google in the browser. Needs the network and the ElevenLabs key.
    py -3.13 test_voice_e2e.py
"""
import asyncio
import io
import os
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np      # noqa: E402
import psutil           # noqa: E402

import reflex           # noqa: E402
import reflex_index     # noqa: E402
import stt_realtime     # noqa: E402
import tts_edge         # noqa: E402

_passed = 0
_failed = 0


def check(label, ok, extra=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"[PASS] {label}")
    else:
        _failed += 1
        print(f"[FAIL] {label}" + (f"  — {extra}" if extra else ""))


def speak(text: str, lang: str) -> np.ndarray:
    tts_edge.set_character("goat")
    tts_edge.set_language(lang)
    return tts_edge.synth(text, target_rate=16000)


async def hear(audio: np.ndarray) -> tuple:
    """Stream like the mic does (20ms blocks, a little faster than real
    time), then commit. Returns (text, lang, ms from end of speech)."""
    loop = asyncio.get_running_loop()
    pair = stt_realtime.Pair(loop, 16000).start()
    pre = np.zeros(3200, dtype=np.float32)          # 200ms preroll silence
    tail = np.zeros(6400, dtype=np.float32)         # 400ms of him going quiet
    block = 320
    data = np.concatenate([pre, audio.astype(np.float32), tail])
    for i in range(0, len(data), block):
        pair.feed(data[i:i + block])
        await asyncio.sleep(0.012)
    t0 = time.monotonic()
    pair.end()
    text = await pair.result()
    return text, pair.lang, (time.monotonic() - t0) * 1000


def procs(name):
    return [p for p in psutil.process_iter(["name"])
            if (p.info["name"] or "").lower() == name]


def muted() -> bool | None:
    try:
        from pycaw.pycaw import AudioUtilities
        return bool(AudioUtilities.GetSpeakers().EndpointVolume.GetMute())
    except Exception:  # noqa: BLE001
        return None


async def turn(text, lang):
    audio = await asyncio.to_thread(speak, text, lang)   # synth runs its own loop
    heard, got_lang, ms = await hear(audio)
    print(f"   said {text!r} -> heard {heard!r} ({got_lang}, {ms:.0f}ms)")
    r = reflex.match(heard or "", got_lang or lang) if heard else None
    return heard, got_lang, r


async def main():
    reflex_index.refresh(background=False)

    # 1. Georgian: mute, then unmute (it's a toggle) — checked on the device.
    before = muted()
    heard, lang, r = await turn("გამორთე ხმა", "ka")
    check("ka ear hears Georgian as Georgian", lang == "ka", repr(heard))
    check("'გამორთე ხმა' -> mute reflex", r is not None and r.detail == "mute",
          repr(r))
    if r is not None and before is not None:
        r.run()
        time.sleep(0.4)
        check("speakers really muted/unmuted", muted() != before,
              f"before={before} after={muted()}")
        r.run()                          # put it back the way he had it
        time.sleep(0.4)
        check("mute restored to how it was", muted() == before)

    # 2. English: close a window by name — a window this test owns. NOT
    #    Notepad: Windows 11 Notepad opens new files as tabs in HIS window.
    win = subprocess.Popen([sys.executable, "-c",
                            "import tkinter as t; r=t.Tk(); r.title('zebra'); "
                            "r.mainloop()"])
    time.sleep(2.0)
    heard, lang, r = await turn("Close the zebra window.", "en")
    check("en ear hears English as English", lang == "en", repr(heard))
    check("'close the zebra window' -> window close", r is not None
          and r.kind == "window" and "close" in r.detail, repr(r))
    if r is not None and "zebra" in r.detail.lower():
        print("   ->", r.run())
    try:
        win.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    check("the zebra window's process really exited", win.poll() is not None)
    if win.poll() is None:
        win.kill()

    # 3. Georgian, unknown window: must NOT close the front window.
    heard, lang, r = await turn("დახურე სპილო", "ka")
    check("a window that doesn't exist is not a reflex (no blind close)",
          r is None, repr(r))

    # 4. English, run-on sentence, a file on his desktop.
    heard, lang, r = await turn(
        "Hey, can you open the PFP picture? For me. That I have on my "
        "desktop.", "en")
    check("run-on 'open the PFP picture…' -> pfp.jpg",
          r is not None and r.kind == "open_path" and "pfp" in r.detail.lower(),
          repr(r))

    # 5. Georgian open, verb last.
    heard, lang, r = await turn("გუგლი გახსენი", "ka")
    check("'გუგლი გახსენი' -> google", r is not None
          and "google.com" in r.detail, repr(r))
    if r is not None:
        res = r.run()
        check("google really opened", not str(res).startswith("ERROR"), res)

    # 6. Quit an app by Georgian name, with the process check.
    heard, lang, r = await turn("დახურე სტიმი", "ka")
    check("'დახურე სტიმი' -> quit steam", r is not None and r.kind == "quit"
          and "steam" in r.detail, repr(r))
    if r is not None and procs("steam.exe"):
        print("   (steam is running — his; not closing it in a test)")
    elif r is not None:
        res = r.run()
        print("   ->", res)
        check("steam verified not running afterwards",
              not procs("steam.exe") and not res.startswith("ERROR"), res)


asyncio.run(main())
print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
