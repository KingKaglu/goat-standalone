"""General-knowledge questions through GOAT's real Live voice path (his order
2026-11-05): the voice must answer at once, itself, in 1-3 spoken sentences —
no hand-off, no "let me check", no question back, no silence.

Each question goes through task_router (a BIG route = a hand-off before the
voice even hears it), then into a real Gemini Live session with GOAT's full
prompt, memory and tools, the same way goat_app sends a turn. Logged per
question: time to first audio, tools called, the spoken transcript, and
checks (answered itself, closing question, Georgian script in English, a
keyword the right answer contains).

Live is free on his key; nothing here touches the REST voice's daily quota.

Run:  py -3.13 test_voice_knowledge.py [--out FILE.json] [--compare BASE.json]
          [--memory-cap N] [--code DIR] [--n N]
  --code DIR       run another checkout's talk_brain/live_talk/task_router
                   (the baseline: a worktree of the commit before the fix)
  --strip-think    drop thinkingConfig at setup (for old code that sends one
                   this model refuses)
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import statistics
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))

# (question, regex the right answer contains)
QUESTIONS = [
    ("how many Georgian lari is 434 euros", r"1[,.]?[23]\d\d|thousand|lari"),
    ("convert 10 miles to kilometers", r"16"),
    ("what is the speed of light", r"300|299|186"),
    ("who was Napoleon", r"French|France|emperor"),
    ("how far is the moon", r"38\d|384|238|three hundred eighty|two hundred thirty"),
    ("why does the sky look blue", r"scatter"),
    ("how does a turbocharger work", r"exhaust"),
    ("what's the capital of Australia", r"Canberra"),
    ("what is 15 percent of 240", r"\b36\b|thirty-six"),
    ("how do you say thank you in German", r"[Dd]anke"),
    ("when did World War 2 end", r"1945|nineteen forty-five"),
    ("what's the tallest mountain in Georgia", r"Shkhara"),
    ("how many kilometers is a marathon", r"42|forty-two"),
    ("who wrote Hamlet", r"Shakespeare"),
    ("what's the boiling point of water in fahrenheit", r"212|two hundred (?:and )?twelve"),
    ("how many people live in Germany", r"8\d|eighty"),
    ("what is photosynthesis", r"light|sun"),
    ("how long should I boil an egg", r"minute"),
    ("what's the square root of 144", r"\b12\b|twelve"),
    ("how much is 100 dollars in euros", r"euro"),
    ("what's the difference between a virus and bacteria", r"cell|living|antibiotic"),
    ("how many calories are in a banana", r"\d|calorie"),
    ("who was Shota Rustaveli", r"poet|Panther|Knight"),
    ("how fast can a cheetah run", r"\d|sixty|seventy|hundred"),
    ("what's the population of Tbilisi", r"million|\d"),
    ("how does a vaccine work", r"immun|antibod"),
    ("what currency does Germany use", r"[Ee]uro"),
    ("why is the ocean salty", r"rock|mineral|river"),
    ("what is 7 times 8", r"56|fifty-six"),
    ("how far is Tbilisi from Berlin", r"\d|hundred|thousand"),
    ("compare the BMW M3 and the Audi RS4", r"M3|RS ?4"),
]

HANDOFF = {"hand_to_worker", "message_worker"}
GEORGIAN = re.compile("[Ⴀ-ჿ]")
LETS_CHECK = re.compile(r"\b(?:let me (?:check|look|search|find)|one moment|checking)\b", re.I)


def load(code_dir: str):
    sys.path.insert(0, code_dir)
    os.chdir(code_dir)
    import talk_brain, live_talk, task_router  # noqa: E401
    return talk_brain, live_talk, task_router


async def run(args):
    tb, lt, tr = load(args.code)
    import voice_hands
    if args.memory_cap is not None:
        tb.MEMORY_CAP = args.memory_cap
    talker = tb.GeminiTalk()
    talker.history.clear()            # each run starts from the same place
    live = lt.LiveTalk(talker)
    if args.strip_think:
        orig = live._setup

        def no_think():
            s = orig()
            s["setup"]["generationConfig"].pop("thinkingConfig", None)
            return s
        live._setup = no_think

    async def runner(name, a):
        if name not in voice_hands.NAMES:
            return None
        return await asyncio.to_thread(voice_hands.execute, name, a)
    live.tool_runner = runner
    print(f"code {args.code}\nmodel {live.model}, system prompt {len(talker._system)} chars "
          f"(memory cap {tb.MEMORY_CAP})\n")

    rows = []
    qs = QUESTIONS[:args.n] if args.n else QUESTIONS
    for q, want in qs:
        r = tr.route(q)
        row = {"q": q, "route": f"{r['type']}/{r['size']}", "ttfa": None, "first": None,
               "tools": [], "text": "", "ok": False, "error": ""}
        if r["size"] == tr.BIG:
            row["error"] = "router: handed to Claude"
            rows.append(row)
            print(f"  HANDOFF  {q}")
            continue
        words, t0 = [], time.monotonic()
        audio_t = []

        def on_audio(pcm):
            if not audio_t:
                audio_t.append(time.monotonic() - t0)

        parts = [{"text": tb.clock_note() + "\n\nGiorgi: " + q}]
        try:
            res = await asyncio.wait_for(
                live.reply(parts, words.append, lambda n, a: row["tools"].append(n), on_audio),
                45)
        except asyncio.TimeoutError:
            res = {"ok": False, "error": "harness timeout 45s", "ttft": None}
        live._down_until = 0.0         # measure every question, no resting
        live.fail_streak = 0
        said = " ".join("".join(words).split())
        row.update(ttfa=audio_t[0] if audio_t else None, first=res.get("ttft"),
                   text=said, ok=bool(res.get("ok")), error=res.get("error") or "")
        talker.remember(q, said)
        row["itself"] = bool(row["ok"] and said and not (set(row["tools"]) & HANDOFF))
        row["no_tool"] = not row["tools"]
        row["question_back"] = said.rstrip().endswith("?")
        row["georgian"] = bool(GEORGIAN.search(said))
        row["lets_check"] = bool(LETS_CHECK.search(said))
        row["right"] = bool(re.search(want, said, re.I))
        row["sentences"] = len([s for s in re.split(r"(?<=[.!?])\s+", said) if s.strip()])
        rows.append(row)
        flag = "".join([" HANDOFF" if set(row["tools"]) & HANDOFF else "",
                        " SILENT" if not said else "", " Q?" if row["question_back"] else "",
                        " KA" if row["georgian"] else "", " WRONG?" if said and not row["right"] else "",
                        f" tools={','.join(row['tools'])}" if row["tools"] else "",
                        f" err={row['error']}" if row["error"] else ""])
        ta = f"{row['ttfa']:.2f}s" if row["ttfa"] is not None else "  -  "
        print(f"  {ta}  {q}{flag}\n          {said[:160]!r}")
    return rows


def summary(rows: list) -> dict:
    t = sorted(r["ttfa"] for r in rows if r["ttfa"] is not None)
    n = len(rows)
    return {
        "n": n,
        "answered_itself": sum(1 for r in rows if r.get("itself")),
        "no_tool": sum(1 for r in rows if r.get("itself") and r.get("no_tool")),
        "silent": sum(1 for r in rows if not r["text"] and not r["error"].startswith("router")),
        "handoff": sum(1 for r in rows if r["error"].startswith("router")
                       or set(r["tools"]) & HANDOFF),
        "question_back": sum(1 for r in rows if r.get("question_back")),
        "georgian_in_english": sum(1 for r in rows if r.get("georgian")),
        "wrong_or_missing": [r["q"] for r in rows if r["text"] and not r.get("right")],
        "ttfa_median": round(statistics.median(t), 2) if t else None,
        "ttfa_worst": round(t[-1], 2) if t else None,
        "over_4s": sum(1 for x in t if x > 4.0),
        "max_sentences": max((r.get("sentences", 0) for r in rows), default=0),
    }


def show(s: dict, title: str):
    print(f"\n== {title} ==")
    for k, v in s.items():
        print(f"  {k:20} {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--compare", default="")
    ap.add_argument("--memory-cap", type=int, default=None)
    ap.add_argument("--code", default=HERE)
    ap.add_argument("--strip-think", action="store_true")
    ap.add_argument("--n", type=int, default=0)
    args = ap.parse_args()
    args.code = os.path.abspath(args.code)
    out = os.path.abspath(args.out) if args.out else ""
    base = os.path.abspath(args.compare) if args.compare else ""
    rows = asyncio.run(run(args))
    s = summary(rows)
    show(s, "this run")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"at": time.strftime("%Y-%m-%d %H:%M"), "code": args.code,
                       "memory_cap": args.memory_cap, "summary": s, "rows": rows},
                      f, ensure_ascii=False, indent=1)
        print(f"\nsaved {out}")
    if base:
        with open(base, encoding="utf-8") as f:
            show(json.load(f)["summary"], f"baseline {base}")
    # pass = every question answered by the voice itself, out loud, no
    # hand-off, no question back, no Georgian script, nothing past 4 s
    good = (s["answered_itself"] == s["n"] and not s["question_back"]
            and not s["georgian_in_english"] and not s["over_4s"])
    print("\nVOICE KNOWLEDGE", "PASS" if good else "FAIL")
    raise SystemExit(0 if good else 1)


if __name__ == "__main__":
    main()
