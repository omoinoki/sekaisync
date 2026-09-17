from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from sekaisync.config import REGIONS, SekaiSyncConfig
from sekaisync.layout import (
    factpack_path,
    freshness_path,
    generation_dir,
    generation_master_dir,
    glossary_path,
    region_master_dir,
    registry_path,
    region_source_dir,
    seed_glossary_path,
)
from sekaisync import dbstore
from sekaisync.coverage import build_region_coverage, build_source_manifest
from sekaisync.factpacks import build_fact_packs, save_fact_packs
from sekaisync.filecache import write_json_atomic
from sekaisync.glossary import merge_glossary, save_glossary
from sekaisync.registry import build_registry, data_files_for_region, save_registry


USER_AGENT = "SekaiSync/0.1 (+local knowledge sync)"

# --------------------------------------------------------------------------
# Input budgets (B1/P12 boundary work).
#
# These are PROVISIONAL starting points taken from the P12 review, not
# capacities measured against the real upstream sources.  They exist so an
# untrusted response cannot grow without bound; they must be calibrated on
# authorised data copies / known-good source samples before being treated as
# anything more than a conservative ceiling.
# --------------------------------------------------------------------------
BUDGET_HTML_BYTES = 16 * 1024 * 1024          # 16 MiB - HTML/XML documents
BUDGET_JSON_BYTES = 128 * 1024 * 1024         # 128 MiB - a single JSON document
BUDGET_ARCHIVE_COMPRESSED_BYTES = 512 * 1024 * 1024   # 512 MiB on the wire
BUDGET_ARCHIVE_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB extracted
BUDGET_ARCHIVE_MEMBERS = 100_000              # member count per archive

# Redirects are re-validated hop by hop and capped; urllib will not be allowed
# to follow a hop that the policy rejects.
MAX_REDIRECTS = 5

# Read size for the bounded transport.  Small enough that a hostile body is
# stopped close to the budget instead of after one oversized read.
_READ_CHUNK = 64 * 1024

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Suffixes that may be materialised from an archive.  The GitHub master-data
# tarballs are text/JSON containers; binaries are not an intended payload.
ALLOWED_ARCHIVE_SUFFIXES = frozenset(
    {".json", ".jsonl", ".txt", ".md", ".markdown", ".csv", ".tsv", ".xml", ".yaml", ".yml"}
)
# Extension-less text files that appear in such archives.
ALLOWED_ARCHIVE_NAMES = frozenset(
    {"license", "licence", "notice", "readme", "changelog", ".gitignore", ".gitattributes"}
)

_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class FetchError(Exception):
    """A response violated the transport boundary (scheme, redirects, budget)."""


class BudgetExceededError(FetchError):
    """A response exceeded its byte or redirect budget."""


class UnsafeArchiveError(FetchError):
    """An archive member would escape the staging directory or is disallowed."""


class StoreBusyError(RuntimeError):
    """Another writer already holds this store's writer lease."""


# In-process guards: a file lock alone does not stop two threads in one
# process from both acquiring (POSIX flock is per file description, and
# Windows msvcrt locking is per handle).  Keyed by resolved store path so two
# different stores never block each other.
_PROCESS_LEASES: dict[str, threading.Lock] = {}
_PROCESS_LEASES_GUARD = threading.Lock()


def _process_lease_for(store_root: Path) -> threading.Lock:
    key = str(Path(store_root).resolve())
    with _PROCESS_LEASES_GUARD:
        lock = _PROCESS_LEASES.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LEASES[key] = lock
        return lock


class WriterLease:
    """Exclusive write access to one store, across threads and processes.

    Every write entry point (sync, crawl, migration, postprocess, review)
    takes this before opening its transaction, so two writers cannot
    interleave a read-modify-write against the same store.  Callers acquire
    the lease *first* and only then open a connection, so lock ordering is
    uniform and cannot deadlock.

    The lock is a file beside the store (``kb/.writer.lock``) held open for
    the duration: ``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere.
    Possession of the file is NOT the lock — a stale file left by a crashed
    process must not block future writers, which is why liveness is decided by
    whether the OS granted the lock, never by the file's existence.
    """

    def __init__(self, store_root: Path, *, timeout: float = 0.0):
        self.store_root = Path(store_root)
        self.timeout = timeout
        self._thread_lock: Optional[threading.Lock] = None
        self._handle = None

    @property
    def lock_path(self) -> Path:
        from sekaisync.layout import kb_dir

        return kb_dir(self.store_root) / ".writer.lock"

    def __enter__(self) -> "WriterLease":
        self._thread_lock = _process_lease_for(self.store_root)
        acquired = (
            self._thread_lock.acquire(blocking=False)
            if self.timeout <= 0
            else self._thread_lock.acquire(timeout=self.timeout)
        )
        if not acquired:
            raise StoreBusyError(
                f"another writer holds the lease for {self.store_root} "
                f"(in-process lock) — refusing to write concurrently"
            )
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.lock_path, "a+b")
            _lock_file_exclusive(self._handle, self.timeout)
        except Exception:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            self._release_thread_lock()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            if self._handle is not None:
                _unlock_file(self._handle)
                self._handle.close()
        finally:
            self._handle = None
            self._release_thread_lock()

    def _release_thread_lock(self) -> None:
        if self._thread_lock is not None:
            try:
                self._thread_lock.release()
            except RuntimeError:
                pass
            self._thread_lock = None


def _lock_file_exclusive(handle, timeout: float) -> None:
    """Take an OS-level exclusive lock on an open file handle."""
    if os.name == "nt":
        import msvcrt

        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if timeout <= 0 or time.monotonic() >= deadline:
                    raise StoreBusyError(
                        "another process holds this store's writer lease"
                    ) from None
                time.sleep(0.05)
    import fcntl

    if timeout <= 0:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise StoreBusyError(
                "another process holds this store's writer lease"
            ) from None
        return
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise StoreBusyError(
                    "another process holds this store's writer lease"
                ) from None
            time.sleep(0.05)


def _unlock_file(handle) -> None:
    """Release only the lock this lease took.

    Deliberately does not delete the lock file: removing it while another
    process waits on the same path would let a third writer in.
    """
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        # The handle is closing anyway; a failure here must not mask the
        # body's result.
        pass


@contextlib.contextmanager
def store_writer_lock(store_root: Path, *, timeout: float = 0.0):
    """Acquire this store's writer lease for the duration of the block."""
    lease = WriterLease(store_root, timeout=timeout)
    with lease:
        yield lease


def validate_fetch_url(
    url: str,
    *,
    allowed_hosts: Optional[Iterable[str]] = None,
    allowed_schemes: Iterable[str] = _ALLOWED_SCHEMES,
) -> str:
    """Validate a URL before any bytes are read.

    Rejects non-HTTP(S) schemes (``file:``, ``data:``, ``ftp:``, ...) and
    URLs carrying userinfo (``user:pass@host``), which are not valid targets
    for this transport.  ``allowed_hosts`` optionally pins the destination to
    an explicit host allowlist (exact host or ``.suffix`` match).
    """
    text = str(url or "").strip()
    if not text:
        raise FetchError("Empty URL is not a fetchable target")
    if any(ch in text for ch in "\r\n\t"):
        raise FetchError(f"URL contains control characters: {text!r}")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise FetchError(f"Unparseable URL: {text!r} ({exc})") from exc
    scheme = parsed.scheme.lower()
    allowed = {str(item).lower() for item in allowed_schemes}
    if scheme not in allowed:
        raise FetchError(
            f"URL scheme {scheme or '(none)'!r} is not allowed "
            f"(only {', '.join(sorted(allowed))}): {text!r}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise FetchError(f"URL must not contain userinfo credentials: {text!r}")
    if "@" in parsed.netloc:
        # urlsplit keeps a bare "user@" in netloc's username, but a malformed
        # authority can still smuggle one through; treat it as userinfo.
        raise FetchError(f"URL must not contain userinfo credentials: {text!r}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise FetchError(f"URL has no host: {text!r}")
    if allowed_hosts:
        permitted = {str(item).strip().lower() for item in allowed_hosts if str(item).strip()}
        if permitted and not any(
            host == candidate or host.endswith("." + candidate) for candidate in permitted
        ):
            raise FetchError(f"Host {host!r} is not in the allowed host set: {text!r}")
    return text


def _authority_of(url: str) -> tuple[str, str, Optional[int]]:
    """(scheme, host, port) of a URL, with the scheme's default port filled in."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return ("", "", None)
    scheme = parsed.scheme.lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return (scheme, (parsed.hostname or "").lower(), port)


class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that validates every hop and caps the hop count.

    urllib follows redirects automatically, including across hosts and (in
    its default configuration) to schemes this transport refuses.  Each hop
    is therefore re-validated against the same policy as the original URL,
    and the chain is capped at ``max_redirects``.
    """

    def __init__(
        self,
        allowed_hosts: Optional[Iterable[str]] = None,
        max_redirects: int = MAX_REDIRECTS,
        same_host_only: bool = False,
    ) -> None:
        super().__init__()
        self._allowed_hosts = (
            tuple(str(item).strip().lower() for item in allowed_hosts if str(item).strip())
            if allowed_hosts
            else ()
        )
        self._max_redirects = max(0, int(max_redirects))
        self._same_host_only = bool(same_host_only)

    def _validate_target(self, req, newurl: str) -> None:
        """Apply the same policy to a redirect target as to the original URL."""
        try:
            validate_fetch_url(newurl, allowed_hosts=self._allowed_hosts or None)
        except FetchError as exc:
            raise FetchError(
                f"Redirect to {newurl!r} from {req.full_url!r} rejected: {exc}"
            ) from exc
        if self._same_host_only and _authority_of(newurl) != _authority_of(req.full_url):
            raise FetchError(
                f"Cross-origin redirect from {req.full_url!r} to {newurl!r} is "
                "not allowed"
            )

    def _check_hops(self, req, newurl: str) -> int:
        hops = int(getattr(req, "_sekaisync_redirect_hops", 0)) + 1
        if hops > self._max_redirects:
            raise BudgetExceededError(
                f"Redirect chain exceeded {self._max_redirects} hops at {newurl!r}"
            )
        return hops

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        self._validate_target(req, newurl)
        hops = self._check_hops(req, newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new._sekaisync_redirect_hops = hops  # type: ignore[attr-defined]
        return new

    def _handle_30x(self, req, fp, code, msg, headers):
        # urllib rejects some redirect schemes with an HTTPError before
        # ``redirect_request`` ever runs; validating the Location header here
        # keeps the failure a single consistent type for callers.
        location = headers.get("location") if headers is not None else None
        if location:
            newurl = urllib.parse.urljoin(req.full_url, location)
            self._validate_target(req, newurl)
            self._check_hops(req, newurl)
        return super().http_error_302(req, fp, code, msg, headers)

    # urllib binds the 3xx aliases to ``http_error_302`` at class-creation
    # time, so each one is rebound explicitly for the override to take effect.
    http_error_301 = _handle_30x
    http_error_302 = _handle_30x
    http_error_303 = _handle_30x
    http_error_307 = _handle_30x
    http_error_308 = _handle_30x


def build_opener(
    *,
    allowed_hosts: Optional[Iterable[str]] = None,
    max_redirects: int = MAX_REDIRECTS,
    same_host_only: bool = False,
) -> urllib.request.OpenerDirector:
    """An opener whose redirect handling is bounded and validated hop by hop."""
    return urllib.request.build_opener(
        _BoundedRedirectHandler(
            allowed_hosts=allowed_hosts,
            max_redirects=max_redirects,
            same_host_only=same_host_only,
        )
    )


def _declared_length(response) -> Optional[int]:
    """``Content-Length`` when present and sane; used for early rejection only."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def read_bounded(response, max_bytes: int, *, what: str = "response") -> bytes:
    """Read a response body, accumulating *actual* bytes against ``max_bytes``.

    ``Content-Length`` is only an early-reject shortcut: it may be absent or
    wrong, so the accumulated count of bytes actually read is the limit that
    matters.  Exceeding the budget raises instead of silently truncating into
    what would look like a complete document.
    """
    if max_bytes <= 0:
        raise BudgetExceededError(f"Refusing to read {what}: no byte budget configured")
    declared = _declared_length(response)
    if declared is not None and declared > max_bytes:
        raise BudgetExceededError(
            f"{what} declares {declared} bytes, over the {max_bytes} byte budget"
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        block = response.read(_READ_CHUNK)
        if not block:
            break
        total += len(block)
        if total > max_bytes:
            raise BudgetExceededError(
                f"{what} exceeded the {max_bytes} byte budget while reading"
            )
        chunks.append(block)
    return b"".join(chunks)


def open_validated(
    url: str,
    *,
    timeout: float = 60,
    allowed_hosts: Optional[Iterable[str]] = None,
    max_redirects: int = MAX_REDIRECTS,
    same_host_only: bool = False,
    headers: Optional[dict[str, str]] = None,
) -> object:
    """Open a validated HTTP(S) target; caller closes the returned response.

    One place applies the scheme/userinfo policy, the hop-by-hop redirect
    validation and the hop cap, so probes and document fetches cannot drift
    apart.  No bytes are read here.
    """
    target = validate_fetch_url(url, allowed_hosts=allowed_hosts)
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(target, headers=request_headers)
    opener = build_opener(
        allowed_hosts=allowed_hosts,
        max_redirects=max_redirects,
        same_host_only=same_host_only,
    )
    return opener.open(request, timeout=timeout)


def fetch_bytes(
    url: str,
    *,
    max_bytes: int = BUDGET_HTML_BYTES,
    timeout: float = 60,
    allowed_hosts: Optional[Iterable[str]] = None,
    max_redirects: int = MAX_REDIRECTS,
    same_host_only: bool = False,
    what: str = "response",
) -> bytes:
    """Fetch ``url`` with a validated boundary and a hard byte budget.

    The scheme/userinfo policy is applied *before* any read, redirects are
    validated hop by hop and capped, and the body is accumulated against
    ``max_bytes``.  A breach raises rather than returning partial content.
    """
    with open_validated(
        url,
        timeout=timeout,
        allowed_hosts=allowed_hosts,
        max_redirects=max_redirects,
        same_host_only=same_host_only,
    ) as response:
        return read_bounded(response, max_bytes, what=what)


def download_file(
    url: str,
    dest: Path,
    timeout: int = 60,
    *,
    max_bytes: int = BUDGET_ARCHIVE_COMPRESSED_BYTES,
    allowed_hosts: Optional[Iterable[str]] = None,
    max_redirects: int = MAX_REDIRECTS,
    same_host_only: bool = False,
) -> Path:
    """Download ``url`` to ``dest`` within the transport boundary.

    The target is validated before any read, redirects are bounded, and the
    body is written through a temporary file that is only moved into place
    once the whole (in-budget) download succeeded — a failure never leaves a
    truncated file that downstream code could mistake for a full document.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(dest.name + ".partial")
    if tmp_path.exists():
        tmp_path.unlink()
    try:
        with open_validated(
            url,
            timeout=timeout,
            allowed_hosts=allowed_hosts,
            max_redirects=max_redirects,
            same_host_only=same_host_only,
        ) as response:
            declared = _declared_length(response)
            if declared is not None and declared > max_bytes:
                raise BudgetExceededError(
                    f"Download declares {declared} bytes, over the "
                    f"{max_bytes} byte budget"
                )
            total = 0
            with open(tmp_path, "wb") as handle:
                while True:
                    block = response.read(_READ_CHUNK)
                    if not block:
                        break
                    total += len(block)
                    if total > max_bytes:
                        raise BudgetExceededError(
                            f"Download exceeded the {max_bytes} byte budget "
                            "while reading"
                        )
                    handle.write(block)
        os.replace(tmp_path, dest)
    except BaseException:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    return dest


def _archive_member_target(member_name: str) -> tuple[str, ...]:
    """Validate one member name and return its safe relative path parts."""
    name = str(member_name or "")
    if not name or name in {".", ".."}:
        raise UnsafeArchiveError(f"Archive member has an unusable name: {name!r}")
    if "\x00" in name:
        raise UnsafeArchiveError(f"Archive member name contains a NUL byte: {name!r}")
    if "\\" in name:
        # A backslash is either a Windows separator in disguise or an
        # ambiguous component; both make the target unpredictable.
        raise UnsafeArchiveError(
            f"Archive member uses a backslash path separator: {name!r}"
        )
    if _WINDOWS_DRIVE_RE.match(name):
        raise UnsafeArchiveError(f"Archive member uses a drive-letter path: {name!r}")
    parts = tuple(part for part in name.split("/") if part not in {"", "."})
    if not parts:
        raise UnsafeArchiveError(f"Archive member has an empty path: {name!r}")
    for part in parts:
        if part == "..":
            raise UnsafeArchiveError(
                f"Archive member escapes the staging directory: {name!r}"
            )
        if part.startswith("/"):
            raise UnsafeArchiveError(f"Archive member is absolute: {name!r}")
        if ":" in part:
            # NTFS alternate data stream ("name:stream") or a drive-relative
            # component; neither is a legitimate name inside the staging dir.
            raise UnsafeArchiveError(
                f"Archive member uses an alternate data stream or drive syntax: {name!r}"
            )
        if any(ord(ch) < 32 for ch in part):
            raise UnsafeArchiveError(
                f"Archive member name contains a control character: {name!r}"
            )
        stem = part.split(".")[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise UnsafeArchiveError(
                f"Archive member uses a Windows reserved device name: {name!r}"
            )
    if name.startswith("/") or Path(name).is_absolute():
        raise UnsafeArchiveError(f"Archive member is absolute: {name!r}")
    return parts


def _is_allowed_archive_file(name: str) -> bool:
    lowered = name.lower()
    suffix = Path(lowered).suffix
    if suffix in ALLOWED_ARCHIVE_SUFFIXES:
        return True
    return lowered in ALLOWED_ARCHIVE_NAMES


def archive_member_plan(
    tarball: Path,
    *,
    max_members: int = BUDGET_ARCHIVE_MEMBERS,
    max_bytes: int = BUDGET_ARCHIVE_EXTRACTED_BYTES,
) -> list[tuple[str, bool, tuple[str, ...]]]:
    """Stream an archive and validate every member before anything is written.

    Returns the accepted ``(member name, is_dir, relative parts)`` list in
    archive order.  Any disallowed member type (symlink / hardlink / device /
    FIFO), escape attempt, duplicate or over-budget condition rejects the
    whole input.
    """
    accepted: list[tuple[str, bool, tuple[str, ...]]] = []
    seen: dict[str, str] = {}
    total_bytes = 0
    count = 0
    with tarfile.open(tarball, "r|gz") as archive:
        for member in archive:
            count += 1
            if count > max_members:
                raise BudgetExceededError(
                    f"Archive {tarball.name} exceeded the {max_members} member budget"
                )
            name = str(member.name or "")
            if member.isdir():
                parts = _archive_member_target(name)
                key = "/".join(parts).casefold()
                if key in seen:
                    raise UnsafeArchiveError(
                        f"Archive contains colliding member targets: "
                        f"{name!r} and {seen[key]!r}"
                    )
                seen[key] = name
                accepted.append((name, True, parts))
                continue
            if member.issym() or member.islnk():
                raise UnsafeArchiveError(
                    f"Archive member {name!r} is a link "
                    f"(to {member.linkname!r}); links are not extracted"
                )
            if not member.isreg():
                raise UnsafeArchiveError(
                    f"Archive member {name!r} is not a regular file or directory "
                    f"(type {member.type!r}); device/FIFO members are not extracted"
                )
            parts = _archive_member_target(name)
            if not _is_allowed_archive_file(parts[-1]):
                raise UnsafeArchiveError(
                    f"Archive member {name!r} is not an allowed text/JSON file"
                )
            key = "/".join(parts).casefold()
            if key in seen:
                raise UnsafeArchiveError(
                    f"Archive contains colliding member targets: {name!r} and {seen[key]!r}"
                )
            seen[key] = name
            if member.size and member.size > 0:
                total_bytes += int(member.size)
                if total_bytes > max_bytes:
                    raise BudgetExceededError(
                        f"Archive {tarball.name} exceeded the {max_bytes} byte "
                        "extracted budget"
                    )
            accepted.append((name, False, parts))
    return accepted


def _ensure_no_link_components(staging_root: Path, target: Path) -> None:
    """Refuse to write through a symlink/Junction anywhere below staging."""
    current = staging_root
    for part in target.relative_to(staging_root).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise UnsafeArchiveError(f"Refusing to write through link {current}")


def extract_tarball(
    tarball: Path,
    dest: Path,
    *,
    max_members: int = BUDGET_ARCHIVE_MEMBERS,
    max_bytes: int = BUDGET_ARCHIVE_EXTRACTED_BYTES,
) -> Path:
    """Extract a text/JSON archive into ``dest`` within an explicit boundary.

    The whole input is validated first (member types, paths, duplicates,
    member/size budgets); only then are member *contents* copied into a fresh
    staging directory.  Archive permissions/owners are never applied, links
    and device nodes are refused, and any breach rejects the whole input.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    staging_root = Path(os.path.abspath(str(dest)))
    plan = archive_member_plan(tarball, max_members=max_members, max_bytes=max_bytes)

    written_bytes = 0
    with tarfile.open(tarball, "r|gz") as archive:
        index = 0
        for member in archive:
            if index >= len(plan):
                break
            expected_name, is_dir, parts = plan[index]
            if str(member.name) != expected_name:
                raise UnsafeArchiveError(
                    f"Archive changed while extracting: expected "
                    f"{expected_name!r}, found {str(member.name)!r}"
                )
            index += 1
            target = dest.joinpath(*parts)
            resolved = Path(os.path.abspath(os.path.normpath(str(target))))
            if resolved != staging_root and staging_root not in resolved.parents:
                raise UnsafeArchiveError(
                    f"Archive member {member.name!r} resolves outside the "
                    f"staging directory: {resolved}"
                )
            if is_dir:
                _ensure_no_link_components(staging_root, target)
                target.mkdir(parents=True, exist_ok=True)
                continue
            _ensure_no_link_components(staging_root, target)
            if target.is_symlink() or target.is_dir():
                raise UnsafeArchiveError(
                    f"Refusing to overwrite non-file target {target}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise UnsafeArchiveError(
                    f"Archive member {member.name!r} has no readable contents"
                )
            with source, open(target, "wb") as handle:
                while True:
                    block = source.read(_READ_CHUNK)
                    if not block:
                        break
                    written_bytes += len(block)
                    if written_bytes > max_bytes:
                        raise BudgetExceededError(
                            f"Archive {tarball.name} exceeded the {max_bytes} "
                            "byte extracted budget while writing"
                        )
                    handle.write(block)
    return dest


def _staging_dir(source_dir: Path) -> Path:
    """A same-filesystem staging directory beside ``source_dir``."""
    return source_dir.parent / f".{source_dir.name}.staging"


def _tarball_allowed_hosts(config: SekaiSyncConfig) -> tuple[str, ...]:
    """Host allowlist for a master-data tarball, derived from local config.

    The allowlist comes from the configured base URL (and an optional
    ``extra.tarball_allowed_hosts`` override) — never from remote content.
    ``codeload.github.com`` is added only for a real github.com base, because
    GitHub's ``/archive/`` endpoint always redirects there; that known hop is
    the reason the tarball fetch is not restricted to a single origin.
    """
    hosts: list[str] = []
    try:
        base_host = (urllib.parse.urlsplit(str(config.github_tarball_base)).hostname or "").lower()
    except ValueError:
        base_host = ""
    if base_host:
        hosts.append(base_host)
    if base_host in {"github.com", "www.github.com"}:
        hosts.append("codeload.github.com")
    extra = config.extra.get("tarball_allowed_hosts") if isinstance(config.extra, dict) else None
    if isinstance(extra, (list, tuple)):
        hosts.extend(str(item).strip().lower() for item in extra if str(item).strip())
    elif isinstance(extra, str) and extra.strip():
        hosts.append(extra.strip().lower())
    return tuple(dict.fromkeys(hosts))


def _assert_tree_has_no_links(root: Path) -> None:
    """Refuse a local mirror tree containing symlinks/reparse points.

    ``shutil.copytree`` follows links by default, which would silently pull
    bytes from outside the mirror into the published staging directory.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in current.iterdir():
            if entry.is_symlink():
                raise UnsafeArchiveError(
                    f"Local mirror contains a link, refusing to copy: {entry}"
                )
            if entry.is_dir():
                stack.append(entry)


def _commit_staging(staging: Path, target: Path) -> None:
    """Replace ``target`` with ``staging``, restoring the old data on failure.

    Windows intermittently denies directory renames right after writes
    (antivirus/indexer holds handles briefly), so renames retry a few times.
    """
    backup = target.parent / f".{target.name}.bak"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    moved_old = False
    if target.exists():
        target.rename(backup)
        moved_old = True
    try:
        for attempt in range(5):
            try:
                staging.rename(target)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(1.0 + attempt)
    except Exception:
        if moved_old and backup.exists():
            backup.rename(target)
        raise
    if moved_old:
        shutil.rmtree(backup, ignore_errors=True)


def fetch_region_from_tarball(region_key: str, config: SekaiSyncConfig) -> Path:
    region = REGIONS[region_key]
    repo = region.repo_slug
    if not repo:
        raise ValueError(f"Region {region_key} has no GitHub repository configured")
    url = f"{config.github_tarball_base}/{repo}/archive/refs/heads/main.tar.gz"
    source_dir = region_source_dir(config.store_root, region_key)
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_dir(source_dir)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tarball = Path(tmp) / "master.tar.gz"
            download_file(url, tarball, allowed_hosts=_tarball_allowed_hosts(config))
            extract_tarball(tarball, staging)
        _commit_staging(staging, source_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return source_dir


def fetch_region_from_local(region_key: str, local_mirror: Path, config: SekaiSyncConfig) -> Path:
    if not local_mirror.exists():
        raise FileNotFoundError(f"Local mirror not found: {local_mirror}")
    _assert_tree_has_no_links(local_mirror)
    source_dir = region_source_dir(config.store_root, region_key)
    source_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_dir(source_dir)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(local_mirror, staging, dirs_exist_ok=True)
        _commit_staging(staging, source_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return source_dir


def fetch_region(
    region_key: str,
    config: SekaiSyncConfig,
    local_mirror: Optional[Path] = None,
) -> Path:
    if local_mirror is not None:
        return fetch_region_from_local(region_key, local_mirror, config)
    return fetch_region_from_tarball(region_key, config)


def _region_versions(
    config: SekaiSyncConfig, regions: Iterable[str],
    roots: Optional[Mapping[str, Path]] = None,
) -> dict[str, dict]:
    """Client/data/asset version numbers from each region's versions.json.

    Sekai Viewer's home page shows these (e.g. ``6.7.0`` / ``6.7.0.40``);
    surfacing them in freshness lets agents state which game version the
    local facts correspond to.  A missing file yields an empty entry so the
    report stays complete.
    """
    out: dict[str, dict] = {}
    for region in regions:
        if region == "demo":
            out[region] = {}
            continue
        # versions.json lives inside the per-region source tree (e.g. under a
        # sekai-master-db-*-diff-main/ subdirectory), mirroring how
        # data_files_for_region discovers tables.
        root = roots[region] if roots is not None else _freshness_roots(config, (region,))[region]
        matches = sorted(root.glob("**/versions.json"))
        if not matches:
            out[region] = {}
            continue
        path = matches[0]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            out[region] = {}
            continue
        out[region] = {
            key: data[key]
            for key in (
                "appVersion",
                "dataVersion",
                "assetVersion",
                "multiPlayVersion",
                "appVersionStatus",
            )
            if data.get(key)
        }
    return out


def _freshness_roots(
    config: SekaiSyncConfig, regions: Iterable[str],
    generation: Optional[str] = None, conn: Optional[object] = None,
) -> dict[str, Path]:
    pointers = dbstore.active_generations(config.store_root, conn=conn)
    roots = {}
    for region in regions:
        selected = generation
        if not selected or not generation_master_dir(config.store_root, region, selected).exists():
            selected = pointers.get(region)
        roots[region] = (
            generation_master_dir(config.store_root, region, selected)
            if selected else region_master_dir(config.store_root, region)
        )
    return roots


def _build_freshness(
    config: SekaiSyncConfig,
    regions: Iterable[str],
    web_status: Optional[dict] = None,
    news_available: bool = False,
    generation: Optional[str] = None,
    conn: Optional[object] = None,
) -> dict:
    regions = tuple(regions)
    roots = _freshness_roots(config, regions, generation, conn)
    region_info = {}
    for region in regions:
        if region == "demo":
            region_info[region] = {
                "language": "zh_hans",
                "official_translation": False,
                "launch_date": None,
                "source": "demo",
            }
        else:
            region_info[region] = {
                "language": REGIONS[region].language,
                "official_translation": REGIONS[region].official_translation,
                "launch_date": REGIONS[region].launch_date,
                "source": f"master_db:{region}",
            }
    master_available = {
        region: (
            region == "demo"
            or any(child.suffix == ".json" for child in roots[region].rglob("*") if child.is_file())
        )
        for region in regions
    }
    freshness = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "regions": region_info,
        # Which raw generation this report describes. Without it a consumer
        # cannot tell whether the coverage above and the raw files on disk come
        # from the same publish (Astra P13: manifest/cache are projections of a
        # committed state and must not claim success earlier than the pointer).
        "raw_generation": generation,
        "coverage": build_region_coverage(
            regions,
            web_status=web_status,
            news_available=news_available,
            master_available=master_available,
        ),
        "versions": _region_versions(config, regions, roots),
        "sources": build_source_manifest(config.sites),
        "web": web_status or {
            "enabled": False,
            "consent": False,
            "sources": {},
        },
    }
    return freshness


def store_freshness_record(conn: object, record: Mapping[str, Any]) -> None:
    """Persist the freshness record in the store's SQL ``meta`` table.

    The DB is the authoritative copy, committed inside the same transaction as
    the indexes and generation pointers; the ``cache/freshness.json`` file is a
    projection that can always be re-materialised with
    :func:`project_freshness`.
    """
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('freshness', ?)",
        (json.dumps(record, ensure_ascii=False, sort_keys=True),),
    )


def project_freshness(config: SekaiSyncConfig) -> Optional[Path]:
    """Write ``cache/freshness.json`` from the committed SQL record.

    Returns the written path, or ``None`` when no record has been committed
    yet. Idempotent, so callers may run it after any crash to repair the file.
    """
    with dbstore.connect(config.store_root) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='freshness'").fetchone()
    if row is None:
        return None
    try:
        record = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    path = freshness_path(config.store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, record)
    return path


def write_freshness(
    config: SekaiSyncConfig,
    regions: Iterable[str],
    web_status: Optional[dict] = None,
    news_available: bool = False,
    generation: Optional[str] = None,
) -> Path:
    """Standalone freshness writer for non-publish callers.

    The publish path uses :func:`_build_freshness` +
    :func:`store_freshness_record` inside its transaction instead, so the
    record and the state it describes commit together.
    """
    freshness = _build_freshness(
        config, regions, web_status=web_status,
        news_available=news_available, generation=generation,
    )
    path = freshness_path(config.store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, freshness)
    return path


def rebuild_indexes(
    config: SekaiSyncConfig,
    regions: Iterable[str],
    generation: Optional[str] = None,
    conn: Optional[object] = None,
) -> dict:
    """Rebuild registry / glossary / factpacks from the local master source tree.

    Shared by ``sync`` (full baseline) and the incremental new-event check, so
    both paths produce indexes from the same source data with the same
    semantics.  Returns entity/term counts for callers that report them.

    ``generation`` pins which raw generation to read. Leave it ``None`` only
    for one-shot CLI work; a publish must pass the generation it prepared so
    the indexes it commits describe exactly the files it is about to make
    visible.

    ``conn`` lets a publish run the SQL writes inside its own BEGIN IMMEDIATE
    transaction, so the indexes and the generation pointer commit atomically.
    """
    entities = build_registry(config.store_root, regions, generation)
    dbstore.save_entities(config.store_root, entities, conn=conn)
    seed = []
    seed_path = seed_glossary_path(config.store_root)
    if seed_path.exists():
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
    terms = merge_glossary(entities, seed)
    dbstore.save_glossary_terms(config.store_root, terms, conn=conn)
    # A caller-owned transaction must not publish files before it commits.
    # publish_generation projects these from committed entities afterwards.
    if conn is None:
        for language in ("ja", "en", "zh_tw", "zh_hans", "ko"):
            packs = build_fact_packs(entities, language=language)
            save_fact_packs(packs, factpack_path(config.store_root, language))
    return {"entities": len(entities), "terms": len(terms)}


def sync(
    config: SekaiSyncConfig,
    regions: Iterable[str],
    local_mirrors: Optional[dict[str, Path]] = None,
) -> dict:
    """Fetch region master tables and publish the derived indexes.

    Holds the store's writer lease for the whole operation: a sync is a
    read-merge-write over shared state, so a second writer interleaving it
    would let one run's baseline clobber the other's.  The lease is acquired
    *before* any connection is opened so lock ordering is uniform (Astra P13).

    Raw master tables are prepared into a **new immutable generation
    directory** and only become visible when the SQL transaction that commits
    the derived indexes also moves the active-generation pointer. Readers
    therefore see either the complete old generation or the complete new one
    (see :func:`_sync_locked`).
    """
    with store_writer_lock(config.store_root):
        return _sync_locked(config, regions, local_mirrors)


def _new_generation_id(regions: Iterable[str]) -> str:
    """A unique, sortable id for a raw generation."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def prepare_raw_generation(
    config: SekaiSyncConfig,
    regions: Iterable[str],
    local_mirrors: Optional[dict[str, Path]] = None,
) -> dict[str, Any]:
    """Download/validate every region into a new immutable generation dir.

    Nothing published so far is touched: the previous active generation keeps
    serving readers while this runs.  Returns a ``PreparedGeneration``-shaped
    dict naming the id and the regions actually prepared.

    A failure here leaves the store exactly as it was — the new directory is
    removed, the pointer never moves, the DB is not written.
    """
    generation = _new_generation_id(regions)
    target_root = generation_dir(config.store_root, generation)
    local_mirrors = local_mirrors or {}
    prepared: list[str] = []

    try:
        for region_key in regions:
            if region_key == "demo":
                continue
            # Same filesystem as the generation root, so the final rename is a
            # directory move and not a cross-device copy.
            staging = target_root.parent / f".{generation}.{region_key}.staging"
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            region_target = generation_master_dir(
                config.store_root, region_key, generation
            )
            try:
                _fetch_region_into(
                    region_key,
                    config,
                    staging,
                    local_mirror=local_mirrors.get(region_key),
                )
                region_target.parent.mkdir(parents=True, exist_ok=True)
                staging.rename(region_target)
                prepared.append(region_key)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return {
            "generation": generation,
            "regions": tuple(prepared),
            "root": target_root,
        }
    except Exception:
        shutil.rmtree(target_root, ignore_errors=True)
        raise


def _fetch_region_into(
    region_key: str,
    config: SekaiSyncConfig,
    staging: Path,
    local_mirror: Optional[Path] = None,
) -> None:
    """Materialise one region's master tables into ``staging``."""
    if local_mirror is not None:
        if not local_mirror.exists():
            raise FileNotFoundError(f"Local mirror not found: {local_mirror}")
        _assert_tree_has_no_links(local_mirror)
        shutil.copytree(local_mirror, staging, dirs_exist_ok=True)
        return

    region = REGIONS[region_key]
    repo = region.repo_slug
    if not repo:
        raise ValueError(f"Region {region_key} has no GitHub repository configured")
    url = f"{config.github_tarball_base}/{repo}/archive/refs/heads/main.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "master.tar.gz"
        download_file(url, tarball, allowed_hosts=_tarball_allowed_hosts(config))
        extract_tarball(tarball, staging)


def _rebuild_indexes_for_generation(
    config: SekaiSyncConfig,
    regions: Iterable[str],
    generation: str,
    conn: Optional[object] = None,
) -> dict:
    """Indexes built from exactly the files in ``generation``.

    Reads only the prepared generation directory, never the active one, so the
    committed indexes cannot describe a half-published tree.
    """
    return rebuild_indexes(config, regions, generation=generation, conn=conn)


def _write_freshness_for_generation(
    config: SekaiSyncConfig,
    regions: Iterable[str],
    generation: str,
    conn: Optional[object] = None,
) -> None:
    """Record the freshness report for the generation being published.

    The record is stored in the DB inside the publish transaction, because
    freshness is part of what makes the generation visible: a freshness record
    claiming a generation the pointer does not name (or the reverse) is a
    mixed-generation report. The ``cache/freshness.json`` file is written
    after COMMIT by :func:`project_publication`.
    """
    record = _build_freshness(config, regions, generation=generation, conn=conn)
    store_freshness_record(conn, record)


def project_publication(config: SekaiSyncConfig) -> dict[str, int]:
    """Re-materialise the regenerable file projections of the committed state.

    Writes ``cache/freshness.json`` from the committed SQL record and rewrites
    the JSON fact packs from the committed indexes. Idempotent and rerunnable,
    so a projection failure or a crash before the files were written is
    repaired by running this again — it never touches authoritative state.
    """
    written = 0
    if project_freshness(config) is not None:
        written += 1
    entities = dbstore.load_entities(config.store_root)
    for language in ("ja", "en", "zh_tw", "zh_hans", "ko"):
        packs = build_fact_packs(entities, language=language)
        save_fact_packs(packs, factpack_path(config.store_root, language))
        written += 1
    return {"factpacks": written, "freshness": 1 if written else 0}


def publish_generation(
    config: SekaiSyncConfig,
    prepared: Mapping[str, Any],
    all_regions: Sequence[str],
) -> dict[str, Any]:
    """Commit a prepared generation and the derived indexes atomically.

    One SQL transaction holds everything that makes the generation visible:

    - the derived indexes (registry / glossary / factpacks)
    - the per-region ``active_raw_generation`` pointer
    - the freshness record and the revision bump

    So a reader sees the old generation with old indexes, or the new
    generation with new indexes — never new indexes pointing at old raw files
    or the reverse. A failure before COMMIT changes nothing; a failure after
    COMMIT leaves a complete, internally-consistent new generation.

    The regenerable JSON projections (fact packs, ``cache/freshness.json``)
    are written only *after* COMMIT succeeds, from the committed state. A
    failure while writing them cannot make the store claim an uncommitted
    generation; :func:`project_publication` repairs the files on a rerun.
    """
    generation = str(prepared["generation"])
    prepared_regions = tuple(prepared.get("regions") or ())

    # Initialize *before* opening the publish connection. `connect()` creates
    # the database file, and an existing file with no `meta` table is then
    # classified as unusable rather than absent — so letting the publish
    # connection create the file would make its own store unopenable.
    if dbstore.inspect_schema(config.store_root).status == "absent":
        dbstore.initialize_new_store(config.store_root, target_version=3)
    else:
        dbstore.ensure_store(config.store_root)
    with dbstore.connect(config.store_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            index_stats = _rebuild_indexes_for_generation(
                config, all_regions, generation, conn=conn
            )
            pointers = dbstore.active_generations(config.store_root, conn=conn)
            # Only the regions actually prepared in this run move; syncing one
            # region must not disturb another region's pointer (Astra P13).
            for region_key in prepared_regions:
                pointers[region_key] = generation
            dbstore.set_active_generations(conn, pointers)
            _write_freshness_for_generation(
                config, all_regions, generation, conn=conn
            )
            revision = dbstore.bump_revision(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    project_publication(config)

    return {
        "generation": generation,
        "regions": prepared_regions,
        "entities": index_stats["entities"],
        "terms": index_stats["terms"],
        "revision": revision,
    }


def _sync_locked(
    config: SekaiSyncConfig,
    regions: Iterable[str],
    local_mirrors: Optional[dict[str, Path]] = None,
) -> dict:
    regions = tuple(regions)
    local_mirrors = local_mirrors or {}

    # Phase 1 — prepare a complete new generation. Nothing live is touched, so
    # a failure here needs no rollback of published state.
    prepared = prepare_raw_generation(config, regions, local_mirrors)

    all_regions: list[str] = []
    for region_key in regions:
        if region_key not in all_regions:
            all_regions.append(region_key)
    for region_key in config.regions:
        if region_key in all_regions:
            continue
        if region_key == "demo" or data_files_for_region(config.store_root, region_key):
            all_regions.append(region_key)

    # Phase 2 — one transaction commits indexes + pointer + freshness.
    published = publish_generation(config, prepared, all_regions)

    return {
        "regions": all_regions,
        "generation": published["generation"],
        "entities": published["entities"],
        "terms": published["terms"],
        "coverage": build_region_coverage(all_regions),
        "registry_db": str(dbstore.db_file(config.store_root)),
        "glossary_db": str(dbstore.db_file(config.store_root)),
    }
