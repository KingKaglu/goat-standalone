"""Tie GOAT's engine processes to GOAT's own life (2026-09-27).

The crash this exists for: GOAT restarted itself, restart-goat.ps1 killed
python.exe, but the engine it had spawned (the SDK's bundled claude.exe)
survived as an orphan. That orphan still held the handle to goat-app.log it
inherited through the launcher's `>> goat-app.log` redirect, so the launcher's
log rotation hit "Permission denied", the VBScript died before it ever started
GOAT, the watchdog read that as a boot crash and rolled back innocent code, and
the relaunch died on the same lock. GOAT stayed down.

The fix at the root: every claude.exe GOAT spawns is put into a Windows Job
Object created with KILL_ON_JOB_CLOSE. Only this process holds the job handle,
so the moment python.exe dies for ANY reason (clean exit, Stop-Process, an
access violation inside Qt) the OS closes the handle and kills the engine with
it. Nothing of GOAT's can outlive GOAT.

SILENT_BREAKAWAY_OK keeps the job to exactly the processes we adopt: whatever
the engine launches for Giorgi (Chrome, Spotify, a build) is NOT in the job and
keeps running when GOAT restarts.

The SDK spawns the engine itself and GOAT reconnects it in several places, so
instead of hooking each spawn a daemon thread adopts new direct children every
couple of seconds. A Toolhelp snapshot costs well under a millisecond.
Windows-only; every failure is swallowed — this must never be able to stop GOAT.
"""
import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes

# The engine, and (2026-10-01, RAM) GOAT's helper servers: a whisper-server
# spawned the night before was still holding 325 MB long after its GOAT died.
ENGINE_NAMES = {"claude.exe", "whisper-server.exe", "piper.exe"}
# Every scan walks the whole process table through ctypes — ~1% of a core
# at a 2s poll, all day, for an engine that only appears on (re)connect.
# So: a slow background scan, and a fast burst right after kick(), which the
# engine calls as it connects.
POLL_S = 10.0
FAST_POLL_S = 1.0
FAST_FOR_S = 20.0

_JobObjectExtendedLimitInformation = 9
_KILL_ON_JOB_CLOSE = 0x2000
_SILENT_BREAKAWAY_OK = 0x1000
_TH32CS_SNAPPROCESS = 0x2
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


_adopted: set[int] = set()
_started = False


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    return k


def _make_job(k):
    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = (
        _KILL_ON_JOB_CLOSE | _SILENT_BREAKAWAY_OK)
    if not k.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                     ctypes.byref(info), ctypes.sizeof(info)):
        k.CloseHandle(job)
        return None
    return job


def engine_children(k=None) -> list[int]:
    """PIDs of engine processes whose parent is this process."""
    k = k or _k32()
    me = os.getpid()
    snap = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return []
    found = []
    try:
        e = _ProcessEntry()
        e.dwSize = ctypes.sizeof(e)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if (e.th32ParentProcessID == me
                    and e.szExeFile.lower() in ENGINE_NAMES):
                found.append(e.th32ProcessID)
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return found


_jobs: list = []      # one kill-on-close job per adopted process


def adopt_once(k=None) -> list[int]:
    """Put each not-yet-adopted child into its OWN kill-on-close job.

    One shared job broke the engine (2026-10-01): with piper already inside
    it, assigning claude.exe failed with ERROR_ACCESS_DENIED — the engine
    sits in a job of its own, and Windows only nests jobs in a strict tree.
    A job per process can never conflict with another adoption. All the
    handles live in GOAT, so when GOAT dies every one of them closes."""
    k = k or _k32()
    newly = []
    for pid in engine_children(k):
        if pid in _adopted:
            continue
        h = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not h:
            continue
        job = _make_job(k)
        try:
            if job and k.AssignProcessToJobObject(job, h):
                _adopted.add(pid)
                _jobs.append(job)
                newly.append(pid)
                job = None
        finally:
            k.CloseHandle(h)
            if job:
                k.CloseHandle(job)
    return newly


_wake = threading.Event()
_fast_until = time.monotonic() + FAST_FOR_S   # boot is a connect too


def kick() -> None:
    """An engine is about to spawn: scan fast for the next FAST_FOR_S."""
    global _fast_until
    _fast_until = time.monotonic() + FAST_FOR_S
    _wake.set()


def _loop():
    k = _k32()
    while True:
        try:
            for pid in adopt_once(k):
                print(f"[guard] pid {pid} (engine/helper) bound to GOAT's life",
                      flush=True)
        except Exception:  # noqa: BLE001 — a guard must never take GOAT down
            pass
        fast = time.monotonic() < _fast_until
        _wake.wait(FAST_POLL_S if fast else POLL_S)
        _wake.clear()


def start() -> None:
    """Start the adopter thread once. No-op off Windows."""
    global _started
    if _started or sys.platform != "win32":
        return
    _started = True
    threading.Thread(target=_loop, name="child-guard", daemon=True).start()
