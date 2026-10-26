"""Music ducking: while GOAT speaks, other apps' audio drops to a fraction and
comes back when it stops. Per-app session volume (pycaw), never the master, so
GOAT's own voice and his system volume are untouched. Every change is
remembered and restored — on silence, on stop(), and at interpreter exit."""
import atexit
import os
import threading
import time

DUCK_TO = 0.25        # other apps play at 25% of their own level while GOAT talks
RELEASE_DELAY = 0.6   # stay ducked this long after speech ends (no pumping between sentences)
POLL = 0.1


class MusicDucker:
    def __init__(self, is_speaking, enabled=True):
        self.is_speaking = is_speaking   # callable -> bool
        self.enabled = enabled
        self._saved = {}                 # pid -> (SimpleAudioVolume, original level)
        self._stop = threading.Event()
        self._thread = None
        self._quiet_since = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="music-duck")
            self._thread.start()
            atexit.register(self.stop)

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # SimpleAudioVolume is a COM interface created on the worker.
            # Let that same COM apartment restore it before it exits.
            thread.join(timeout=2.0)

    def _sessions(self):
        from pycaw.pycaw import AudioUtilities
        me = os.getpid()
        for s in AudioUtilities.GetAllSessions():
            p = s.Process
            if p is None or p.pid == me:
                continue
            if p.name().lower() in ("audiodg.exe",):
                continue
            yield p.pid, s.SimpleAudioVolume

    def duck(self):
        try:
            for pid, vol in self._sessions():
                if pid in self._saved:
                    continue
                cur = vol.GetMasterVolume()
                if cur <= 0.02:
                    continue
                self._saved[pid] = (vol, cur)
                vol.SetMasterVolume(cur * DUCK_TO, None)
        except Exception as e:  # noqa: BLE001 — ducking is a nicety, never a crash
            print(f"[duck] {type(e).__name__}: {str(e)[:100]}")

    def _restore_on_owner_thread(self):
        saved, self._saved = self._saved, {}
        for vol, orig in saved.values():
            try:
                vol.SetMasterVolume(orig, None)
            except Exception:  # noqa: BLE001 — the app closed meanwhile
                pass

    def _run(self):
        import comtypes
        comtypes.CoInitialize()
        try:
            while not self._stop.is_set():
                time.sleep(POLL)
                try:
                    speaking = bool(self.enabled and self.is_speaking())
                except Exception:  # noqa: BLE001
                    speaking = False
                if speaking:
                    self._quiet_since = None
                    self.duck()
                elif self._saved:
                    if self._quiet_since is None:
                        self._quiet_since = time.monotonic()
                    elif time.monotonic() - self._quiet_since >= RELEASE_DELAY:
                        self._restore_on_owner_thread()
                        self._quiet_since = None
        finally:
            self._restore_on_owner_thread()
            comtypes.CoUninitialize()
