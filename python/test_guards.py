"""GOAT_GUARDS: on asks as before, off drops the confirm-first gates but keeps
private files, none drops those too. No SDK, no model, no window touched.
Run: py -3.13 test_guards.py"""
import asyncio
import os
import sys

import close_guard
import goat_app as g
import guards
import screen_tools
import task_router
import voice_hands
from goat_paths import GOAT_ROOT

fails = []
SECRETS = os.path.join(GOAT_ROOT, ".goat-secrets.json")
DELETE = {"command": "Remove-Item -Recurse C:\\some\\folder"}


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  - {info}" if info and not ok else ""))
    if not ok:
        fails.append(name)


def denied(r):
    return (r.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


def shell(ti):
    r = asyncio.run(g._close_guard_hook({"tool_name": "PowerShell", "tool_input": ti}, "id", None))
    close_guard.clear()
    return denied(r)


def secret(ti):
    r = asyncio.run(g._secret_guard_hook({"tool_name": "Read", "tool_input": ti}, "id", None))
    close_guard.clear()
    return denied(r)


def screen_gate():
    r = screen_tools._gate("click", {"x": 5, "y": 5, "label": "Delete account"})
    close_guard.clear()
    return r is not None


def job():
    r = {"task_summary": "delete the old backups", "needs_confirmation": True}
    return "ask Giorgi to confirm" in task_router.job_text(r, "delete the old backups")


def main() -> int:
    for raw, want in (("", "on"), ("on", "on"), ("OFF", "off"), (" none ", "none"),
                      ("0", "on"), ("false", "on"), ("nonsense", "on")):
        os.environ["GOAT_GUARDS"] = raw
        check(f"level({raw!r}) == {want}", guards.level() == want, guards.level())
    os.environ.pop("GOAT_GUARDS", None)
    check("unset means on", guards.level() == "on" and guards.asks() and guards.secrets())

    # (level, confirm-first gates ask, private files protected)
    for lvl, asks, private in (("on", True, True), ("off", False, True), ("none", False, False)):
        os.environ["GOAT_GUARDS"] = lvl
        check(f"{lvl}: destructive shell command {'held' if asks else 'runs'}",
              shell(DELETE) is asks)
        check(f"{lvl}: harmless shell command runs", shell({"command": "Get-ChildItem"}) is False)
        check(f"{lvl}: risky screen click {'held' if asks else 'runs'}", screen_gate() is asks)
        check(f"{lvl}: confirm line {'in' if asks else 'not in'} a destructive job", job() is asks)
        check(f"{lvl}: worker read of a private file {'held' if private else 'runs'}",
              secret({"file_path": SECRETS}) is private)
        check(f"{lvl}: voice read of a private file {'refused' if private else 'allowed'}",
              voice_hands._private(SECRETS) is private)
        check(f"{lvl}: ordinary file is never private",
              voice_hands._private(os.path.join(GOAT_ROOT, "README.md")) is False)
    os.environ.pop("GOAT_GUARDS", None)

    # The prompts follow the switch (2026-11-15): the brains must not be told
    # to ask first while the gates themselves no longer ask.
    import re
    worker = g.WORKER_BASE_PROMPT + "\n" + g.WORKER_PERSONA
    voice = g.VOICE_BASE_PROMPT + "\n" + g.PERSONA
    os.environ["GOAT_GUARDS"] = "on"
    import importlib
    desc_on = importlib.reload(screen_tools).COMPUTER_DESC
    everything = worker + voice + desc_on
    check("on: prompts are untouched", g.guards.prompt(worker) == worker
          and g.guards.prompt(voice) == voice)
    for pattern, _new in guards.PROMPT_RULES:
        check(f"prompt rule still finds its sentence: {pattern[:40]!r}",
              re.search(pattern, everything, flags=re.DOTALL) is not None)
    ask_first = re.compile(r"explicit yes|confirmation line first|SAFETY GATE|"
                           r"ask only before|asking for\s+confirmation:")
    check("on: the ask-first wording is there", len(ask_first.findall(everything)) >= 5)
    for lvl in ("off", "none"):
        os.environ["GOAT_GUARDS"] = lvl
        out = guards.prompt(worker) + guards.prompt(voice) + guards.prompt(desc_on)
        check(f"{lvl}: no ask-first wording left in the prompts",
              not ask_first.search(out), ask_first.findall(out))
        check(f"{lvl}: the prompt says his order is enough",
              "NO GATE" in out and "without asking" in out)
        check(f"{lvl}: strangers' text is still not an order",
              "never counts as an order" in out)
        check(f"{lvl}: a garbled order is still checked",
              "garbled/context-free" in out)
        check(f"{lvl}: private-file line {'kept' if lvl == 'off' else 'dropped'}",
              ("key, login or token files" in out) is (lvl == "off"))
    os.environ.pop("GOAT_GUARDS", None)

    print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
