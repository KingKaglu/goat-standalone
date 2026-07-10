"""GOAT self-edit safety net — the rule that a change to GOAT's own code can
never brick GOAT.

Three jobs, one file:

  preflight  (default) — prove the code currently on disk can boot BEFORE the
             running instance is killed: byte-compile every core module, then
             import the whole stack in a fresh subprocess (catches top-level
             runtime errors compile can't see), then verify the assets the app
             cannot live without. Exit 0 + "PREFLIGHT PASS" means safe.
  snapshot   — copy the core modules to .self-backup/last-good/. Called
             automatically by goat_app right after a successful boot, so the
             backup is by definition a version that actually ran.
  rollback   — restore .self-backup/last-good/ over python/. The escape
             hatch when a bad edit slipped through anyway.

restart-goat.ps1 runs `preflight` and refuses to kill the live app when it
fails; after relaunch it watches the fresh instance and, if it dies at boot,
restores the snapshot itself (in PowerShell — this file might be the thing
that's broken) and relaunches.

Run:  python C:/Users/user/goat-standalone/python/self_check.py [preflight|snapshot|rollback]
"""
import json
import os
import py_compile
import shutil
import subprocess
import sys

PY_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PY_DIR)
BACKUP = os.path.join(PY_DIR, ".self-backup", "last-good")

CORE = [
    "goat_paths.py",
    "audio_io.py",
    "stt_whisper.py",
    "tts_edge.py",
    "tts_piper.py",
    "goat_app.py",
    "ui_qt.py",
    "goat_doctor.py",
    "self_check.py",
]

# goat_doctor runs its probes at import time (it's a script), so it is
# compile-checked but excluded from the import smoke test.
IMPORT_TEST = "import goat_paths, stt_whisper, tts_edge, tts_piper, audio_io, goat_app, ui_qt"

# Things the app dies or goes deaf/mute without.
ASSETS = [
    os.path.join(ROOT, "stt", "bin", "Release", "whisper-server.exe"),
    os.path.join(ROOT, "stt", "ggml-base.en.bin"),
    os.path.join(ROOT, "tts", "piper", "piper.exe"),
    os.path.join(ROOT, "tts", "en_GB-alan-low.onnx"),
    os.path.join(ROOT, "workspace"),
    os.path.join(PY_DIR, "start-goat-app.vbs"),
    os.path.join(PY_DIR, "restart-goat.ps1"),
]


def preflight() -> list:
    problems = []

    for name in CORE:
        path = os.path.join(PY_DIR, name)
        if not os.path.exists(path):
            problems.append(f"missing module: {name}")
            continue
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            problems.append(f"compile error in {name}: {e.msg.strip().splitlines()[-1]}")

    if not problems:
        # Fresh interpreter so a stale sys.modules can't mask a broken import.
        # This loads torch/Qt/the SDK exactly like a real boot would — slow
        # (tens of seconds) and worth it: it's the same code path that decides
        # whether the next launch lives.
        try:
            r = subprocess.run(
                [sys.executable, "-c", IMPORT_TEST],
                cwd=PY_DIR, capture_output=True, text=True, timeout=180,
            )
            if r.returncode != 0:
                tail = (r.stderr or "").strip().splitlines()
                problems.append("import test failed: " + (tail[-1] if tail else "unknown"))
        except subprocess.TimeoutExpired:
            problems.append("import test hung (>180s)")

    for path in ASSETS:
        if not os.path.exists(path):
            problems.append(f"missing asset: {os.path.relpath(path, ROOT)}")

    try:
        with open(os.path.join(ROOT, "stt-fixes.json"), encoding="utf-8") as f:
            json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        problems.append(f"stt-fixes.json unreadable: {type(e).__name__}")

    return problems


def snapshot() -> int:
    """Keep the last code that provably booted. Called by goat_app after a
    successful boot — never snapshot from here on unverified disk state."""
    os.makedirs(BACKUP, exist_ok=True)
    n = 0
    for name in CORE:
        src = os.path.join(PY_DIR, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(BACKUP, name))
            n += 1
    print(f"snapshot: {n} modules -> {BACKUP}")
    return 0


def rollback() -> int:
    if not os.path.isdir(BACKUP):
        print("rollback: no last-good snapshot exists yet")
        return 1
    n = 0
    for name in CORE:
        src = os.path.join(BACKUP, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(PY_DIR, name))
            n += 1
    print(f"rollback: restored {n} modules from last-good")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "preflight"
    if cmd == "snapshot":
        return snapshot()
    if cmd == "rollback":
        return rollback()
    problems = preflight()
    if problems:
        for p in problems:
            print(f"[FAIL] {p}")
        print("PREFLIGHT FAIL — do NOT restart; fix or run rollback.")
        return 1
    print("PREFLIGHT PASS — safe to restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
