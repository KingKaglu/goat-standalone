"""Bug 2026-10-18 13:50: "Okay, have we updated it?" was answered but never
heard on either device. A side-talk line ("Whatever we need.") arrived while
the answer was queued; _talk cancelled the voice, then the brain answered
[[quiet]]. Also: faint far-field phone clips became phantom lines ("Go on.").

Run: Python313 test_stale_voice.py (no network, no models)."""
import asyncio
import sys
import time

import numpy as np

import goat_app as g
import phone_server as ps

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    PASS, FAIL = PASS + ok, FAIL + (not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail and not ok else ""))


class Tts:
    def __init__(self, speaking=True):
        self._speaking = speaking
        self.cancels = self.new_turns = 0
        self.said = []
        self.gen = 0
        self._sounded = True

    def speaking(self):
        return self._speaking

    def cancel(self):
        self.cancels += 1
        self.gen += 1
        self._speaking = False

    def new_turn(self):
        self.new_turns += 1

    def mark_reply(self):
        pass

    def say(self, t, **_):
        if t.strip():
            self.said.append(t)


def app_for(speaking=True, busy=False):
    a = object.__new__(g.GoatApp)
    a.tts = Tts(speaking)
    a.events = []
    a.emit = lambda k, d="": a.events.append((k, d))
    a.busy = busy
    a.turn_lang = "en"
    a._say_buf, a._first_said = "", False
    a._quiet_state, a._quiet_probe = None, ""
    a._merge_prev = None
    a.worked = []

    async def fake_work(text, hard=False, echo_you=False):
        a.worked.append(text)
    a._work = fake_work
    return a


async def main():
    # 1. Side talk while my answer is still queued: the answer must survive.
    a = app_for(speaking=True)
    await a._talk("Whatever we need.")
    check("new line while answer plays: voice NOT cancelled yet",
          a.tts.cancels == 0 and a._stale_voice and a.worked == ["Whatever we need."])
    a._quiet_state, a._quiet_probe = None, ""
    for c in ("[[", "quiet", "]]"):
        a._speak_delta(c)
    check("brain says [[quiet]]: old answer still not cancelled",
          a.tts.cancels == 0 and a._quiet_state == "quiet")

    # 2. A real follow-up: old answer gives way at the first real words.
    a = app_for(speaking=True)
    await a._talk("And what about the laptop?")
    a._quiet_state, a._quiet_probe = None, ""
    a._speak_delta("The laptop's fine. ")
    check("real reply: old voice cancelled once, fresh label opened",
          a.tts.cancels == 1 and a.tts.new_turns == 1 and not a._stale_voice
          and ("delta", "") in a.events, (a.tts.cancels, a.tts.new_turns))
    check("real reply is spoken", a.tts.said == ["The laptop's fine."], a.tts.said)

    # 3. Nothing playing: old behaviour (cancel at once).
    a = app_for(speaking=False)
    await a._talk("What time is it in Tbilisi?")
    check("nothing playing: immediate cancel as before",
          a.tts.cancels == 1 and not getattr(a, "_stale_voice", False))

    # 4. "stop" while I talk: cut at once, never deferred.
    a = app_for(speaking=True)
    await a._talk("stop")
    check("stop is never deferred",
          not getattr(a, "_stale_voice", False))

    # 5. Mid-turn (busy, spoken answer streaming): side talk is held, not a cut.
    a = app_for(speaking=True, busy=True)
    a._cut, a._turn_has_tools, a._after_turn = False, False, ""
    cuts = []

    async def fake_cut(then):
        cuts.append(then)
    a._cut_turn = fake_cut
    a.hard_model = a.work_model = "x"
    await g.GoatApp._work(a, "Whatever we need.")
    check("busy + side talk: answer not cut, line held",
          not cuts and a._after_turn == "Whatever we need.")
    await g.GoatApp._work(a, "Have we updated it?")
    check("busy + a real question: cut as before", cuts == ["Have we updated it?"])


class FakeApp:
    def __init__(self):
        self.tts = type("T", (), {"cancel": lambda s: None, "on_sentence": None,
                                  "on_cancel": None, "local_mute": False})()
        self.heard, self.phone_live, self.reply_to = [], False, "phone"
        self._say_buf = ""

    def _route(self, origin):
        self.reply_to = origin

    def emit(self, *a, **k):
        pass

    def _lat_start(self):
        pass

    async def _heard(self, text, gap, vcheck, source):
        self.heard.append(text)

    async def _handle_utterance(self, audio, rt=None, gap=None, source="laptop"):
        self.heard.append("<loud clip>")


async def phone():
    import stt_whisper
    app = FakeApp()
    srv = ps.PhoneServer(app, port=0)
    srv._send = lambda *_: None
    srv._phone_speaks = lambda: True
    said = {"t": ""}
    stt_whisper.transcribe = lambda audio: said["t"]

    def clip(peak):
        x = (np.sin(np.arange(16000) / 5.0) * peak * 32767).astype("<i2")
        return x.tobytes()

    # The raw bubble mic: his own voice peaks ~0.1. No loudness gate any more
    # ("Please restore them" was thrown away as "faint", 2026-10-18 14:05).
    srv._utterance(clip(0.095))
    await asyncio.sleep(0.2)
    check("quiet bubble clip: straight through", app.heard == ["<loud clip>"], app.heard)
    app.heard.clear()
    srv._utterance(clip(0.9))
    await asyncio.sleep(0.2)
    check("his normal voice: straight through", app.heard == ["<loud clip>"], app.heard)
    app.heard.clear()
    # "Music active" right after my own voice is me (the panel's audio
    # context), not a video: no name needed.
    srv._voice_end = time.time() - 2.5
    srv._utterance(clip(0.3), media=True)
    await asyncio.sleep(0.2)
    check("media flag just after my voice: straight through",
          app.heard == ["<loud clip>"], app.heard)
    app.heard.clear()
    srv._voice_end = 0.0
    said["t"] = "Okay, close all the tabs."
    srv._utterance(clip(0.3), media=True)
    await asyncio.sleep(0.2)
    check("real other-app audio, no name: ignored", app.heard == [], app.heard)
    said["t"] = "GOAT, close all the tabs."
    srv._utterance(clip(0.3), media=True)
    await asyncio.sleep(0.2)
    check("other-app audio that names me: heard",
          app.heard == ["GOAT, close all the tabs."], app.heard)
    # Echo of my own words, whichever mic sent it.
    srv._said.append((time.time(), "Checking what the panel does to audio when it closes."))
    check("my own sentence back: echo",
          srv.is_echo("Taking what the panel does to audio, I think."))
    check("his new words: not echo", not srv.is_echo("I have never said that."))
    srv._said.append((time.time(), "Want me to close the tabs?"))
    check("his answer reusing my words: not echo",
          not srv.is_echo("Yes, close the tabs."))
    check("over my voice, mostly my words: echo",
          srv.is_echo("close the tabs", overlap=True))


asyncio.run(main())
asyncio.run(phone())
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
