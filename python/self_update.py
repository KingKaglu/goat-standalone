"""Daily self-update (his order 2026-09-27: "check once in 24 hours, don't
tell me about it — if there's an update, just update and tell me you did").

What gets checked, once per 24h, from the running app (goat_app._update_watch):
  engine  — claude-agent-sdk on Python 3.13. It bundles the Claude CLI I run
            on, and a new model id only works once the bundled CLI knows it
            (Opus 5.5 needed 0.2.159). The bundled claude.exe is LOCKED while
            I'm alive, so a found update is only marked pending here; it is
            installed by restart-goat.ps1 in the gap between kill and relaunch.
  cli     — the global Claude Code CLI (npm). Updated in place by its own
            updater; it isn't what I run on, so no restart needed.
  models  — Anthropic's model list. A newer Opus/Sonnet/Fable than the roster
            in goat_app gets probed on his account; if it answers, he's told
            (switching brains changes labels + billing, so that stays his call).

  python self_update.py check    -> run the check now (ignores the 24h clock)
  python self_update.py install  -> restart-goat.ps1 only, while GOAT is down
  python self_update.py revert   -> restart-goat.ps1 watchdog: boot died after
                                    an install, put the previous engine back
"""
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request  # PyPI JSON only

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, ".update-state.json")      # last check time
PENDING = os.path.join(HERE, ".update-pending.json")  # engine update to install
LAST = os.path.join(HERE, ".update-last.json")        # what install changed
NOTICE = os.path.join(HERE, "update-notice.txt")      # told on his next turn

PY313 = r"C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe"
SDK = "claude-agent-sdk"
MODELS_URL = "https://docs.claude.com/en/docs/about-claude/models/overview"
EVERY_H = 24.0
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)


def _run(args, timeout=300):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


def notify(line):
    """Queue one line for GOAT to tell him on his next turn."""
    with open(NOTICE, "a", encoding="utf-8") as f:
        f.write(line.strip() + "\n")


def take_notice():
    """Read and clear the queued lines ('' when there's nothing)."""
    try:
        with open(NOTICE, encoding="utf-8") as f:
            text = f.read().strip()
        os.remove(NOTICE)
        return text
    except OSError:
        return ""


def due():
    last = (_load(STATE, {}) or {}).get("checked", 0)
    return datetime.datetime.now().timestamp() - last >= EVERY_H * 3600


def pending():
    return _load(PENDING)


def sdk_installed():
    code, out = _run([PY313, "-m", "pip", "show", SDK], 60)
    m = re.search(r"^Version:\s*(\S+)", out, re.M)
    return m.group(1) if m else None


def sdk_latest():
    with urllib.request.urlopen(f"https://pypi.org/pypi/{SDK}/json",
                                timeout=20) as r:
        return json.load(r)["info"]["version"]


def _cli_version():
    code, out = _run(["cmd", "/c", "claude", "--version"], 60)
    m = re.search(r"\d+\.\d+\.\d+", out)
    return m.group(0) if m else None


def _check_cli():
    before = _cli_version()
    if not before:
        return None
    _run(["cmd", "/c", "claude", "update"], 300)
    after = _cli_version()
    if after and _vtuple(after) > _vtuple(before):
        return f"Claude Code CLI {before} -> {after}"
    return None


def _roster():
    """Model ids GOAT is configured with, read from goat_app's source (no
    import: that would drag in the whole app)."""
    with open(os.path.join(HERE, "goat_app.py"), encoding="utf-8") as f:
        src = f.read()
    return set(re.findall(r'^MODEL_\w+\s*=\s*"(claude-[\w-]+)"', src, re.M))


def _probe(model):
    """Does this model answer on his account? One tiny prompt through the
    bundled CLI — the same binary the app itself runs on."""
    import claude_agent_sdk  # noqa: PLC0415 — only on the 3.13 side
    exe = os.path.join(os.path.dirname(claude_agent_sdk.__file__),
                       "_bundled", "claude.exe")
    try:
        code, out = _run([exe, "-p", "Reply with only: OK", "--model", model],
                         120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return code == 0 and "OK" in out and "usage" not in out.lower()


def _check_models():
    # curl, not urllib: Python's cert store rejected docs.claude.com's chain
    # ("certificate has expired") on 2026-09-27 while curl/schannel was fine.
    code, page = _run(["curl.exe", "-sL", "--max-time", "30", MODELS_URL], 60)
    if code != 0 or "claude-" not in page:
        raise RuntimeError(f"model list fetch failed (curl exit {code})")
    ids = set(re.findall(
        r"claude-(?:opus|sonnet|fable)-\d+(?:-\d)?(?![\d-])", page))
    roster = _roster()
    seen = set((_load(STATE, {}) or {}).get("models_told", []))
    found = []
    for fam in ("opus", "sonnet", "fable"):
        mine = [m for m in roster if m.startswith(f"claude-{fam}-")]
        if not mine:
            continue
        best = max(mine, key=_vtuple)
        newer = sorted((i for i in ids if i.startswith(f"claude-{fam}-")
                        and _vtuple(i) > _vtuple(best) and i not in seen),
                       key=_vtuple)
        if newer and _probe(newer[-1]):
            found.append(newer[-1])
    return found


def check():
    """The daily sweep. Every part is independent — one failing (offline,
    site changed) never blocks the others."""
    state = _load(STATE, {}) or {}
    log = []
    try:
        have, latest = sdk_installed(), sdk_latest()
        if have and latest and _vtuple(latest) > _vtuple(have):
            _save(PENDING, {"from": have, "to": latest})
            log.append(f"engine {have} -> {latest} pending")
    except Exception as e:  # noqa: BLE001
        log.append(f"engine check failed: {e}")
    try:
        cli = _check_cli()
        if cli:
            notify(f"Updated {cli}.")
            log.append(cli)
    except Exception as e:  # noqa: BLE001
        log.append(f"cli check failed: {e}")
    try:
        for m in _check_models():
            notify(f"New model out and working on his account: {m} "
                   "(not switched — his call).")
            state.setdefault("models_told", []).append(m)
            log.append(f"new model {m}")
    except Exception as e:  # noqa: BLE001
        log.append(f"model check failed: {e}")
    state["checked"] = datetime.datetime.now().timestamp()
    state["last_result"] = log
    _save(STATE, state)
    return log


def install():
    """Only while GOAT is down (restart-goat.ps1): the bundled exe is free."""
    p = pending()
    if not p:
        return 0
    code, out = _run([PY313, "-m", "pip", "install", "--disable-pip-version-check",
                      f"{SDK}=={p['to']}"], 600)
    os.remove(PENDING)
    now = sdk_installed()
    if code == 0 and now == p["to"]:
        _save(LAST, p)
        notify(f"Updated my engine ({SDK}) {p['from']} -> {p['to']}.")
        return 0
    notify(f"Tried to update my engine to {p['to']} but pip failed; "
           f"still on {now}.")
    return 1


def boot_ok():
    """The fresh engine booted end to end — nothing left to revert."""
    try:
        os.remove(LAST)
    except OSError:
        pass


def revert():
    """Boot died after an install: put the previous engine back."""
    last = _load(LAST)
    if not last:
        return 0
    _run([PY313, "-m", "pip", "install", "--disable-pip-version-check",
          f"{SDK}=={last['from']}"], 600)
    os.remove(LAST)
    notify(f"Engine {last['to']} wouldn't boot, so I rolled back to "
           f"{last['from']}.")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        print("\n".join(check()) or "nothing new")
    elif cmd == "install":
        sys.exit(install())
    elif cmd == "revert":
        sys.exit(revert())
    else:
        sys.exit(f"unknown command: {cmd}")
