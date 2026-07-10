import json
import os
import subprocess
import tempfile
import time

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

from goat_paths import GOAT_ROOT

PIPER_EXE = os.path.join(GOAT_ROOT, "tts", "piper", "piper.exe")
PIPER_VOICE = os.path.join(GOAT_ROOT, "tts", "en_GB-alan-low.onnx")


def synth_to_16k(text: str, target_rate: int = 16000) -> np.ndarray:
    """Same piper.exe/voice server.js already uses — one-shot invocation is
    fine here since this is a verification script, not the resident server."""
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            [PIPER_EXE, "-m", PIPER_VOICE, "-f", wav_path],
            input=text.encode("utf-8"),
            check=True,
            capture_output=True,
            creationflags=flags,
        )
        rate, data = wavfile.read(wav_path)
    finally:
        os.unlink(wav_path)

    if data.dtype == np.int16:
        data = data.astype(np.float32) / 32768.0
    else:
        data = data.astype(np.float32)
    if data.ndim > 1:
        data = data[:, 0]

    if rate != target_rate:
        g = np.gcd(rate, target_rate)
        data = resample_poly(data, target_rate // g, rate // g).astype(np.float32)

    return data


def _load_wav_16k(wav_path: str, target_rate: int) -> np.ndarray:
    rate, data = wavfile.read(wav_path)
    if data.dtype == np.int16:
        data = data.astype(np.float32) / 32768.0
    else:
        data = data.astype(np.float32)
    if data.ndim > 1:
        data = data[:, 0]
    if rate != target_rate:
        g = np.gcd(rate, target_rate)
        data = resample_poly(data, target_rate // g, rate // g).astype(np.float32)
    return data


class PiperResident:
    """Piper kept alive with the voice loaded (--json-input) — ~0.2s per
    sentence instead of ~1.4s of engine start each call. Same length_scale
    (0.85, slightly brisk) as server.js's /tts route."""

    def __init__(self):
        self.proc = None
        self._start()

    def _start(self):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            [PIPER_EXE, "-m", PIPER_VOICE, "--json-input"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def synth(self, text: str, target_rate: int = 16000,
              length_scale: float = 0.85, timeout_s: float = 10.0) -> np.ndarray:
        """Blocking — call from the TTS worker thread only (single consumer;
        piper processes its stdin lines strictly in order)."""
        text = " ".join(text.split())
        if not text:
            return np.zeros(0, dtype=np.float32)
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            line = json.dumps({"text": text, "output_file": wav_path,
                               "length_scale": length_scale}) + "\n"
            self.proc.stdin.write(line.encode("utf-8"))
            self.proc.stdin.flush()
            # Same readiness signal server.js uses: file exists, has data,
            # and the size has stopped changing.
            deadline = time.time() + timeout_s
            last_size = -1
            while time.time() < deadline:
                try:
                    size = os.path.getsize(wav_path)
                except OSError:
                    size = -1
                if size > 44 and size == last_size:
                    return _load_wav_16k(wav_path, target_rate)
                last_size = size
                time.sleep(0.06)
            raise TimeoutError(f"piper produced no wav in {timeout_s}s")
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
