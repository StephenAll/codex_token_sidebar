"""Rollout membership and bounded refresh policy, independent of token parsing."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable

from state_index import IndexSnapshot, IndexUnavailable, NativeStateIndex

_UUID = r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
UUID_RE = re.compile(_UUID, re.IGNORECASE)
ROLLOUT_ID_RE = re.compile(r"-" + _UUID + r"\.jsonl$", re.IGNORECASE)
WINDOWS_FILE_SIGNATURE = os.name == "nt"


def _session_id_from_path(path: Path) -> str:
    match = ROLLOUT_ID_RE.search(path.name) or UUID_RE.search(path.name)
    return match.group(1) if match else path.stem


@dataclass(frozen=True)
class DiscoveryResult:
    """Complete means all checks due now succeeded, not zero reconciliation lag."""

    selected_id: str
    paths: tuple[Path, ...]
    complete: bool
    backend: str
    reason: str | None = None


@dataclass(frozen=True)
class _Metadata:
    signature: tuple[int, ...]
    filename_id: str
    key: str | None


def _signature(info: os.stat_result) -> tuple[int, ...]:
    # Windows path and descriptor stats can disagree on ctime for one unchanged file.
    stable = info.st_dev, info.st_ino, info.st_mtime_ns
    return (*stable, info.st_size) if WINDOWS_FILE_SIGNATURE else (*stable, info.st_ctime_ns, info.st_size)


def _scan(root: Path) -> set[Path]:
    try:
        info = root.stat()
    except FileNotFoundError:
        return set()
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError()

    def fail(error: OSError) -> None:
        raise error

    paths = set()
    # Unlike rglob, walk's onerror makes an unreadable subtree a failed scan,
    # never a successful partial set capable of revoking usage contributions.
    for directory, directories, files in os.walk(root, onerror=fail, followlinks=False):
        paths.update(Path(directory) / name for name in (*directories, *files)
                     if name.endswith(".jsonl"))
    return paths


class RolloutCatalog:
    """Lazy, in-memory membership; an optional index supplies candidates only."""

    def __init__(self, sessions_dir: Path, *, index: NativeStateIndex | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 discovery_interval: float = 5.0) -> None:
        self.sessions_dir = sessions_dir
        self._index = index
        self._clock = clock
        self._interval = max(1.0, discovery_interval)
        self._metadata: dict[Path, _Metadata] = {}
        self._members: dict[str, set[Path]] = {}
        self._last_scan = float("-inf")
        self._last_check = float("-inf")
        self._next_index = float("-inf")
        self._last_index = float("-inf")
        self._db_snapshot: IndexSnapshot | None = None
        self._db_paths: set[Path] = set()
        self._pending: set[Path] = set()
        self._unready: set[Path] = set()
        self._retry_paths: set[Path] = set()
        self._retry_scan = False
        self._selected = ""
        self._backend = "filesystem"
        self._index_reason: str | None = None
        self._complete = True
        self._reason: str | None = None
        self._closed = False

    def _read_metadata(self, path: Path) -> _Metadata | None:
        try:
            signature = _signature(path.stat())
        except FileNotFoundError:
            return None
        cached = self._metadata.get(path)
        if cached and cached.signature == signature:
            return cached
        key = None
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"session_meta"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    break
                if not isinstance(entry, dict):
                    raise OSError("Invalid rollout metadata")
                payload = entry.get("payload")
                if isinstance(payload, dict):
                    value = payload.get("session_id") or payload.get("id")
                    if isinstance(value, str) and value:
                        key = value
                break
            if (_signature(os.fstat(handle.fileno())) != signature
                    or _signature(path.stat()) != signature):
                raise OSError("Rollout metadata changed during read")
        return _Metadata(signature, _session_id_from_path(path), key)

    def _commit(self, updates: dict[Path, _Metadata | None]) -> None:
        for path, current in updates.items():
            old = self._metadata.get(path)
            if (current and current.key is None) or (current is None and path in self._db_paths):
                self._pending.add(path)
            else:
                self._pending.discard(path)
            if ((current and current.key is not None)
                    or (current is None and path not in self._db_paths)):
                self._unready.discard(path)
            if old == current:
                continue
            if old:
                for key in {old.filename_id, old.key} - {None}:
                    members = self._members[key]
                    members.discard(path)
                    if not members:
                        del self._members[key]
            if current:
                self._metadata[path] = current
                for key in {current.filename_id, current.key} - {None}:
                    self._members.setdefault(key, set()).add(path)
            else:
                self._metadata.pop(path, None)

    def _poll_index(self, now: float, known: bool) -> set[Path]:
        deadline = (min(self._next_index, self._last_index + 1.0)
                    if not known and self._backend == "native" else self._next_index)
        if self._index is None or now < deadline:
            return set()
        self._last_index = now
        result = self._index.poll()
        if isinstance(result, IndexUnavailable):
            self._backend, self._index_reason = "filesystem", result.reason.value
            self._next_index = now + 30.0
            return set()
        self._backend, self._index_reason = "native", None
        self._next_index = now + self._interval
        if result is self._db_snapshot:
            return set()
        paths = {entry.path for entry in result.entries}
        changed = paths ^ self._db_paths
        self._db_snapshot, self._db_paths = result, paths
        return changed

    def resolve(self, selected_id: str) -> DiscoveryResult:
        """Resolve filename OR first-metadata membership in sorted Path order."""
        if self._closed:
            return DiscoveryResult(selected_id, (), False, self._backend, "closed")
        if not selected_id:
            return DiscoveryResult(selected_id, (), True, self._backend, self._index_reason)
        now = self._clock()
        known = bool(self._members.get(selected_id))
        changed = self._poll_index(now, known)
        native = self._backend == "native"
        interval = (60.0 if native else self._interval) if known else 1.0
        scan = now - self._last_scan >= (1.0 if self._retry_scan else interval)
        check = native and (now - self._last_check >= 1.0 or selected_id != self._selected)
        if scan or check or changed:
            self._selected = selected_id
            self._last_check = now
            targets = changed | self._retry_paths
            if check:
                targets |= self._pending | self._members.get(selected_id, set())
            try:
                if scan:
                    self._last_scan = now
                    files = _scan(self.sessions_dir)
                    targets |= files | self._metadata.keys()
                    if native:
                        targets |= self._db_paths
                updates = {path: self._read_metadata(path) for path in targets}
                if scan:
                    absent = self._metadata.keys() - files - (self._db_paths if native else set())
                    updates.update((path, None) for path in absent)
            except OSError:
                self._retry_paths = targets
                self._retry_scan = scan or self._retry_scan
                self._complete, self._reason = False, "filesystem_io"
            else:
                if native:
                    self._unready.update(path for path in (changed | self._retry_paths) & self._db_paths
                                         if updates.get(path) is None and path not in self._metadata)
                else:
                    self._unready.clear()
                self._commit(updates)
                self._retry_paths.clear()
                if scan:
                    self._retry_scan = False
                    # A complete filesystem inventory confirms that an absent
                    # DB path currently contributes nothing. Keep retrying the
                    # hint, but don't let a stale row defer usage indefinitely.
                    self._unready.clear()
                elif self._unready:
                    self._retry_scan = True
                self._complete = not (self._unready or self._retry_scan)
                self._reason = ("index_path_pending" if self._unready else
                                "filesystem_io" if self._retry_scan else None)
        return DiscoveryResult(selected_id, tuple(sorted(self._members.get(selected_id, ()))),
                               self._complete, self._backend, self._reason or self._index_reason)

    def close(self) -> None:
        """Release the owned index; no persistent metadata cache is written."""
        self._closed = True
        if self._index:
            self._index.close()
