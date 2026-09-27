"""Live check: a dead audio stream (what resume-from-sleep leaves behind) must
be noticed and reopened by the watchdog. Uses the real mic/speaker.

Run:  cd C:/Users/user/goat-standalone/python && py -3.13 test_audio_stall.py
"""
import time

from audio_io import DuplexAudio

a = DuplexAudio(on_status=lambda m: print("status:", m))
a.start()
time.sleep(1.5)
print("before kill: active", a._stream.active,
      "gap", round(time.monotonic() - a._last_cb, 3))
a._stream.abort()  # simulate the resume-from-sleep death: callbacks stop
t0 = time.monotonic()
while a.stream_reopens == 0 and time.monotonic() - t0 < 10:
    time.sleep(0.2)
time.sleep(1.0)
gap = time.monotonic() - a._last_cb
print("after: reopens", a.stream_reopens, "active", a._stream.active,
      "gap", round(gap, 3))
ok = a.stream_reopens == 1 and a._stream.active and gap < 0.2
a.stop()
print("PASS" if ok else "FAIL")
