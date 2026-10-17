"""A soft pulse while GOAT works, so a long pause never reads as a dead line.
Synthesised in memory (no asset), quiet, played on its own stream; stops the
instant GOAT starts speaking or the work ends."""
import threading
import time

import numpy as np

SR = 24000
START_AFTER = 1.5     # seconds of work before the first pulse (fast turns stay silent)
EVERY = 2.4           # gap between pulses
GAIN = 0.05           # quiet on purpose


def _pulse() -> np.ndarray:
    n = int(SR * 0.55)
    t = np.arange(n) / SR
    env = np.sin(np.pi * t / t[-1]) ** 2
    tone = np.sin(2 * np.pi * 392 * t) + 0.5 * np.sin(2 * np.pi * 587 * t)
    return (GAIN * env * tone / 1.5).astype(np.float32)


class ThinkingSound:
    def __init__(self, is_thinking, is_speaking, enabled=True):
        self.is_thinking = is_thinking
        self.is_speaking = is_speaking
        self.enabled = enabled
        self._buf = _pulse()
        self._stop = threading.Event()
        self._since = None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="thinking-sound").start()

    def stop(self):
        self._stop.set()

    def _run(self):
        import sounddevice as sd
        last = 0.0
        while not self._stop.is_set():
            time.sleep(0.1)
            try:
                think = bool(self.enabled and self.is_thinking()
                             and not self.is_speaking())
            except Exception:  # noqa: BLE001
                think = False
            if not think:
                if self._since is not None:
                    self._since = None
                    try:
                        sd.stop()
                    except Exception:  # noqa: BLE001
                        pass
                continue
            now = time.monotonic()
            if self._since is None:
                self._since = now
            if now - self._since >= START_AFTER and now - last >= EVERY:
                last = now
                try:
                    sd.play(self._buf, SR)
                except Exception as e:  # noqa: BLE001
                    print(f"[think] {type(e).__name__}: {str(e)[:80]}")
