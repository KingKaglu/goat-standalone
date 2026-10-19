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
print("ALL PASS" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
