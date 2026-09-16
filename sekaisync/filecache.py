"""Process-level mtime-aware cache for parsed JSON files.

The aggregate endpoints (``status`` / ``progress`` / ``trust_summary``)
re-read the same large JSON files multiple times per call.  This module
gives read paths a shared in-process cache keyed by
``(scope, path, mtime_ns, size)`` so repeated reads hit memory until the
underlying file changes.  Write paths (index rebuilds, crawler saves)
must bypass this cache and read from disk directly: some of them mutate
the parsed page dicts in place, which would poison cached entries.

Cached values are shared mutable objects.  Callers must treat them as
read-only; anything that needs to mutate should parse its own copy.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

_lock = threading.Lock()
# (scope, path) -> (signature, parsed value); signature = (mtime_ns, size)
_cache: dict[tuple[str, str], tuple[tuple[int, int], Any]] = {}


def file_signature(path: Path) -> Optional[tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def cached_json(
    path: Path,
    load: Callable[[Path], Any],
    scope: str = "json",
) -> Any:
    """Return the parsed value of ``path``, re-parsing only when it changed.

    ``scope`` separates different parse results for the same file (e.g. the
    raw merged web index vs. the normalized page list derived from it).
    """
    key = (scope, str(path))
    signature = file_signature(path)
    if signature is None:
        return load(path)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == signature:
            return hit[1]
    value = load(path)
    with _lock:
        # Store the signature observed before parsing: if the file changed
        # while reading, the next stat() will mismatch and force a re-read.
        _cache[key] = (signature, value)
    return value


def cached_json_file(path: Path, scope: str = "json") -> Any:
    """``cached_json`` with the default ``json.loads`` reader."""
    return cached_json(path, lambda p: json.loads(p.read_text(encoding="utf-8")), scope=scope)


def clear_cache(scope: Optional[str] = None) -> None:
    """Drop cached entries (all, or one scope). Used by tests."""
    with _lock:
        if scope is None:
            _cache.clear()
        else:
            for key in [k for k in _cache if k[0] == scope]:
                del _cache[key]


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON so a reader never observes a partial file.

    A plain ``write_text`` over an authoritative file leaves a truncated
    document if the process dies mid-write, and nothing distinguishes it from
    a complete one.  Here the payload is serialized first (so a serialization
    failure cannot touch the target at all), written to a unique temporary
    file in the same directory, flushed and fsynced, then moved into place
    with ``os.replace`` — atomic on the same filesystem.

    Only this call's temporary file is cleaned up on failure.

    Scope: this makes a *single file* swap atomic.  It is not a substitute for
    cross-file consistency (that needs the immutable-generation pointers of
    P13) and it must never be used to rewrite the user's settings.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize before touching the filesystem so a bad payload cannot leave a
    # half-written authority file behind.
    data = json.dumps(payload, ensure_ascii=False, indent=2)

    tmp_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
