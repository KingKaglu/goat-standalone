"""Debug metrics: one JSON line per event, only when GOAT_METRICS=on.

Off (the default) costs one bool check per call site. On, events append to
python/metrics.jsonl (gitignored). Nothing here may ever raise into the app.

Events (all carry "t" = unix time):
  latency      total, endpoint, ear, voice, filler, token, ear_kind   (ms)
  answer_audio ms            first ANSWER audio (not filler, not "Sir,")
  endpoint     hang_ms, state   state = why the hangover was chosen
  heard        gap, merged, held, answering, words
  drop         reason        a clip that never became a turn
  barge        what          possible / confirmed / echo, not you / ...
  claude_usage model, input, cache_read, cache_create, output
  ear          ms, mode      laptop ear: pause -> transcript; pinned / auto

  py -3.13 metrics.py summary [file]   -> the numbers PLAN.md tracks
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

ON = os.environ.get("GOAT_METRICS", "off").strip().lower() in ("on", "1", "true", "yes")
PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.jsonl")
_lock = threading.Lock()


def emit(kind: str, **fields) -> None:
    if not ON:
        return
    try:
        rec = {"t": round(time.time(), 3), "kind": kind}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _lock, open(PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — metrics must never cost a turn
        pass


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

BARGE_WINDOW_S = 4.0   # a confirmed barge with no turn of his inside this = false
SPLIT_GAP_S = 3.0      # his next turn this soon, not merged = a split candidate
SPLIT_PREV_S = 8.0     # ...and his previous turn was heard at most this long before


def _pct(xs: list, p: float):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


def load(path: str = PATH) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def summarize(events: list) -> dict:
    ev = sorted(events, key=lambda e: e.get("t", 0))
    lat = [e for e in ev if e["kind"] == "latency"]
    s: dict = {"turns": len(lat)}
    for key in ("total", "endpoint", "ear", "voice"):
        xs = [float(e[key]) for e in lat if isinstance(e.get(key), (int, float)) and e[key] >= 0]
        s[key] = {"p50": _pct(xs, 0.5), "p90": _pct(xs, 0.9), "n": len(xs)}
    for mode in ("pinned", "auto"):     # the ear's own job time (stt_whisper)
        xs = [float(e["ms"]) for e in ev if e["kind"] == "ear" and e.get("mode") == mode]
        s[f"ear_job_{mode}"] = {"p50": _pct(xs, 0.5), "p90": _pct(xs, 0.9), "n": len(xs)}
    ans = [float(e["ms"]) for e in ev if e["kind"] == "answer_audio"]
    s["ttfw"] = {"p50": _pct(ans, 0.5), "p90": _pct(ans, 0.9), "n": len(ans)}
    s["fillers"] = sum(1 for e in lat if (e.get("filler") or -1) >= 0)

    eps = [e for e in ev if e["kind"] == "endpoint"]
    states: dict = {}
    for e in eps:
        states[str(e.get("state"))] = states.get(str(e.get("state")), 0) + 1
    s["endpoint_long"] = sum(1 for e in eps if (e.get("hang_ms") or 0) >= 1000)
    s["endpoint_n"] = len(eps)
    s["endpoint_states"] = states

    jobs = [e for e in ev if e["kind"] == "claude_usage"]
    tot = {k: sum(int(e.get(k) or 0) for e in jobs)
           for k in ("input", "cache_read", "cache_create", "output")}
    denom = tot["input"] + tot["cache_read"] + tot["cache_create"]
    s["claude"] = {"jobs": len(jobs), **tot,
                   "per_job_in": round(denom / len(jobs)) if jobs else None,
                   "cache_hit": round(tot["cache_read"] / denom, 3) if denom else None}

    heard = [e for e in ev if e["kind"] == "heard"]
    drops = [e for e in ev if e["kind"] == "drop"]
    confirmed = [e for e in ev if e["kind"] == "barge"
                 and str(e.get("what", "")).startswith("confirmed")]
    false_b = 0
    for b in confirmed:
        t = b["t"]
        nxt_h = next((h for h in heard if t <= h["t"] <= t + BARGE_WINDOW_S), None)
        nxt_d = next((d for d in drops if t <= d["t"] <= t + BARGE_WINDOW_S), None)
        if nxt_h is None or (nxt_d is not None and nxt_d["t"] <= nxt_h["t"]):
            false_b += 1
    s["barge"] = {"confirmed": len(confirmed), "false": false_b,
                  "echo_ducks": sum(1 for e in ev if e["kind"] == "barge"
                                    and str(e.get("what", "")).startswith("echo, not you"))}
    # A split = his turn starting soon after his previous HEARD turn, not
    # merged into it. The gap alone counts any earlier clip, even one that
    # was dropped as noise, so the previous turn must really exist.
    splits = 0
    for i, h in enumerate(heard):
        g = h.get("gap")
        if g is None or g > SPLIT_GAP_S or h.get("merged") or h.get("held") or i == 0:
            continue
        prev = heard[i - 1]
        if h["t"] - prev["t"] <= g + SPLIT_PREV_S and not prev.get("held"):
            splits += 1
    s["splits"] = splits
    s["merged"] = sum(1 for h in heard if h.get("merged"))
    s["drops"] = len(drops)
    return s


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] != "summary":
        print(__doc__)
        return 2
    path = argv[2] if len(argv) > 2 else PATH
    print(json.dumps(summarize(load(path)), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
