"""Read-only candidate paths from an explicitly selected native state database.

This module does not discover databases, read rollouts, interpret session
membership or supply token values. The caller owns those separate policies.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from enum import Enum
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Any, Callable


@dataclass(frozen=True)
class IndexedRollout:
    thread_id: str
    path: Path


@dataclass(frozen=True)
class IndexSnapshot:
    """Complete in-scope DB candidates, not proof that every rollout is indexed or exists."""

    entries: tuple[IndexedRollout, ...]


class IndexFailure(str, Enum):
    MISSING = "missing"
    UNSUPPORTED_PROFILE = "unsupported_profile"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    DATABASE_CHANGED = "database_changed"
    CORRUPT = "corrupt"
    IO_ERROR = "io_error"
    INVALID_DATA = "invalid_data"
    BUSY = "busy"
    TIMEOUT = "timeout"
    CLOSED = "closed"


@dataclass(frozen=True)
class IndexUnavailable:
    """No usable new catalog; the consumer must retain its prior file/usage state."""

    reason: IndexFailure


class _Rejected(Exception):
    def __init__(self, reason: IndexFailure) -> None:
        self.reason = reason


class NativeStateIndex:
    """A lazy, caller-owned connection; use poll and close on the same thread.

    Both paths are explicit; no environment/home lookup or database discovery is
    performed. A cached snapshot describes the last successful DB read. File
    existence and membership must be checked by the caller before consuming it.
    The default 150 ms budget bounds lock waits and cooperatively interrupts SQL
    and Python work, but cannot preempt an individual blocking filesystem call.
    """

    def __init__(self, database: Path, sessions_dir: Path, *, timeout: float = 0.15,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.database = database.absolute()
        self.sessions_dir = sessions_dir.absolute()
        self._timeout = timeout
        self._clock = clock
        self._deadline = 0.0
        self._connection: sqlite3.Connection | None = None
        self._data_version: int | None = None
        self._snapshot: IndexSnapshot | None = None
        self._rows: list[tuple[Any, ...]] | None = None
        self._file_identity: tuple[int, int] | None = None
        self._closed = False

    def poll(self) -> IndexSnapshot | IndexUnavailable:
        """Return a complete candidate snapshot or an explicit failure, never partial rows."""
        if self._closed:
            return IndexUnavailable(IndexFailure.CLOSED)
        self._deadline = self._clock() + self._timeout
        try:
            return self._poll_catalog()
        except _Rejected as error:
            reason = error.reason
        except FileNotFoundError:
            reason = IndexFailure.MISSING
        except OSError:
            reason = IndexFailure.IO_ERROR
        except sqlite3.Error as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xFF
            # sqlite_errorcode is unavailable on Python 3.10. Messages are used
            # only for classification; paths or SQL details never leave here.
            message = str(error).lower()
            reason = (IndexFailure.BUSY if code in (5, 6) or "locked" in message
                      else IndexFailure.TIMEOUT if code == 9 or "interrupted" in message
                      else IndexFailure.CORRUPT if code in (11, 26)
                      or "not a database" in message or "malformed" in message
                      else IndexFailure.IO_ERROR)
        finally:
            if self._connection is not None:
                self._connection.set_progress_handler(None, 0)
        self._disconnect()
        return IndexUnavailable(reason)

    def _poll_catalog(self) -> IndexSnapshot:
        if self.database.name != "state_5.sqlite":
            raise _Rejected(IndexFailure.UNSUPPORTED_PROFILE)
        info = self.database.stat()
        if not stat.S_ISREG(info.st_mode):
            raise _Rejected(IndexFailure.IO_ERROR)
        identity = info.st_dev, info.st_ino
        if self._file_identity != identity:
            self._disconnect()
        if self._connection is None:
            self._connection = sqlite3.connect(
                self.database.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=self._remaining(),
            )
            self._file_identity = identity
        self._connection.set_progress_handler(lambda: self._clock() >= self._deadline, 1000)
        self._query("PRAGMA query_only=ON")
        versions = self._query("PRAGMA main.data_version")
        if len(versions) != 1 or len(versions[0]) != 1 or not isinstance(versions[0][0], int):
            raise _Rejected(IndexFailure.UNSUPPORTED_SCHEMA)
        version = versions[0][0]
        if self._snapshot is not None and version == self._data_version:
            self._verify_identity()
            self._remaining()
            return self._snapshot
        self._query("BEGIN")
        try:
            self._validate_schema()
            rows = self._query("SELECT id, rollout_path FROM main.threads ORDER BY id")
        finally:
            self._connection.set_progress_handler(None, 0)
            self._connection.rollback()
        # Usage/title commits also advance data_version. The narrow rows are
        # still read in full, but unchanged rows need no new path objects or
        # scope resolution. Scope evidence, like a version-stable snapshot, is
        # a discovery hint; the catalog independently checks actual files.
        snapshot = (self._snapshot if rows == self._rows and self._snapshot is not None
                    else IndexSnapshot(self._candidates(rows)))
        self._verify_identity()
        self._remaining()
        # Use the version observed BEFORE the read, never a newer post-read
        # version that might certify old rows after a concurrent commit.
        self._data_version, self._snapshot = version, snapshot
        self._rows = rows
        return snapshot

    def _candidates(self, rows: list[tuple[Any, ...]]) -> tuple[IndexedRollout, ...]:
        # Filesystem checks happen only after releasing the SQLite read transaction.
        try:
            root = self.sessions_dir.resolve(strict=False)
        except RuntimeError:
            raise _Rejected(IndexFailure.IO_ERROR) from None
        candidates = []
        parents: dict[str, tuple[Path, bool]] = {}
        resolved_parents = {str(self.sessions_dir): str(root)}
        lexical_prefix = str(self.sessions_dir).rstrip(os.sep) + os.sep
        resolved_prefix = str(root).rstrip(os.sep) + os.sep
        for identity, raw_path in rows:
            self._remaining()
            if (not isinstance(identity, str) or not identity or "\0" in identity
                    or not isinstance(raw_path, str) or not raw_path or "\0" in raw_path):
                raise _Rejected(IndexFailure.INVALID_DATA)
            if not os.path.isabs(raw_path) or ".." in raw_path.split(os.sep):
                raise _Rejected(IndexFailure.INVALID_DATA)
            normalized = os.path.normpath(raw_path)
            try:
                if not normalized.endswith(".jsonl") or not normalized.startswith(lexical_prefix):
                    continue
                parent, name = os.path.split(normalized)
                if parent not in parents:
                    lexical_parent = Path(parent)
                    resolved_parent = self._resolve_parent(parent, resolved_parents)
                    parent_in_scope = os.path.join(resolved_parent, "").startswith(resolved_prefix)
                    parents[parent] = (lexical_parent, parent_in_scope)
                lexical_parent, parent_in_scope = parents[parent]
                # Shared parent paths retain lexical identity; scope resolution
                # and leaf symlink checks are separate. Both parent caches are
                # local to this refresh, never cross-poll filesystem evidence.
                try:
                    is_link = stat.S_ISLNK(os.lstat(normalized).st_mode)
                except FileNotFoundError:
                    is_link = False
                in_scope = (str(Path(normalized).resolve(strict=False)).startswith(resolved_prefix)
                            if is_link else parent_in_scope)
            except RuntimeError:
                raise _Rejected(IndexFailure.INVALID_DATA) from None
            if in_scope:
                # Preserve the lexical path and its filename identity/precedence.
                candidates.append(IndexedRollout(identity, lexical_parent / name))
        return tuple(candidates)

    def _resolve_parent(self, path: str, resolved: dict[str, str]) -> str:
        # The caller admits only normalized descendants of the cached root.
        # Walk iteratively so deep paths cannot exhaust the Python call stack.
        pending, ancestor = [], path
        while ancestor not in resolved:
            self._remaining()
            pending.append(ancestor)
            ancestor = os.path.dirname(ancestor)
        for directory in reversed(pending):
            self._remaining()
            parent, name = os.path.split(directory)
            candidate = os.path.join(resolved[parent], name)
            try:
                is_link = stat.S_ISLNK(os.lstat(candidate).st_mode)
            except FileNotFoundError:
                is_link = False
            # Ordinary directory components only need one lstat per refresh.
            # Delegate links (including dangling targets and loops) to pathlib.
            resolved[directory] = (str(Path(candidate).resolve(strict=False))
                                   if is_link else candidate)
        return resolved[path]

    def _verify_identity(self) -> None:
        stat = self.database.stat()
        if (stat.st_dev, stat.st_ino) != self._file_identity:
            raise _Rejected(IndexFailure.DATABASE_CHANGED)

    def _query(self, sql: str, parameters: tuple[str, ...] = ()) -> list[tuple[Any, ...]]:
        assert self._connection is not None
        remaining = self._remaining()
        # Each statement receives only the remaining whole-poll lock budget.
        self._connection.execute(f"PRAGMA busy_timeout={int(remaining * 1000)}").close()
        with closing(self._connection.execute(sql, parameters)) as cursor:
            rows = []
            while True:
                batch = cursor.fetchmany(256)
                self._remaining()
                if not batch:
                    return rows
                rows.extend(batch)

    def _remaining(self) -> float:
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise _Rejected(IndexFailure.TIMEOUT)
        return remaining

    def _validate_schema(self) -> None:
        tables = self._query("SELECT type, sql FROM main.sqlite_master WHERE name='threads'")
        if (len(tables) != 1 or tables[0][0] != "table"
                or tables[0][1].lstrip().upper().startswith("CREATE VIRTUAL")):
            raise _Rejected(IndexFailure.UNSUPPORTED_SCHEMA)
        columns = {row[1]: row for row in self._query("PRAGMA main.table_info(threads)")}
        identity, path = columns.get("id"), columns.get("rollout_path")
        if (identity is None or path is None or identity[2].upper() != "TEXT"
                or identity[5] != 1 or sum(bool(row[5]) for row in columns.values()) != 1
                or path[2].upper() != "TEXT" or path[3] != 1):
            raise _Rejected(IndexFailure.UNSUPPORTED_SCHEMA)
        indexes = self._query("PRAGMA main.index_list(threads)")
        for index in indexes:
            if index[2] == 1 and index[3] == "pk" and index[4] == 0:
                keys = self._query("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],))
                if keys == [("id",)]:
                    return
        raise _Rejected(IndexFailure.UNSUPPORTED_SCHEMA)

    def _disconnect(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._data_version = None
        self._snapshot = None
        self._rows = None
        self._file_identity = None

    def close(self) -> None:
        """Release the owned connection permanently; repeated calls are harmless."""
        self._closed = True
        self._disconnect()
