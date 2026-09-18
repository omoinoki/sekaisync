"""B8 performance: cold + warm medians/p95, and Windows actual working set."""
import ctypes
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sekaisync.core import SekaiSyncCore

_psapi = ctypes.windll.psapi
_psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
_psapi.GetProcessMemoryInfo.restype = ctypes.c_int


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def ws_mb():
    """Actual process working set — covers SQLite/C allocations, which
    tracemalloc does not."""
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    ok = _psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(),
                                     ctypes.byref(pmc), pmc.cb)
    return round(pmc.WorkingSetSize / 1e6, 1) if ok else None


core = SekaiSyncCore(Path("store"))
print("python:", sys.version.split()[0], "| sqlite:", __import__("sqlite3").sqlite_version)
print("working set after Core init:", ws_mb(), "MB")


def bench(label, fn, runs):
    t = time.perf_counter()
    r = fn()
    cold = time.perf_counter() - t
    samples = []
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t)
    samples.sort()
    p95 = samples[min(len(samples) - 1, max(0, int(round(0.95 * len(samples))) - 1))]
    rows = len(r) if hasattr(r, "__len__") else "n/a"
    print(f"{label}: cold={cold:.2f}s median={statistics.median(samples):.2f}s "
          f"p95={p95:.2f}s min={samples[0]:.2f}s rows={rows} ws={ws_mb()}MB")
    sys.stdout.flush()


bench("status()", lambda: core.status(), 3)
bench("web_browse(limit=20)", lambda: core.web_browse(limit=20), 10)
bench("web_lookup(limit=8)", lambda: core.web_lookup("星乃一歌", limit=8), 3)
print("final working set:", ws_mb(), "MB")
