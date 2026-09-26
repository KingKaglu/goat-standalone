"""Self-update guard tests — the 2026-09-27 deafness must never repeat.

That day claude-agent-sdk 0.2.160 shipped with no win_amd64 wheel; pip built
it from the sdist (no bundled claude.exe), the SDK fell back to npm's
claude.CMD, refused it, the engine died on connect and GOAT heard nothing.

No pip, no network: PyPI, pip and the engine self-test are all stubs, and the
state files live in a temp dir.

Run:  python test_self_update.py   (prints PASS/FAIL per case)
"""
import io
import json
import os
import tempfile

import self_update as su

PASS = FAIL = 0


def check(name, ok):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")


def fake_pypi(latest, files_by_version):
    def urlopen(url, timeout=None):
        if url.endswith(f"/{su.SDK}/json"):
            body = {"info": {"version": latest}}
        else:
            ver = url.rstrip("/").split("/")[-2]
            body = {"urls": [{"filename": f} for f in files_by_version[ver]]}
        return io.BytesIO(json.dumps(body).encode())
    return urlopen


class FakeEnv:
    """pip + installed SDK state. `broken` versions install fine but can't run
    (no bundled exe) — the 0.2.160 shape."""
    def __init__(self, installed, broken=()):
        self.installed = installed
        self.broken = set(broken)
        self.pip_calls = []

    def pip_install(self, version):
        self.pip_calls.append(version)
        self.installed = version
        return 0, "ok"

    def works(self):
        return self.installed not in self.broken


def setup(tmp, env):
    for name in ("STATE", "PENDING", "LAST", "NOTICE", "ENGINE_UP"):
        setattr(su, name, os.path.join(tmp, os.path.basename(getattr(su, name))))
    su.sdk_installed = lambda: env.installed
    su._pip_install = env.pip_install
    su.engine_works = env.works
    su._check_cli = lambda: None
    su._check_models = lambda: []


WIN = "claude_agent_sdk-{v}-py3-none-win_amd64.whl"
MAC = "claude_agent_sdk-{v}-py3-none-macosx_11_0_arm64.whl"
SDIST = "claude_agent_sdk-{v}.tar.gz"

with tempfile.TemporaryDirectory() as tmp:
    # ---- check(): no Windows wheel -> never queued (the exact 0.2.160 case)
    env = FakeEnv("0.2.159")
    setup(tmp, env)
    su.urllib.request.urlopen = fake_pypi("0.2.160", {
        "0.2.160": [MAC.format(v="0.2.160"), SDIST.format(v="0.2.160")]})
    log = su.check()
    check("no win_amd64 wheel: update NOT queued", su.pending() is None)
    check("no win_amd64 wheel: logged as skipped",
          any("skipped" in ln for ln in log))

    # ---- check(): Windows wheel present -> queued
    su.urllib.request.urlopen = fake_pypi("0.2.161", {
        "0.2.161": [WIN.format(v="0.2.161"), SDIST.format(v="0.2.161")]})
    su.check()
    check("win_amd64 wheel present: update queued",
          su.pending() == {"from": "0.2.159", "to": "0.2.161"})

    # ---- install(): good engine -> kept, LAST written for the watchdog
    rc = su.install()
    check("good install: exit 0 and new version kept",
          rc == 0 and env.installed == "0.2.161")
    check("good install: LAST recorded for boot rollback",
          su._load(su.LAST) == {"from": "0.2.159", "to": "0.2.161"})
    os.remove(su.LAST)
    su.take_notice()

    # ---- install(): installs but engine can't run -> rolled back at once
    env = FakeEnv("0.2.159", broken={"0.2.162"})
    setup(tmp, env)
    su._save(su.PENDING, {"from": "0.2.159", "to": "0.2.162"})
    rc = su.install()
    check("broken engine: install reports failure", rc == 1)
    check("broken engine: previous version reinstalled before relaunch",
          env.installed == "0.2.159" and env.pip_calls == ["0.2.162", "0.2.159"])
    check("broken engine: no LAST left behind", su._load(su.LAST) is None)
    check("broken engine: he is told it was kept",
          "kept 0.2.159" in su.take_notice())

    # ---- revert(): watchdog path puts the old engine back
    env = FakeEnv("0.2.163")
    setup(tmp, env)
    su._save(su.LAST, {"from": "0.2.159", "to": "0.2.163"})
    su.revert()
    check("revert: previous engine reinstalled", env.installed == "0.2.159")
    su.take_notice()

    # ---- engine marker for the restart watchdog
    su.mark_engine_up()
    check("mark_engine_up writes the watchdog marker", os.path.exists(su.ENGINE_UP))

# ---- the real pip call must refuse source builds
import importlib  # noqa: E402
importlib.reload(su)   # undo the stubs above
calls = []
su._run = lambda args, timeout=300: (calls.append(args), (0, ""))[1]
su._pip_install("0.2.159")
check("pip install uses --only-binary=:all:",
      calls and "--only-binary=:all:" in calls[-1])

# ---- live: the engine installed on this PC right now can run
importlib.reload(su)
check("LIVE: installed engine's bundled claude.exe runs", su.engine_works())

print(f"\n{PASS} passed, {FAIL} failed")
raise SystemExit(1 if FAIL else 0)
