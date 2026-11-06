"""One command for GOAT's offline test suites.

    py -3.13 run_tests.py            # the default set: no audio, no network,
                                     # no API cost, no windows, no input
    py -3.13 run_tests.py --qt       # + the Qt window tests (open windows)
    py -3.13 run_tests.py --slow     # + test_dsp (~4 min)
    py -3.13 run_tests.py --live     # + test_screen (moves GOAT's live window)
    py -3.13 run_tests.py name ...   # only these (e.g. phone_server reflex)

Each suite runs as its own process (they are scripts, not pytest), with a
timeout. Exit code 0 only if every suite exits 0.

Deliberately NOT here: test_voice_e2e (real ear + real actions), test_voice_
knowledge (real Gemini Live), test_audio_stall / test_aec (real mic and
speaker), test_screen_live / test_live_eyes (touch the desktop), probe_*.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT = [
    "self_check",           # preflight: compile + import gate
    "test_metrics",
    "test_engine_router",
    "test_two_brains",
    "test_acceptance_handoff",
    "test_brain_route",
    "test_task_router",
    "test_action_policy",
    "test_phone_server",
    "test_phone_cut",
    "test_voice_latency",
    "test_voice_hands",
    "test_voice_hands_safety",
    "test_secret_guard",
    "test_desk_hands",
    "test_live_talk",
    "test_stt_live",
    "test_stt_local_stream",
    "test_stt_outage_status",
    "test_stale_voice",
    "test_side_talk",
    "test_reflex",
    "test_reveal",
    "test_echo_rescue",
    "test_barge_gaps",
    "test_barge_doubletalk",
    "test_barge_acoustic",
    "test_barge_room",
    "test_audio_resilience",
    "test_voice_id",
    "test_child_guard",
    "test_self_update",
    "test_self_awareness",
    "test_sight_games",
    "test_game_mind",
    "test_trade_hands",
    "test_watchers",
]
QT = ["test_bubble", "test_presence", "test_palette", "test_scroll",
      "test_statusword", "test_message_pop", "test_home_desk"]
SLOW = ["test_dsp"]            # ~230 s of filter maths
# Moves GOAT's own live window (stand_aside / hide_self). Its
# "stand_aside drops always-on-top" check already fails on 248740c (AUDIT B6).
LIVE = ["test_screen"]
TIMEOUT_S = 240


def run(name: str) -> tuple:
    t0 = time.monotonic()
    try:
        p = subprocess.run([sys.executable, "-u", name + ".py"], cwd=HERE,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TIMEOUT_S)
        code, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        code, out = -9, f"timed out after {TIMEOUT_S}s"
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:] or [""]
    return code, time.monotonic() - t0, tail[0][:110], out


def main(argv: list) -> int:
    names = [a for a in argv[1:] if not a.startswith("--")]
    suites = ([n if n.startswith(("test_", "self_")) else "test_" + n for n in names]
              or DEFAULT + (QT if "--qt" in argv else [])
              + (SLOW if "--slow" in argv else [])
              + (LIVE if "--live" in argv else []))
    bad = []
    t_all = time.monotonic()
    for name in suites:
        code, dt, tail, out = run(name)
        print(f"{'ok  ' if code == 0 else 'FAIL'} {name:<26} {dt:6.1f}s  {tail}", flush=True)
        if code != 0:
            bad.append((name, out))
    print(f"\n{len(suites) - len(bad)}/{len(suites)} suites passed "
          f"in {time.monotonic() - t_all:.0f}s")
    for name, out in bad:
        print(f"\n===== {name} (last 25 lines) =====")
        print("\n".join(out.strip().splitlines()[-25:]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
