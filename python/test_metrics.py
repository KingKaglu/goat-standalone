"""metrics.py: silent when off, one line per event when on, and the summary
counts what PLAN.md tracks (latency percentiles, endpoint states, cache hit,
false barge-ins, split utterances). No audio, no network.
Run: py -3.13 test_metrics.py"""
import os
import sys
import tempfile

import metrics

fails = []


def check(name, ok):
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)


def main() -> int:
    tmp = tempfile.mkdtemp()
    metrics.PATH = os.path.join(tmp, "m.jsonl")

    metrics.ON = False
    metrics.emit("latency", total=1)
    check("off writes nothing", not os.path.exists(metrics.PATH))

    metrics.ON = True
    metrics.emit("latency", total=2500, endpoint=1200, ear=600, voice=700,
                 filler=-1, token=-1, ear_kind="streaming")
    metrics.emit("bad", value=object())          # unserialisable -> str, no raise
    check("on writes lines", len(metrics.load(metrics.PATH)) == 2)

    t = 1000.0
    ev = [
        {"t": t, "kind": "latency", "total": 2000, "endpoint": 1200, "ear": 400,
         "voice": 400, "filler": -1},
        {"t": t + 1, "kind": "latency", "total": 3000, "endpoint": 500, "ear": 500,
         "voice": 2000, "filler": 1500},
        {"t": t, "kind": "answer_audio", "ms": 2100},
        {"t": t, "kind": "endpoint", "hang_ms": 1216, "state": "local-pending"},
        {"t": t, "kind": "endpoint", "hang_ms": 500, "state": "local-True"},
        {"t": t, "kind": "claude_usage", "input": 10, "cache_read": 80,
         "cache_create": 10, "output": 5},
        # real barge: his words follow within 4 s
        {"t": t + 10, "kind": "barge", "what": "confirmed interrupt"},
        {"t": t + 11, "kind": "heard", "gap": 5.0, "merged": False, "held": False},
        # false barge: nothing of his follows
        {"t": t + 20, "kind": "barge", "what": "confirmed interrupt"},
        # false barge: the next clip was dropped as echo
        {"t": t + 30, "kind": "barge", "what": "confirmed interrupt"},
        {"t": t + 31, "kind": "drop", "reason": "cut-in-echo"},
        {"t": t + 40, "kind": "barge", "what": "echo, not you"},
        # not a split: 1.2 s after a clip that never became a turn
        {"t": t + 45, "kind": "heard", "gap": 1.2, "merged": False, "held": False},
        # split: his previous turn was heard 2 s before, 1.2 s gap, not merged
        {"t": t + 48, "kind": "heard", "gap": 9.0, "merged": False, "held": False},
        {"t": t + 50, "kind": "heard", "gap": 1.2, "merged": False, "held": False},
        # merged: not a split
        {"t": t + 60, "kind": "heard", "gap": 0.8, "merged": True, "held": False},
    ]
    s = metrics.summarize(ev)
    check("turns counted", s["turns"] == 2)
    check("total p50", s["total"]["p50"] == 2500.0)
    check("ttfw p50", s["ttfw"]["p50"] == 2100.0)
    check("fillers counted", s["fillers"] == 1)
    check("long endpoints", s["endpoint_long"] == 1 and s["endpoint_n"] == 2)
    check("endpoint states", s["endpoint_states"].get("local-pending") == 1)
    check("cache hit", s["claude"]["cache_hit"] == 0.8)
    check("per-job input", s["claude"]["per_job_in"] == 100)
    check("barges", s["barge"] == {"confirmed": 3, "false": 2, "echo_ducks": 1})
    check("splits", s["splits"] == 1 and s["merged"] == 1)
    check("empty summary safe", metrics.summarize([])["claude"]["cache_hit"] is None)

    print(f"\n{14 - len(fails)} passed, {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
