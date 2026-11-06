"""The voice's hands can't be turned against him (2026-11-06 audit S5-S7):
no secret file is read, listed, found, opened or written; nothing that
launches a program is opened; the terminal-Claude message never passes
through a shell. Offline: no file outside a temp folder is touched, nothing
is opened, no process is started.
Run: py -3.13 test_voice_hands_safety.py"""
import os
import subprocess
import sys
import tempfile

import desk_hands
import secret_paths
import voice_hands as vh
from goat_paths import GOAT_ROOT

fails = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  — {info}" if info and not ok else ""))
    if not ok:
        fails.append(name)


def main() -> int:
    home = os.path.expanduser("~")
    # --- the list itself
    for p in (os.path.join(GOAT_ROOT, ".goat-secrets.json"),
              os.path.join(GOAT_ROOT, ".phone", "token.txt"),
              os.path.join(GOAT_ROOT, ".phone"),
              os.path.join(home, ".claude", ".credentials.json"),
              os.path.join(home, ".ssh", "id_ed25519"),
              os.path.join(GOAT_ROOT, "voiceid", "me.npy"),
              os.path.join(home, "Desktop", "server.pem"),
              os.path.join(home, "Documents", "proj", ".env"),
              os.path.join(home, "Documents", "proj", ".env.local"),
              os.path.join(GOAT_ROOT, "python", "..", ".goat-secrets.json")):
        check(f"secret: {os.path.relpath(p, home)}", secret_paths.is_secret(p))
    for p in (os.path.join(home, "Desktop", "notes.txt"),
              os.path.join(GOAT_ROOT, "workspace", "memory.md"),
              os.path.join(home, "Documents", "environment.md"),
              os.path.join(home, "Documents", "keys-to-success.md"),
              ""):
        check(f"not secret: {p!r}", not secret_paths.is_secret(p))

    tmp = tempfile.mkdtemp()
    fake_env = os.path.join(tmp, ".env")
    with open(fake_env, "w", encoding="utf-8") as f:
        f.write("API_KEY=sk-not-real")
    normal = os.path.join(tmp, "plan.md")
    with open(normal, "w", encoding="utf-8") as f:
        f.write("the barbershop plan")

    # --- read / list / open / write refuse secrets
    r = vh.read_file(fake_env)
    check("read_file refuses a .env", r == secret_paths.REFUSAL and "sk-" not in r)
    r = vh.read_file(os.path.join(GOAT_ROOT, ".goat-secrets.json"))
    check("read_file refuses .goat-secrets.json", r == secret_paths.REFUSAL)
    check("read_file still reads a normal doc", vh.read_file(normal) == "the barbershop plan")
    check("list_folder refuses .phone",
          vh.list_folder(os.path.join(GOAT_ROOT, ".phone")) == secret_paths.REFUSAL)
    check("list_folder still lists a normal folder", "plan.md" in vh.list_folder(tmp))

    opened = []
    real_startfile = os.startfile
    os.startfile = lambda p, *a, **k: opened.append(p)
    try:
        check("open_path refuses a secret", vh.open_path(fake_env) == secret_paths.REFUSAL)
        for ext in (".lnk", ".url", ".cpl", ".msc", ".scf", ".py", ".settingcontent-ms"):
            p = os.path.join(tmp, "x" + ext)
            open(p, "w").close()
            check(f"open_path refuses {ext}", vh.open_path(p).startswith("ERROR"))
        check("nothing launched", opened == [])
        check("open_path still opens a doc", vh.open_path(normal).startswith("opened")
              and opened == [normal])
    finally:
        os.startfile = real_startfile

    # the temp folder lives under AppData, which desk_hands refuses on its
    # own; lift only that rule so the SECRET rule is what's being tested
    no_write, desk_hands._NO_WRITE = desk_hands._NO_WRITE, ()
    try:
        r = desk_hands.write_file({"path": os.path.join(tmp, ".goat-secrets.json"),
                                   "content": "x", "mode": "append", "open": False})
        check("write_file refuses a secret name", r == secret_paths.REFUSAL, r)
        r = desk_hands.write_file({"path": os.path.join(tmp, "ok.txt"), "content": "hi",
                                   "open": False})
        check("write_file still writes a note", r.startswith("wrote"), r)
    finally:
        desk_hands._NO_WRITE = no_write

    # --- find_files never lists a secret
    vh._index[:] = [(fake_env.lower(), fake_env, 0.0), (normal.lower(), normal, 0.0)]
    vh._index_t = 1e18
    out = vh.find_files("env")
    check("find_files hides secret hits", fake_env not in out, out)
    out = vh.find_files("plan")
    check("find_files still finds docs", normal in out, out)

    # --- the terminal Claude message never goes through a shell
    calls = []

    class FakePopen:
        def __init__(self, args, **kw):
            calls.append((args, kw))
    real_popen, real_exe = subprocess.Popen, vh._claude_exe
    subprocess.Popen = FakePopen
    vh._claude_exe = lambda: r"C:\fake\claude.exe"
    try:
        evil = 'hi" & calc.exe & "'
        r = vh._resume([{"status": "idle", "sessionId": "abc", "cwd": tmp}], evil)
        args, kw = calls[-1] if calls else ([], {})
        check("resume: no shell", calls and not kw.get("shell"))
        check("resume: his words stay ONE argument", args[-1] == evil and len(args) == 5)
        vh._claude_exe = lambda: None
        r = vh._resume([{"status": "idle", "sessionId": "abc", "cwd": tmp}], "x")
        check("resume: no claude.exe -> refuses, starts nothing",
              r.startswith("ERROR") and len(calls) == 1)
    finally:
        subprocess.Popen, vh._claude_exe = real_popen, real_exe
    exe = vh._claude_exe()
    check("claude resolves to a real .exe here", exe is None or exe.lower().endswith(".exe"))

    print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
