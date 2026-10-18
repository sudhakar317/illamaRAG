"""Memory reporting, for a project that runs close to its RAM limit.

Surfaced via /api/health so the client can show real numbers: on an 8 GB
machine running a 2.5 GB model, "is it swapping?" is a question worth being
able to answer at a glance.

Windows-only; every function degrades to None elsewhere rather than raising.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.wintypes.DWORD),
        ("dwMemoryLoad", ctypes.wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


_MB = 1024 * 1024
_GB = 1024**3


def process_memory_mb() -> dict | None:
    """Resident ("working set") memory of this process, in MB."""
    if not sys.platform.startswith("win"):
        return None
    try:
        fn = ctypes.windll.kernel32.K32GetProcessMemoryInfo
        # argtypes matter: without them the 64-bit process HANDLE is truncated
        # to 32 bits and the call reports zeroes.
        fn.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            ctypes.wintypes.DWORD,
        ]
        fn.restype = ctypes.wintypes.BOOL

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not fn(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        ):
            return None
        return {
            "rss_mb": round(counters.WorkingSetSize / _MB, 1),
            "peak_rss_mb": round(counters.PeakWorkingSetSize / _MB, 1),
        }
    except Exception:  # noqa: BLE001 - diagnostics must never break a request
        return None


def system_memory_gb() -> dict | None:
    """Total / available physical RAM, in GB."""
    if not sys.platform.startswith("win"):
        return None
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return {
            "total_gb": round(status.ullTotalPhys / _GB, 2),
            "available_gb": round(status.ullAvailPhys / _GB, 2),
            "percent_used": status.dwMemoryLoad,
        }
    except Exception:  # noqa: BLE001
        return None


def snapshot() -> dict:
    """Both views at once, for the health endpoint."""
    return {"process": process_memory_mb(), "system": system_memory_gb()}
