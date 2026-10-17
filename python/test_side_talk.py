"""Side talk gate, on REAL data: the call fragments from 2026-10-01 22:28
must stay silent; his own lines must almost never be dropped.

Run:  py -3.13 test_side_talk.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("GOAT_REFLEX", "on")
import goat_app as g        # noqa: E402 — WAKE_RE
import reflex               # noqa: E402
import side_talk as st      # noqa: E402

PASS = FAIL = 0


def check(name, ok, info=""):
    global PASS, FAIL
    PASS += bool(ok)
    FAIL += not ok
    print(("[PASS] " if ok else "[FAIL] ") + name + ("" if ok else f"  {info}"))


rm = lambda t: reflex.match(t, "en")  # noqa: E731
CALL = ["I'm not the longest relationship we're gonna have with each other.",
        "I honestly don't know what to say.",
        "There's anything I can do for you, just ask.",
        "Your faucets bigger than him.", "Your mother is...",
        "I'm not going to get a job.", "... Supposed to work. I-",
        "We gave you an opportunity to be responsible."]
for c in CALL:
    check(f"call fragment ignored in side mode: {c[:40]!r}",
          st.gate(c, False, g.WAKE_RE, rm) == "ignore")
    check(f"call fragment gets no filler: {c[:40]!r}", st.looks_aside(c, g.WAKE_RE))

# Real orders and questions to GOAT must get through even mid-call.
for s, want in [("Hey GOAT, what time is it?", "pass"), ("Good. Please restart yourself.", "pass"),
                ("Close this tab", "pass"), ("Did you push...", "pass"),
                ("What's on my screen?", "pass"), ("Of course, save every update.", "pass"),
                ("არა, არა Google-ი გახსენი", "pass"), ("Let's play some music.", "pass"),
                ("So everything's up to date?", "probe"),
                ("What do you mean by that?", "probe")]:
    got = st.gate(s, False, g.WAKE_RE, rm)
    check(f"{s!r} -> {want}", got == want, got)
check("a probe never interrupts real work",
      st.gate("So everything's up to date?", True, g.WAKE_RE, rm) == "ignore")

for s, want in [("I have to go inside and- Call your dad, a creep.", True),
                ("I'm on the phone", True), ("hold on, goat", True),
                ("დამირეკეს", True), ("What's on the phone screen?", False),
                ("Call the API again", False)]:
    check(f"enters_call({s!r}) == {want}", st.enters_call(s) == want)

# His whole history: how much would side mode drop? Only lines that are
# neither addressed nor questions — and only during a detected call.
lines = []
tx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workspace", "transcript.jsonl")
if os.path.exists(tx):
    for ln in open(tx, encoding="utf-8"):
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        u = (d.get("user") or "").strip()
        if u and not u.startswith("["):
            lines.append(u)
if lines:
    dropped = [u for u in lines if st.gate(u, False, g.WAKE_RE, rm) == "ignore"]
    share = len(dropped) / len(lines)
    print(f"      his {len(lines)} lines: {len(dropped)} would be dropped if said mid-call ({share:.0%})")
    check("mid-call, at most 1 in 3 of his lines would be dropped (none outside a call)",
          share <= 0.34, f"{share:.0%}")

# The [[quiet]] reply filter in GoatApp._speak_delta, fed the way the stream
# feeds it (token by token).
class _TTS:
    def __init__(self):
        self.said = []

    def say(self, t, **_):
        if t.strip():
            self.said.append(t)


def _stream(chunks):
    app = object.__new__(g.GoatApp)
    app.tts, app.shown = _TTS(), []
    app.emit = lambda k, d: app.shown.append(d) if k == "delta" else None
    app._say_buf, app._first_said = "", False
    app._quiet_state, app._quiet_probe = None, ""
    for c in chunks:
        app._speak_delta(c)
    return app


a = _stream(["[[", "qui", "et]]"])
check("[[quiet]] in pieces: nothing spoken or shown",
      a._quiet_state == "quiet" and not a.tts.said and not "".join(a.shown))
a = _stream(["  [[quiet]]", " sorry"])
check("[[quiet]] then stray text: still silent",
      a._quiet_state == "quiet" and not a.tts.said and not "".join(a.shown))
a = _stream(["[", "1] Done. Pushed it."])
check("a reply that only starts like the token is spoken whole",
      a._quiet_state == "speak" and "".join(a.shown) == "[1] Done. Pushed it.",
      "".join(a.shown))
a = _stream(["On it. ", "Opening YouTube."])
check("a normal reply streams untouched",
      a._quiet_state == "speak" and a.tts.said and a.tts.said[0].startswith("On it"),
      a.tts.said)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
