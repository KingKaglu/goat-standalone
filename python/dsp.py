"""The three bits of signal processing GOAT used scipy for, in plain numpy.

2026-10-01, his order: "cut GOAT's RAM footprint … nothing may slow GOAT
down". `import scipy.signal` cost ~66 MB of resident memory (and, with
scipy's own OpenBLAS, ~400 MB committed) for three small jobs:

- resample_poly(): Edge TTS hands back 24 kHz, the duplex stream is 16 kHz.
  This is scipy's exact recipe (Kaiser-windowed FIR, beta 5, half length
  10 x max(up, down); zero-stuff, convolve, decimate, same alignment), so
  the voice is the same to float precision — test_dsp.py checks it against
  scipy itself.
- comb(): the Ultron voice's single-tap feedback comb, which scipy did as
  lfilter([1], [1, 0 … 0, -g]). One tap means it can be run c samples at a
  time with no Python loop per sample — identical output, same speed.
- read_wav() / write_wav16(): soundfile and the stdlib wave module instead
  of scipy.io.wavfile.
"""
from __future__ import annotations

import io
import wave
from functools import lru_cache

import numpy as np


@lru_cache(maxsize=8)
def _kaiser_lowpass(up: int, down: int) -> np.ndarray:
    """scipy.signal.firwin(2*half+1, 1/max_rate, window=('kaiser', 5.0)) * up"""
    max_rate = max(up, down)
    half_len = 10 * max_rate
    n = 2 * half_len + 1
    cutoff = 1.0 / max_rate
    m = np.arange(n) - (n - 1) / 2.0
    h = cutoff * np.sinc(cutoff * m)
    h *= np.kaiser(n, 5.0)
    h /= h.sum()                      # firwin scale=True: unity gain at DC
    return h * up


def resample_poly(x: np.ndarray, up: int, down: int) -> np.ndarray:
    """1-D float signal resampled by up/down — scipy.signal.resample_poly."""
    x = np.asarray(x, dtype=np.float64)
    g = int(np.gcd(up, down))
    up, down = up // g, down // g
    if up == down == 1:
        return x.copy()
    n_in = x.size
    n_out = n_in * up // down + bool(n_in * up % down)
    h = _kaiser_lowpass(up, down)
    half_len = (h.size - 1) // 2
    n_pre_pad = down - half_len % down
    n_post_pad = 0
    n_pre_remove = (half_len + n_pre_pad) // down

    def out_len(len_h):
        return ((n_in - 1) * up + len_h - 1) // down + 1

    while out_len(h.size + n_pre_pad + n_post_pad) < n_out + n_pre_remove:
        n_post_pad += 1
    hp = np.concatenate([np.zeros(n_pre_pad), h, np.zeros(n_post_pad)])
    # Polyphase, computing only the samples that survive decimation. Output
    # j is sample k = (n_pre_remove + j) * down of the zero-stuffed
    # convolution; only taps of phase p = k % up meet real input there, and
    # the outputs of one phase step through x by `down`. Splitting those taps
    # once more by t mod down turns each piece into an ordinary convolution
    # of a decimated x — native np.convolve, no wasted multiplies. Same
    # numbers as convolve-then-decimate (test_dsp.py checks against scipy).
    lh = hp.size
    pad = lh + 2 * down + 2
    xpad = np.concatenate([np.zeros(pad), x, np.zeros(pad)])
    y = np.empty(n_out)
    for j0 in range(min(up, n_out)):
        k0 = (n_pre_remove + j0) * down
        p = k0 % up
        taps = hp[p::up]                     # taps[t] pairs with x[base - t]
        cnt = len(range(j0, n_out, up))
        base0 = (k0 - p) // up               # base for output i: base0 + down*i
        acc = np.zeros(cnt)
        for r in range(min(down, taps.size)):
            tr = taps[r::down]
            s_n = tr.size
            off = base0 - r + pad
            start = off - down * (s_n - 1)
            seq = xpad[start:start + down * (cnt + s_n - 1):down]
            acc += np.convolve(seq, tr)[s_n - 1:s_n - 1 + cnt]
        y[j0::up] = acc
    return y


def comb(y: np.ndarray, c: int, g: float) -> np.ndarray:
    """out[n] = y[n] + g * out[n - c]  (scipy lfilter([1], [1, 0…, -g]))."""
    y = np.asarray(y, dtype=np.float64)
    n = y.size
    nb = -(-n // c)                         # blocks of c samples
    Y = np.zeros(nb * c)
    Y[:n] = y
    Y = Y.reshape(nb, c)
    # Block b is y_b + g * out_{b-1}. M blocks at a time: a small lower-
    # triangular matrix of powers of g does M steps of that recursion in
    # one matmul — only positive powers, so nothing can blow up.
    M = 16
    i = np.arange(M)
    G = np.tril(float(g) ** (i[:, None] - i[None, :]))
    gp = float(g) ** (i + 1)
    out = np.empty_like(Y)
    prev = np.zeros(c)
    for b0 in range(0, nb, M):
        seg = Y[b0:b0 + M]
        m = seg.shape[0]
        o = G[:m, :m] @ seg + gp[:m, None] * prev
        out[b0:b0 + m] = o
        prev = o[-1]
    return out.reshape(-1)[:n]


def write_wav16(target, rate: int, pcm: np.ndarray) -> None:
    """Mono int16 PCM to a path or a file-like object."""
    pcm = np.asarray(pcm, dtype=np.int16)
    with wave.open(target, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())


def read_wav(path) -> tuple:
    """(rate, mono float32 in [-1, 1])."""
    import soundfile as sf
    data, rate = sf.read(path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data[:, 0]
    return int(rate), data


def wav_bytes16(rate: int, pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    write_wav16(buf, rate, pcm)
    return buf.getvalue()
