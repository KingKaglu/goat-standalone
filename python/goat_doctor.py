"""GOAT self-diagnostic — run by GOAT's own brain when Giorgi asks how it's
doing ("are you okay", "status", "run diagnostics"). Read-only probes, safe
while the app is live. Prints a compact report; the brain speaks ONE line.

Run:  python C:/Users/user/goat-standalone/python/goat_doctor.py
"""
import json
import os
import socket
import subprocess
import time

import httpx

PY_DIR = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(PY_DIR, "goat-app.log")
SESSION = "C:/Users/user/goat-standalone/.goat-session-py"
FIXES = "C:/Users/user/goat-standalone/stt-fixes.json"
STT_PORT = 3781

checks = []


def check(name, ok, detail=""):
    checks.append((name, ok, detail))
    print(f"[{' OK ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# 1. Is the app itself running (exactly one instance)?
try:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\""
         " | Where-Object { $_.CommandLine -match 'ui_qt' } | Measure-Object).Count"],
        capture_output=True, text=True, timeout=20)
    n = int((out.stdout or "0").strip() or 0)
    check("app process", n == 1, f"{n} instance(s)")
except Exception as e:  # noqa: BLE001
    check("app process", False, repr(e))

# 2. Hearing: whisper-server on 3781.
try:
    httpx.get(f"http://127.0.0.1:{STT_PORT}/", timeout=2.0)
    check("hearing (whisper :3781)", True, "responding")
except httpx.HTTPError as e:
    # Any HTTP-level grumble still means something is listening.
    ok = not isinstance(e, (httpx.ConnectError, httpx.TimeoutException))
    check("hearing (whisper :3781)", ok, type(e).__name__)

# 3. Ava voice route (edge-tts endpoint reachability; Piper covers offline).
try:
    socket.create_connection(("speech.platform.bing.com", 443), timeout=3).close()
    check("ava voice route", True, "reachable")
except OSError as e:
    check("ava voice route", False, f"{e} (Piper fallback would carry the voice)")

# 4. Recent log health: survived-error counters and crash lines.
try:
    with open(LOG, encoding="utf-8", errors="ignore") as f:
        tail = f.readlines()[-80:]
    bad = [ln.strip() for ln in tail
           if any(k in ln.lower() for k in
                  ("callback error", "vad] error", "crash", "traceback", "failed"))]
    check("log (last 80 lines)", len(bad) == 0,
          f"{len(bad)} suspicious line(s)" + (f"; latest: {bad[-1][:90]}" if bad else ""))
except OSError:
    check("log (last 80 lines)", True, "no log file (fresh boot?)")

# 5. Session continuity file.
if os.path.exists(SESSION):
    age_h = (time.time() - os.path.getmtime(SESSION)) / 3600
    check("session file", True, f"present, touched {age_h:.1f}h ago")
else:
    check("session file", False, "missing — next boot starts a fresh brain")

# 6. STT learned-fixes file parses.
try:
    with open(FIXES, encoding="utf-8") as f:
        n_fix = len([k for k in json.load(f) if not k.startswith("_")])
    check("stt fixes", True, f"{n_fix} learned corrections")
except (OSError, json.JSONDecodeError) as e:
    check("stt fixes", False, type(e).__name__)

fails = [c for c in checks if not c[1]]
print()
if fails:
    print(f"verdict: {len(fails)} issue(s) — " + "; ".join(c[0] for c in fails))
else:
    print("verdict: all systems nominal")
