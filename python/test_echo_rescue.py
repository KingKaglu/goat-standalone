"""His words inside my own echo (2026-10-19): the phone clips that were
dropped whole as "my own voice" while they carried his "stop" or his request.
Every case is a real transcript from goat-app.log that day.
Run: py -3.13 test_echo_rescue.py"""
import sys
import time

import phone_server as ps

s = ps.PhoneServer.__new__(ps.PhoneServer)
now = time.time()
s._said = [(now - 30, "Sir, yes, your phone is already on the newest build, 2 9 8 7 3 1 7 7. "
                      "So the install went through, and you can test talking over me whenever you like."),
           (now - 10, "Sir, looking into it."),
           (now - 5, "Sir, here's one. Once upon a time, in a small workshop in Essen, there was a "
                     "laptop with eight gigabytes of memory and a very big ego. It called itself the "
                     "GOAT, and it was convinced it could run the whole city if anyone would just let it.")]
CASES = [
    ("Here's one. Once upon a time, in a small workshop in Essen- Okay, stop ... there was a laptop "
     "with eight gigabytes of memory- No, no, no, no ... and a very big ego. Stop. It called itself "
     "the GOAT.", "stop"),
    ("So the install went through, and you can test talking over me whenever you like. No, could "
     "you please tell me a story so I can test it out.", "run"),
    ("Sir, looking into it.", "drop"),
    ("3, 1, 7, 7.", "drop"),
    ("Once upon a time in a small workshop in Essen there was a laptop", "drop"),
    ("It called itself the goat and it was convinced it could run the city", "drop"),
    ("a laptop with eight gigabytes, wait, wait, stop", "stop"),
    ("in a small workshop გაჩერდი in Essen", "stop"),
]
fails = 0
for text, want in CASES:
    stop = s.stop_inside_echo(text)
    run = None if stop else s.his_run(text)
    got = "stop" if stop else "run" if run else "drop"
    ok = got == want
    fails += not ok
    print(("PASS " if ok else "FAIL ") + f"{want:4} {text[:70]!r}" + ("" if ok else f" -> {got}"))

# My echo misheard by the ear is still my echo (2026-10-19 11:59: "up the
# cliffs, and Elio" came back "off a cliff, and Emilio" and was answered).
said0 = s._said
s._said = [(time.time() - 3, "On a quiet slope in the Dolomites lived a goat named Elio, who was "
            "the smallest in his herd. The big goats raced each other up the cliffs, "
            "and Elio always finished last.")]
for text, want in [("... Off a cliff, and Emilio always finished last.", "drop"),
                   ("the big goats raced each other stop stop wait", "stop"),
                   ("up the cliffs could you tell me about the weather instead", "run")]:
    stop = s.stop_inside_echo(text)
    got = "stop" if stop else "run" if s.his_run(text) else "drop"
    ok = got == want
    fails += not ok
    print(("PASS " if ok else "FAIL ") + f"{want:4} {text!r}" + ("" if ok else f" -> {got}"))
s._said = said0

# Paused for "him" at a reply's onset: room noise (no words from the streaming
# ear in BARGE_HEAR_S) tells the page to carry on; his words keep it paused.
import asyncio
ps.BARGE_HEAR_S = 0.01


class Ear:
    def __init__(self, p): self.last_partial = p


def watch(partial, clip_in=False):
    sent = []
    s._send = sent.append
    s._streams = {"a": {"rt": Ear(partial), "t": time.monotonic()}}
    s._barge = mark = (time.time(), "")

    async def go():
        t = asyncio.create_task(s._barge_watch(mark))
        if clip_in:
            s._barge = None
        await t
    asyncio.run(go())
    return any('"carry"' in x for x in sent)


for name, ok in [("noise -> carry on", watch("")),
                 ("his words -> stay paused", not watch("stop")),
                 ("clip already in -> its verdict", not watch("", clip_in=True))]:
    fails += not ok
    print(("PASS " if ok else "FAIL ") + name)

# A stray word cut in over my voice carries on; his stop/order/answer don't.
import goat_app as g


class App:
    turn_lang = "en"


for text, heard, want in [("I am.", "Bruno climbed.", True), ("Okay", "x", True),
                          ("Wait", "x", False), ("მოიცა", "x", False),
                          ("Goat.", "x", False), ("Yes.", "Shall I close it?", False),
                          ("volume up", "x", False), ("Tell me another one", "x", False)]:
    ok = g.GoatApp._murmur(App(), text, heard) == want
    fails += not ok
    print(("PASS " if ok else "FAIL ") + f"murmur={want} {text!r}")
for text in ("Stop. Stop. Wait a second.", "wait a sec", "okay one second"):
    ok = bool(g.BARE_STOP_RE.match(text) and g.BARE_STOP_CORE.search(text))
    fails += not ok
    print(("PASS " if ok else "FAIL ") + f"bare stop {text!r}")
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
