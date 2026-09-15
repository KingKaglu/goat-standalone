"""Machine gazetteer for the reflex lane — every file, folder and installed
app GOAT can open, resolved by NAME in microseconds.

Why this exists (2026-09-15, his complaint: "why did it take so long to just
open a file?"). Asking for `gg` on the desktop used to cost a full cloud turn
and then an ESCALATE into the work lane, which went looking for it with
SCREENSHOTS — take a picture of the desktop, find the icon with vision,
double-click it. Twenty seconds for a double-click.

Two root causes, both fixed here:

1. GOAT had no idea what was on the disk. Every "open X" was a guess the
   model had to make, and a wrong guess became a visual search.
2. His Desktop is ONEDRIVE-REDIRECTED — the real one is
   C:\\Users\\user\\OneDrive\\Desktop, while the prompt told the model
   C:\\Users\\user\\Desktop (which also exists, and is nearly empty). So the
   model looked in a real folder, found nothing, and escalated. `gg` lives in
   the OneDrive one. Known-folder IDs are asked of Windows itself here, so
   redirection can never lie to us again.

The index is built on a daemon thread at boot (~0.3s for his folders) and
cached to workspace/file-index.json so the very first command after a restart
still resolves instantly while the fresh scan runs behind it.

Nothing here touches Qt, the network, or a model. Pure disk + ctypes.
"""
import ctypes
import ctypes.wintypes
import difflib
import json
import os
import re
import threading
import time
import uuid

from goat_paths import GOAT_ROOT

CACHE = os.path.join(GOAT_ROOT, "workspace", "file-index.json")

# Folders never worth walking: they are enormous, and he never says "open
# node_modules". Skipping them is the difference between 0.3s and a minute.
SKIP_DIRS = {
    "node_modules", ".git", ".next", "__pycache__", "venv", ".venv", "env",
    "dist", "build", "target", ".cache", ".idea", ".vscode", "site-packages",
    ".pytest_cache", ".mypy_cache", "obj", "bin", ".gradle", "vendor",
    "AppData", "$RECYCLE.BIN", "System Volume Information",
}
# Noise that would otherwise win a fuzzy match against a real file.
SKIP_FILES = {"desktop.ini", "thumbs.db", ".ds_store", ".gitkeep"}

MAX_ENTRIES = 40_000

# ---- Windows known folders (authoritative: survives OneDrive redirection) --
_KNOWN = {
    "desktop":   "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures":  "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "videos":    "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "music":     "{4BD8D571-6D19-48D3-BE97-422220080E43}",
}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, spec: str):
        super().__init__()
        u = uuid.UUID(spec)
        self.Data1, self.Data2, self.Data3 = (
            u.time_low, u.time_mid, u.time_hi_version)
        for i, b in enumerate(u.bytes[8:]):
            self.Data4[i] = b


def _known_path(guid: str) -> str | None:
    try:
        fn = ctypes.windll.shell32.SHGetKnownFolderPath
        fn.argtypes = [ctypes.POINTER(_GUID), ctypes.wintypes.DWORD,
                       ctypes.wintypes.HANDLE,
                       ctypes.POINTER(ctypes.c_wchar_p)]
        out = ctypes.c_wchar_p()
        if fn(ctypes.byref(_GUID(guid)), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return out.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:  # noqa: BLE001 — a missing folder is not an error
        return None


_dirs_cache: dict = {}


def user_dirs() -> dict:
    """{'desktop': 'C:\\Users\\user\\OneDrive\\Desktop', ...} — the REAL
    locations, asked of Windows, not assembled from the home path."""
    if _dirs_cache:
        return _dirs_cache
    home = os.path.expanduser("~")
    for name, guid in _KNOWN.items():
        p = _known_path(guid)
        if not p or not os.path.isdir(p):
            p = os.path.join(home, name.capitalize())
        if os.path.isdir(p):
            _dirs_cache[name] = p
    # OneDrive redirection leaves the ORIGINAL folder in place and sometimes
    # still holds things — index both, with the live one taking priority.
    legacy = os.path.join(home, "Desktop")
    if (os.path.isdir(legacy)
            and os.path.normcase(legacy) != os.path.normcase(
                _dirs_cache.get("desktop", ""))):
        _dirs_cache["desktop2"] = legacy
    return _dirs_cache


def _start_menus() -> list:
    out = []
    for env, tail in (("APPDATA", r"Microsoft\Windows\Start Menu\Programs"),
                      ("ProgramData", r"Microsoft\Windows\Start Menu\Programs")):
        base = os.environ.get(env)
        if base:
            p = os.path.join(base, tail)
            if os.path.isdir(p):
                out.append(p)
    return out


# ---- name normalisation ----------------------------------------------------
# "Affiliate-Income-Guide-Georgia.docx" and "affiliate income guide georgia"
# have to be the same key. Georgian is left alone: its letters are already
# lowercase-only and carry no separators.
_SEP_RE = re.compile(r"[\s_\-.,!?'\"()\[\]{}+&@#]+")


def norm(s: str) -> str:
    return _SEP_RE.sub(" ", (s or "").strip().lower()).strip()


def _stem(name: str, is_dir: bool) -> str:
    if is_dir:
        return name
    root, ext = os.path.splitext(name)
    # A bare ".lnk"/".url" is plumbing, never something he says out loud.
    return root if ext.lower() in (".lnk", ".url", ".exe", ".bat", ".cmd",
                                   ".ps1", ".msi") else name


# ---- the index -------------------------------------------------------------
# entry = [norm_key, display_name, full_path, is_dir, source, depth]
_lock = threading.Lock()
_entries: list = []
_built_at = 0.0
_building = False


def _walk(root: str, source: str, max_depth: int, out: list):
    stack = [(root, 0)]
    while stack and len(out) < MAX_ENTRIES:
        path, depth = stack.pop()
        try:
            it = os.scandir(path)
        except OSError:
            continue
        with it:
            for e in it:
                if len(out) >= MAX_ENTRIES:
                    break
                name = e.name
                if name.startswith("~$") or name.lower() in SKIP_FILES:
                    continue
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir and name in SKIP_DIRS:
                    continue
                key = norm(_stem(name, is_dir))
                if key:
                    out.append([key, name, e.path, is_dir, source, depth])
                if is_dir and depth < max_depth:
                    stack.append((e.path, depth + 1))


def build() -> list:
    """Scan the places he actually keeps things. Blocking (~0.3s here)."""
    dirs = user_dirs()
    out: list = []
    # Depth is generous on the desktop (project folders live there) and thin
    # everywhere else — deep hits are noise he never asks for by bare name.
    plan = [(dirs.get("desktop"), "desktop", 3),
            (dirs.get("desktop2"), "desktop", 2),
            (dirs.get("downloads"), "downloads", 2),
            (dirs.get("documents"), "documents", 2),
            (dirs.get("pictures"), "pictures", 1),
            (dirs.get("videos"), "videos", 1),
            (dirs.get("music"), "music", 1)]
    for root, source, depth in plan:
        if root and os.path.isdir(root):
            _walk(root, source, depth, out)
    for menu in _start_menus():
        _walk(menu, "app", 4, out)
    return out


def _save(entries: list):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "entries": entries}, f)
        os.replace(tmp, CACHE)
    except OSError:
        pass


def _load() -> list:
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f).get("entries") or []
    except (OSError, ValueError):
        return []


def refresh(background: bool = True):
    """Rebuild the index. At boot: serve the cache instantly, rescan behind."""
    global _building
    with _lock:
        if _building:
            return
        _building = True
        if not _entries:
            cached = _load()
            if cached:
                _entries.extend(cached)

    def work():
        global _built_at, _building
        try:
            fresh = build()
            with _lock:
                _entries[:] = fresh
                _built_at = time.time()
            _save(fresh)
            print(f"[reflex-index] {len(fresh)} entries")
        except Exception as e:  # noqa: BLE001 — no index is degraded, not fatal
            print(f"[reflex-index] build failed: {e}")
        finally:
            _building = False

    if background:
        threading.Thread(target=work, daemon=True).start()
    else:
        work()


def ready() -> bool:
    return bool(_entries)


def age() -> float:
    return time.time() - _built_at if _built_at else float("inf")


# ---- lookup ----------------------------------------------------------------
# Scoring, best first. Exact name beats prefix beats fuzzy; a shallower, more
# "front of the machine" hit beats a buried one. Everything is a plain list
# scan over a few thousand tuples — tens of microseconds, no model involved.
_SOURCE_BONUS = {"desktop": 30, "app": 26, "downloads": 14,
                 "documents": 10, "pictures": 4, "videos": 4, "music": 4}


def lookup(name: str, prefer: str = "", want_dir: bool | None = None,
           apps_only: bool = False) -> tuple | None:
    """Best (display_name, full_path, is_dir, source) for a spoken name.

    prefer — a location he mentioned ("on my desktop", "დესკტოპზე"), which
    only tilts the score; it never excludes a hit somewhere else, because his
    "desktop" often means "the screen I look at", not the folder.
    """
    key = norm(name)
    if not key or not _entries:
        return None
    with _lock:
        rows = list(_entries)
    if apps_only:
        rows = [r for r in rows if r[4] == "app"]
    best = None
    best_score = 0
    keys = []
    for r in rows:
        k = r[0]
        score = 0
        if k == key:
            score = 100
        elif k.startswith(key + " ") or k.startswith(key):
            score = 78 if len(key) >= 2 else 0
        elif f" {key} " in f" {k} ":
            score = 70
        elif len(key) >= 4 and key in k:
            score = 58
        if not score:
            keys.append(k)
            continue
        score += _SOURCE_BONUS.get(r[4], 0)
        score -= r[5] * 4              # shallower wins
        if prefer and r[4] == prefer:
            score += 25
        if want_dir is not None and r[3] == want_dir:
            score += 8
        # Shortcuts are what "open spotify" means — a .lnk beats a raw folder.
        if r[2].lower().endswith((".lnk", ".url")):
            score += 6
        if score > best_score:
            best, best_score = r, score
    if best is not None:
        return best[1], best[2], best[3], best[4]
    # Nothing literal — allow one fuzzy pass. Cloud STT mangles short names
    # ("GG" comes back as "gigi", "ჯიჯი"), so this is not a luxury.
    close = difflib.get_close_matches(key, keys, n=1, cutoff=0.84)
    if close:
        for r in rows:
            if r[0] == close[0]:
                return r[1], r[2], r[3], r[4]
    return None


def lookup_live(name: str, prefer: str = "", want_dir: bool | None = None,
                apps_only: bool = False) -> tuple | None:
    """lookup(), and on a miss a fresh shallow re-scan of the two folders
    that change constantly.

    The index is built at boot, so a file he saved or downloaded SINCE then
    is invisible to it — and "I just downloaded it, open it" is one of the
    most natural things to say. A depth-1 rescan of Desktop and Downloads
    costs a few milliseconds, which the reflex budget can afford on a miss;
    the full rebuild is kicked off behind it so the next lookup is complete.
    """
    hit = lookup(name, prefer, want_dir, apps_only)
    if hit is not None or apps_only:
        return hit
    fresh: list = []
    dirs = user_dirs()
    for key_name in ("desktop", "desktop2", "downloads"):
        root = dirs.get(key_name)
        if root and os.path.isdir(root):
            _walk(root, "desktop" if key_name.startswith("desktop")
                  else "downloads", 1, fresh)
    with _lock:   # the background rebuild swaps _entries wholesale
        known = {r[2] for r in _entries}
    added = [r for r in fresh if r[2] not in known]
    if added:
        with _lock:
            _entries.extend(added)
        refresh(background=True)     # pick up everything else properly
        return lookup(name, prefer, want_dir, apps_only)
    return None
