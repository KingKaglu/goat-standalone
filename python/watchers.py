"""Background watchers: GOAT notices things on its own and says so.

Finished downloads, low disk, low battery, a flapping charger and RAM
starvation. Each alert goes to the phone as a push and, when GOAT is idle,
is spoken on the laptop. Pure polling in one daemon thread; it never raises
into the app and never speaks over a turn in progress."""
import os
import threading
import time

import psutil

POLL = 15                 # seconds between checks
DISK_WARN_GB = 25         # warn below this, re-arm above DISK_OK_GB
DISK_OK_GB = 30
BATT_WARN = 20            # percent, only while discharging
RAM_WARN = 95             # percent, must hold for RAM_HOLD polls
RAM_HOLD = 4
FLAP_COUNT = 3            # plug/unplug changes ...
FLAP_WINDOW = 600         # ... inside this many seconds
COOLDOWN = 1800           # same alert type at most every 30 min
PARTIAL = (".crdownload", ".part", ".tmp", ".download", ".opdownload")


def _downloads_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Downloads")


class Watchers:
    def __init__(self, engine, downloads: str | None = None):
        self.engine = engine
        self.dir = downloads or _downloads_dir()
        self._stop = threading.Event()
        self._seen: set[str] = set()      # downloads already announced / pre-existing
        self._sizes: dict[str, int] = {}  # candidate -> size at previous poll
        self._last: dict[str, float] = {}
        self._disk_armed = True
        self._plug: bool | None = None
        self._flips: list[float] = []
        self._ram_hits = 0
        self._primed = False

    # ---- plumbing ----
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="watchers").start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(POLL):
            try:
                for kind, spoken, push in self.poll():
                    self.alert(kind, spoken, push)
            except Exception as e:  # noqa: BLE001 — a watcher must never hurt GOAT
                print(f"[watchers] {type(e).__name__}: {e}")

    def _cool(self, kind: str, now: float) -> bool:
        if kind in self._last and now - self._last[kind] < COOLDOWN:
            return False
        self._last[kind] = now
        return True

    def alert(self, kind: str, spoken: str, push: str):
        eng = self.engine
        try:     # v8 (e): a quiet card in the window too, spoken or not
            import json as _j
            eng.emit("card", _j.dumps({"text": push, "kicker": "heads up",
                                       "key": f"watch-{kind}"}))
        except Exception:  # noqa: BLE001
            pass
        phone = getattr(eng, "phone", None)
        if phone is not None:
            try:
                phone.notify("GOAT", push, f"watch-{kind}")
            except Exception:  # noqa: BLE001
                pass
        try:
            idle = (not eng.busy and not getattr(eng, "suppressed", False)
                    and not eng.tts.speaking() and not eng.audio.is_tts_playing)
        except Exception:  # noqa: BLE001
            idle = False
        if idle and eng.loop is not None:
            eng.loop.call_soon_threadsafe(eng._say_now, spoken)
        else:
            try:
                eng.emit("status", push)
            except Exception:  # noqa: BLE001
                pass

    # ---- the checks (pure, testable: poll() returns what to announce) ----
    def poll(self, now: float | None = None) -> list[tuple[str, str, str]]:
        now = time.time() if now is None else now
        out: list[tuple[str, str, str]] = []
        out += self._downloads(now)
        out += self._disk(now)
        out += self._power(now)
        out += self._ram(now)
        self._primed = True
        return out

    def _downloads(self, now):
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for n in names:
            p = os.path.join(self.dir, n)
            if n in self._seen or n.lower().endswith(PARTIAL) or not os.path.isfile(p):
                continue
            if not self._primed:           # first sweep: everything there is old news
                self._seen.add(n)
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size > 0 and self._sizes.get(n) == size:   # unchanged over a poll = finished
                self._seen.add(n)
                self._sizes.pop(n, None)
                short = n if len(n) <= 40 else n[:37] + "..."
                out.append(("download", f"Your download finished: {short}.",
                            f"Download finished: {n}"))
            else:
                self._sizes[n] = size
        return out

    def _disk(self, now):
        free = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").free / 1e9
        if free > DISK_OK_GB:
            self._disk_armed = True
        elif free < DISK_WARN_GB and self._disk_armed:
            self._disk_armed = False
            return [("disk", f"Heads up, the C drive is down to {free:.0f} gigabytes free.",
                     f"C: is down to {free:.0f} GB free")]
        return []

    def _power(self, now):
        out = []
        b = psutil.sensors_battery()
        if b is None:
            return out
        plug = bool(b.power_plugged)
        if self._plug is not None and plug != self._plug:
            self._flips = [t for t in self._flips if now - t < FLAP_WINDOW] + [now]
            if len(self._flips) >= FLAP_COUNT and self._cool("flap", now):
                out.append(("flap", "The charger keeps dropping out. That loose jack again.",
                            "Charger keeps disconnecting (loose jack)"))
        self._plug = plug
        if not plug and b.percent <= BATT_WARN and self._cool("battery", now):
            out.append(("battery", f"Battery's at {b.percent:.0f} percent and not charging.",
                        f"Battery {b.percent:.0f}%, not charging"))
        return out

    def _ram(self, now):
        used = psutil.virtual_memory().percent
        self._ram_hits = self._ram_hits + 1 if used >= RAM_WARN else 0
        if self._ram_hits >= RAM_HOLD and self._cool("ram", now):
            return [("ram", f"Memory's at {used:.0f} percent, things may start to crawl.",
                     f"RAM at {used:.0f}%")]
        return []
