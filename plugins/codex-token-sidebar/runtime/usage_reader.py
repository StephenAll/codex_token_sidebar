"""Incremental rollout reading and cached usage snapshots."""

from __future__ import annotations

import json
import os
import hashlib
from copy import deepcopy
import logging
from pathlib import Path
import time
from typing import Any, Callable

from usage import (
    FEATURE_TASKS, UsageStream, UsageAccumulator,
)
from credits import CreditsLedger
from rate_sync import bundled_card
import rollout_discovery
from rollout_discovery import DiscoveryResult, RolloutCatalog, _session_id_from_path, _signature as _file_signature

LOGGER = logging.getLogger("codex-token-sidebar")

class _RolloutState:
    """Byte cursor and bounded boundary samples; retains no complete message text."""

    def __init__(self, session_id: str, audit_after: float):
        self.parser = UsageStream(session_id)
        self.signature: tuple[int, ...] = ()
        self.offset = 0
        self.pending = b""
        self.provisional = False
        self.head = b""
        self.tail = b""
        self.digest = hashlib.sha256()
        self.needs_audit = rollout_discovery.WINDOWS_FILE_SIGNATURE
        self.audit_after = audit_after

    def consume(self, data: bytes) -> None:
        self.digest.update(data)
        self.offset += len(data)
        self.head = (self.head + data)[:64]
        self.tail = (self.tail + data)[-64:]
        lines = (self.pending + data).split(b"\n")
        self.pending = lines.pop()
        for line in lines:
            self.parser.feed_line(line.decode("utf-8", errors="replace"))
        self.provisional = False
        if self.pending.strip():
            text = self.pending.decode("utf-8", errors="replace")
            try:
                json.loads(text)
            except json.JSONDecodeError:
                return
            # Preserve existing support for a valid final JSON object without LF.
            # Any later growth rebuilds this exceptional case to avoid committing
            # an object that subsequently turns out to be part of a malformed line.
            self.parser.feed_line(text)
            self.pending = b""
            self.provisional = True


class UsageReader:
    def __init__(
        self, sessions_dir: Path | None = None, *,
        clock: Callable[[], float] = time.monotonic,
        discovery_interval: float = 5.0,
        audit_interval: float = 30.0,
        discoverer: RolloutCatalog | None = None,
        rate_card: dict | None = None, rate_source=None,
    ) -> None:
        self.sessions_dir = sessions_dir or (Path.home() / ".codex" / "sessions")
        self._states: dict[str, _RolloutState] = {}
        self._credits = CreditsLedger(rate_card or bundled_card())
        self._credit_sources = {}
        self.rate_source = rate_source
        self._rate_status = "bundled"
        self._contributors: tuple[dict, dict] = ({}, {})
        self._totals = (UsageAccumulator(modern=True), UsageAccumulator(modern=False))
        self._discoverer = discoverer or RolloutCatalog(
            self.sessions_dir, clock=clock, discovery_interval=discovery_interval)
        self._discovery_state: tuple | None = None
        self.discovery_result: DiscoveryResult | None = None
        self._audit_interval = max(1.0, audit_interval)
        self._clock = clock
        self._snapshot_key: Any = None
        self._snapshot: dict[str, Any] | None = None
        self._read_failed = False
        self._discovery_failed = False
        self.read_status = "waiting"

    @staticmethod
    def _signature(path: Path) -> tuple[int, ...]:
        return _file_signature(path.stat())

    def _session_paths(self, session_id: str) -> list[Path]:
        result = self._discoverer.resolve(session_id)
        self.discovery_result = result
        self._discovery_failed = not result.complete
        self._read_failed = self._read_failed or self._discovery_failed
        state = result.backend, result.complete, result.reason
        if state != self._discovery_state:
            LOGGER.info("rollout discovery backend=%s complete=%s reason=%s", *state)
            self._discovery_state = state
        return list(result.paths)

    def close(self) -> None:
        """Close the owned discovery resources on the reader's calling thread."""
        self._discoverer.close()
        if self.rate_source is not None:
            self.rate_source.close()

    @staticmethod
    def _stat_signature(stat: os.stat_result) -> tuple[int, ...]:
        return _file_signature(stat)

    def _apply_changes(self, path: str, changes: tuple[dict, dict]) -> None:
        for sources, totals, delta in zip(self._contributors, self._totals, changes):
            for key, value in delta.items():
                bucket = sources.setdefault(key, {})
                if value is None:
                    bucket.pop(path, None)
                else:
                    bucket[path] = value
                # Match the batch reader's sorted-path, last-file-wins policy.
                totals.set(key, bucket[max(bucket, key=Path)] if bucket else None)
                if not bucket:
                    sources.pop(key, None)

    def _apply_credits(self, path, changes):
        for key, value in changes.items():
            bucket = self._credit_sources.setdefault(key, {})
            if value is None: bucket.pop(path, None)
            else: bucket[path] = value
            self._credits.set(key, bucket[max(bucket, key=Path)] if bucket else None)
            if not bucket: self._credit_sources.pop(key, None)

    def set_rate_card(self, card):
        if card != self._credits.card:
            self._credits.set_card(card)
            self._credits.card = deepcopy(card)
            self._snapshot_key = None

    def _drop_file(self, path: str) -> None:
        state = self._states.pop(path, None)
        if state:
            self._apply_credits(path, {key: None for key in state.parser.credits.records})
            self._apply_changes(path, ({key: None for key in state.parser.records},
                                       {key: None for key in state.parser.events}))

    def _refresh_file(self, path: Path, expected: tuple[int, ...]) -> None:
        key = str(path)
        state = self._states.get(key)
        now = self._clock()
        audit = bool(state and state.needs_audit and now >= state.audit_after)
        if state and expected == state.signature and not audit:
            return
        reset = (state is None or state.signature[:2] != expected[:2]
                 or expected[-1] <= state.offset or state.provisional)
        # An audit with no new bytes should verify rather than reset.
        if state and expected == state.signature and audit:
            reset = False
        with path.open("rb") as handle:
            if self._stat_signature(os.fstat(handle.fileno())) != expected:
                raise OSError("Rollout changed before read")
            if reset:
                data = handle.read(expected[-1])
            elif audit:
                verified = hashlib.sha256()
                remaining = state.offset
                while remaining:
                    block = handle.read(min(65536, remaining))
                    if not block:
                        raise OSError("Rollout truncated during audit")
                    verified.update(block)
                    remaining -= len(block)
                if verified.digest() != state.digest.digest():
                    reset = True
                    handle.seek(0)
                    data = handle.read(expected[-1])
                else:
                    data = handle.read(expected[-1] - state.offset)
            else:
                head = handle.read(len(state.head))
                handle.seek(state.offset - len(state.tail))
                tail = handle.read(len(state.tail))
                if head != state.head or tail != state.tail:
                    reset = True
                    handle.seek(0)
                    data = handle.read(expected[-1])
                else:
                    handle.seek(state.offset)
                    data = handle.read(expected[-1] - state.offset)
            if len(data) != expected[-1] - (0 if reset else state.offset):
                raise OSError("Rollout truncated during read")
            # Commit neither cursor nor parser until the descriptor and path agree.
            if (self._stat_signature(os.fstat(handle.fileno())) != expected
                    or self._signature(path) != expected):
                raise OSError("Rollout changed during read")
        if reset:
            replacement = _RolloutState(_session_id_from_path(path), now + self._audit_interval)
            replacement.consume(data)
            self._drop_file(key)
            state = replacement
            self._states[key] = state
        elif data:
            state.consume(data)
            state.needs_audit = True
        if audit:
            state.needs_audit = rollout_discovery.WINDOWS_FILE_SIGNATURE
            state.audit_after = now + self._audit_interval
        state.signature = expected
        self._apply_changes(key, state.parser.drain())
        self._apply_credits(key, state.parser.credits.drain())

    def snapshot(self, session_id: str | None) -> dict[str, Any]:
        self._read_failed = False
        if self.rate_source is not None:
            card, status = self.rate_source.poll(bool(self._credits.reasons.get('no_public_model_rate')))
            self.set_rate_card(card)
            if status != self._rate_status:
                self._rate_status = status
                self._snapshot_key = None
        paths = self._session_paths(session_id or "")
        if self._discovery_failed:
            if self._snapshot is not None and self._snapshot["conversationId"] == session_id:
                self.read_status = "deferred" if self._snapshot["status"] == "ok" else "failed"
                return deepcopy(self._snapshot)
            # Do not attach a previous selection's contributions to a new ID.
            paths = []
        signatures = []
        for path in paths:
            try:
                signatures.append((str(path), self._signature(path)))
            except FileNotFoundError:
                self._drop_file(str(path))
            except OSError:
                self._read_failed = True
        key = (session_id, tuple(signatures))
        due = any(state.needs_audit and self._clock() >= state.audit_after
                  for state in self._states.values())
        if key == self._snapshot_key and self._snapshot is not None and not self._read_failed and not due:
            self.read_status = "ok" if self._snapshot["status"] == "ok" else "waiting"
            return deepcopy(self._snapshot)
        live = {str(path) for path in paths}
        for stale in set(self._states) - live:
            self._drop_file(stale)
        for name, signature in signatures:
            try:
                self._refresh_file(Path(name), signature)
            except OSError:
                self._read_failed = True
                LOGGER.debug("rollout read deferred path=%s", name, exc_info=True)
        if self._read_failed and self._snapshot is not None and self._snapshot['conversationId'] == session_id:
            self._snapshot_key = None
            self.read_status = 'deferred' if self._snapshot['status'] == 'ok' else 'failed'
            return deepcopy(self._snapshot)
        modern, legacy = self._totals
        if modern.items:
            summary = modern.report()
            by_feature = summary["by_feature"]
            request_count, turn_count = summary["request_count"], summary["turn_count"]
            event_count = 0
        else:
            summary = legacy.report()
            by_feature = ([{"feature": FEATURE_TASKS, **summary["total"], "share": 100.0}]
                          if legacy.items else [])
            request_count = turn_count = None
            event_count = len(legacy.items)
        limitations = set().union(*(state.parser.credits.limitations for state in self._states.values()))
        if (modern.items or legacy.items) and not self._credits.records:
            limitations.add('missing_response_records')
        if self._read_failed: limitations.add('read_incomplete')
        credit_report = self._credits.report(limitations)
        credit_report['rateStatus'] = self._rate_status
        credit_report['rateVerifiedOn'] = self._credits.card.get('verifiedOn')
        payload = {
            "status": "ok" if session_id and (modern.items or legacy.items) else "waiting",
            "conversationId": session_id,
            "session": summary["total"],
            "sessionCredits": credit_report,
            "sessionByFeature": by_feature,
            "sessionByModel": summary["by_model"],
            "sessionEventCount": event_count,
            "sessionRequestCount": request_count,
            "sessionTurnCount": turn_count,
        }
        payload["revision"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self._snapshot_key = None if self._read_failed else key
        self._snapshot = payload
        self.read_status = (("deferred" if payload["status"] == "ok" else "failed")
                            if self._read_failed else ("ok" if payload["status"] == "ok" else "waiting"))
        return deepcopy(payload)
