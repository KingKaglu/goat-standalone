"""dsp.py must match scipy exactly — the voice may not change by a hair.

Run:  py -3.13 test_dsp.py   (needs scipy, which GOAT itself no longer loads)
"""
import io
import os
import sys
import tempfile
import time

import numpy as np
from scipy.io import wavfile
from scipy.signal import lfilter, resample_poly

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dsp  # noqa: E402

PASS = FAIL = 0


def check(name, ok, info=""):
    global PASS, FAIL
    PASS += bool(ok)
    FAIL += not ok
    print(("[PASS] " if ok else "[FAIL] ") + name + ("" if ok else f"  {info}"))


rng = np.random.default_rng(7)
# Speech-like test signals: noise, a chirp, and a real clip if present.
sigs = {"noise": rng.standard_normal(24000 * 3).astype(np.float32) * 0.3,
        "chirp": np.sin(2 * np.pi * np.cumsum(np.linspace(80, 6000, 24000 * 2)) / 24000)
                 .astype(np.float32),
        "short": rng.standard_normal(37).astype(np.float32)}
for name, x in sigs.items():
    for up, down in ((2, 3), (320, 441), (3, 2), (1, 2)):
        if name == "short" and up > 10:
            continue
        ref = resample_poly(x, up, down)
        got = dsp.resample_poly(x, up, down)
        err = float(np.max(np.abs(ref - got))) if ref.shape == got.shape else 1e9
        check(f"resample_poly {name} {up}/{down} matches scipy (max err {err:.1e})",
              ref.shape == got.shape and err < 1e-5, f"{ref.shape} vs {got.shape}")

x = sigs["noise"][:16000 * 3]
for c in (67, 1, 500):
    a = np.zeros(c + 1)
    a[0], a[c] = 1.0, -0.33
    ref = lfilter([1.0], a, x)
    got = dsp.comb(x, c, 0.33)
    err = float(np.max(np.abs(ref - got)))
    check(f"comb c={c} matches lfilter (max err {err:.1e})", err < 1e-5)

# Speed: never slower than scipy by more than a millisecond on a 3 s clip.
clip = sigs["noise"]
t0 = time.perf_counter()
for _ in range(20):
    resample_poly(clip, 2, 3)
ts = (time.perf_counter() - t0) / 20
t0 = time.perf_counter()
for _ in range(20):
    dsp.resample_poly(clip, 2, 3)
td = (time.perf_counter() - t0) / 20
check(f"resample speed: dsp {td*1000:.2f}ms vs scipy {ts*1000:.2f}ms per 3s clip",
      td <= ts + 0.002)
a = np.zeros(68)
a[0], a[67] = 1.0, -0.33
t0 = time.perf_counter()
for _ in range(20):
    lfilter([1.0], a, x)
ts = (time.perf_counter() - t0) / 20
t0 = time.perf_counter()
for _ in range(20):
    dsp.comb(x, 67, 0.33)
td = (time.perf_counter() - t0) / 20
check(f"comb speed: dsp {td*1000:.2f}ms vs scipy {ts*1000:.2f}ms per 3s clip",
      td <= ts + 0.002)

# WAV round trip, byte-identical to scipy's writer.
pcm = (rng.standard_normal(16000) * 8000).astype(np.int16)
b1 = io.BytesIO()
wavfile.write(b1, 16000, pcm)
b2 = dsp.wav_bytes16(16000, pcm)
check("write_wav16 is byte-identical to scipy.io.wavfile", b1.getvalue() == b2)
p = os.path.join(tempfile.gettempdir(), "goat_dsp_test.wav")
dsp.write_wav16(p, 22050, pcm)
rate, data = dsp.read_wav(p)
check("read_wav: rate and samples survive",
      rate == 22050 and np.allclose(data, pcm.astype(np.float32) / 32768.0))
os.remove(p)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
