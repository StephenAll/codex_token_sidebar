"""Pure rollout parsing and token aggregation; no I/O."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
MODEL_KEYS = ("model", "model_name")
FEATURE_TASKS = "tasks"
FEATURE_SUBAGENTS = "subagents"
FEATURE_AUTO_REVIEW = "auto_review"
FEATURE_ORDER = (
    FEATURE_TASKS,
    FEATURE_SUBAGENTS,
    FEATURE_AUTO_REVIEW,
)


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return None
    return None


def _first_model(*objects: Any) -> str | None:
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for key in MODEL_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        metadata = obj.get("metadata")
        if isinstance(metadata, dict):
            value = metadata.get("model")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _raw_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None

    def pick(*keys: str) -> int:
        for key in keys:
            parsed = _number(value.get(key))
            if parsed is not None:
                return parsed
        return 0

    input_tokens = pick("input_tokens", "prompt_tokens", "input")
    cached_input_tokens = pick(
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cached_tokens",
    )
    output_tokens = pick("output_tokens", "completion_tokens", "output")
    reasoning_output_tokens = pick(
        "reasoning_output_tokens",
        "reasoning_tokens",
    )
    total_tokens = pick("total_tokens")
    if total_tokens == 0 and (
        input_tokens
        or cached_input_tokens
        or output_tokens
        or reasoning_output_tokens
    ):
        total_tokens = input_tokens + output_tokens + reasoning_output_tokens
    cached_input_tokens = min(cached_input_tokens, input_tokens)
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": total_tokens,
    }


def _subtract_usage(
    current: dict[str, int],
    previous: dict[str, int] | None,
) -> dict[str, int]:
    if previous is None:
        return current
    return {
        key: max(0, current[key] - previous.get(key, 0))
        for key in TOKEN_FIELDS
    }


@dataclass(frozen=True)
class UsageEvent:
    session_id: str
    timestamp: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int

    def dedupe_key(self) -> tuple[Any, ...]:
        return (
            self.session_id,
            self.model,
            self.timestamp,
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
            self.total_tokens,
        )


@dataclass(frozen=True)
class UsageRecord:
    """Per-response usage record with feature and model attribution."""

    session_id: str
    timestamp: str
    response_id: str
    turn_id: str
    feature: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int

    def dedupe_key(self) -> tuple[Any, ...]:
        if self.response_id:
            return (self.session_id, self.response_id)
        return (
            self.session_id,
            self.turn_id,
            self.timestamp,
            self.model,
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_output_tokens,
            self.total_tokens,
        )


def _event_from_usage(
    session_id: str,
    timestamp: Any,
    model: str | None,
    usage: dict[str, int] | None,
) -> UsageEvent | None:
    if usage is None:
        return None
    if not any(usage[key] for key in TOKEN_FIELDS):
        return None
    return UsageEvent(
        session_id=session_id,
        timestamp=str(timestamp or ""),
        model=model or "gpt-5",
        input_tokens=usage["input_tokens"],
        cached_input_tokens=usage["cached_input_tokens"],
        output_tokens=usage["output_tokens"],
        reasoning_output_tokens=usage["reasoning_output_tokens"],
        total_tokens=usage["total_tokens"],
    )


def parse_usage_text(text: str, session_id: str) -> list[UsageEvent]:
    """Parse the token_count subset of one Codex rollout JSONL file."""
    events: list[UsageEvent] = []
    previous_total: dict[str, int] | None = None
    current_model: str | None = None

    for line in text.splitlines():
        # Most rollout lines are messages, tool calls, or reasoning content.
        # Avoid JSON-decoding those large lines before checking for one of the
        # small set of usage/model markers we understand.
        if (
            '"token_count"' not in line
            and '"turn_context"' not in line
            and '"usage"' not in line
        ):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        entry_type = entry.get("type")
        payload = entry.get("payload")
        if entry_type == "turn_context" and isinstance(payload, dict):
            current_model = _first_model(payload) or current_model
            continue

        if entry_type == "event_msg" and isinstance(payload, dict):
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            total_usage = _raw_usage(info.get("total_token_usage"))
            last_usage = _raw_usage(info.get("last_token_usage"))
            usage = last_usage
            if usage is None and total_usage is not None:
                usage = _subtract_usage(total_usage, previous_total)
            if total_usage is not None:
                previous_total = total_usage
            model = _first_model(payload, info) or current_model
            if model:
                current_model = model
            event = _event_from_usage(
                session_id,
                entry.get("timestamp"),
                model,
                usage,
            )
            if event:
                events.append(event)
            continue

        # Headless Codex records can carry a usage object without event_msg.
        # Keep this narrow so ordinary response content is never counted.
        if entry_type in {"session_meta", "turn_context", "event_msg"}:
            continue
        usage = _raw_usage(entry.get("usage"))
        if usage is None:
            for key in ("data", "result", "response"):
                nested = entry.get(key)
                if isinstance(nested, dict):
                    usage = _raw_usage(nested.get("usage"))
                    if usage is not None:
                        break
        if usage is not None:
            event = _event_from_usage(
                session_id,
                entry.get("timestamp")
                or entry.get("created_at")
                or entry.get("createdAt"),
                _first_model(entry) or current_model,
                usage,
            )
            if event:
                events.append(event)

    deduped: dict[tuple[Any, ...], UsageEvent] = {}
    for event in events:
        deduped[event.dedupe_key()] = event
    return list(deduped.values())


def _feature_for_record(
    thread_source: Any,
    record: dict[str, Any],
    model: str,
) -> str:
    source = str(thread_source or "").strip().lower().replace("-", "_")
    model_key = model.strip().lower().replace("-", "_")

    if (
        "auto_review" in source
        or "guardian_review" in source
        or "auto_review" in model_key
    ):
        return FEATURE_AUTO_REVIEW
    if (
        source in {"subagent", "sub_agent"}
        or record.get("thread_id") != record.get("session_id")
        or record.get("turn_id") != record.get("root_turn_id")
    ):
        return FEATURE_SUBAGENTS
    return FEATURE_TASKS


def parse_usage_records(text: str, session_id: str) -> list[UsageRecord]:
    """Parse per-response usage records and attach feature/model metadata."""
    model_by_turn: dict[str, str] = {}
    thread_source: Any = None
    raw_records: list[tuple[str, dict[str, Any]]] = []

    for line in text.splitlines():
        if (
            '"token_usage_record"' not in line
            and '"turn_context"' not in line
            and '"session_meta"' not in line
        ):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue

        entry_type = entry.get("type")
        if entry_type == "session_meta":
            thread_source = payload.get("thread_source") or payload.get("source")
        elif entry_type == "turn_context":
            turn_id = payload.get("turn_id")
            model = _first_model(payload)
            if isinstance(turn_id, str) and model:
                model_by_turn[turn_id] = model
        elif entry_type == "token_usage_record":
            raw_records.append((str(entry.get("timestamp") or ""), payload))

    records: dict[tuple[Any, ...], UsageRecord] = {}
    for timestamp, payload in raw_records:
        usage = _raw_usage(payload.get("usage"))
        if usage is None:
            continue
        turn_id = str(payload.get("turn_id") or "")
        root_turn_id = str(payload.get("root_turn_id") or "")
        model = (
            model_by_turn.get(turn_id)
            or model_by_turn.get(root_turn_id)
            or "unknown"
        )
        record = UsageRecord(
            session_id=session_id,
            timestamp=timestamp,
            response_id=str(payload.get("response_id") or ""),
            turn_id=turn_id,
            feature=_feature_for_record(thread_source, {
                "session_id": str(payload.get("session_id") or session_id),
                "thread_id": str(payload.get("thread_id") or ""),
                "turn_id": turn_id,
                "root_turn_id": root_turn_id,
            }, model),
            model=model,
            input_tokens=usage["input_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            output_tokens=usage["output_tokens"],
            reasoning_output_tokens=usage["reasoning_output_tokens"],
            total_tokens=usage["total_tokens"],
        )
        records[record.dedupe_key()] = record
    return list(records.values())


def _empty_totals() -> dict[str, int]:
    return {key: 0 for key in TOKEN_FIELDS}


def _add_totals(target: dict[str, int], event: UsageEvent | UsageRecord) -> None:
    target["input_tokens"] += event.input_tokens
    target["cached_input_tokens"] += event.cached_input_tokens
    target["output_tokens"] += event.output_tokens
    target["reasoning_output_tokens"] += event.reasoning_output_tokens
    target["total_tokens"] += event.total_tokens


def _rows_by_key(
    groups: dict[str, dict[str, int]],
    key_name: str,
    total_tokens: int,
    include_cache_hit_rate: bool = False,
) -> list[dict[str, Any]]:
    rows = []
    for key, values in sorted(
        groups.items(),
        key=lambda item: (-item[1]["total_tokens"], item[0]),
    ):
        share = (
            round(values["total_tokens"] * 100 / total_tokens, 1)
            if total_tokens
            else 0.0
        )
        row: dict[str, Any] = {
            key_name: key,
            **values,
            "share": share,
        }
        if include_cache_hit_rate:
            input_tokens = values["input_tokens"]
            cached_input_tokens = values["cached_input_tokens"]
            row["cache_hit_rate"] = (
                round(cached_input_tokens * 100 / input_tokens, 1)
                if input_tokens
                else 0.0
            )
        rows.append(row)
    return rows


def aggregate_usage_records(records: Iterable[UsageRecord]) -> dict[str, Any]:
    total = _empty_totals()
    by_model: dict[str, dict[str, int]] = {}
    by_feature: dict[str, dict[str, int]] = {}
    model_request_ids: dict[str, set[str]] = {}
    materialized = list(records)
    for record in materialized:
        _add_totals(total, record)
        _add_totals(by_model.setdefault(record.model, _empty_totals()), record)
        _add_totals(by_feature.setdefault(record.feature, _empty_totals()), record)
        if record.response_id:
            model_request_ids.setdefault(record.model, set()).add(record.response_id)
    for feature in FEATURE_ORDER:
        by_feature.setdefault(feature, _empty_totals())
    model_rows = _rows_by_key(
        by_model,
        "model",
        total["total_tokens"],
        include_cache_hit_rate=True,
    )
    for row in model_rows:
        model = row["model"]
        row["request_count"] = len(model_request_ids.get(model, set()))
    return {
        "total": total,
        "by_model": model_rows,
        "by_feature": _rows_by_key(
            by_feature,
            "feature",
            total["total_tokens"],
        ),
        "request_count": len({record.response_id for record in materialized if record.response_id}),
        "turn_count": len({record.turn_id for record in materialized if record.turn_id}),
    }


def aggregate_events(events: Iterable[UsageEvent]) -> dict[str, Any]:
    total = _empty_totals()
    by_model: dict[str, dict[str, int]] = {}
    for event in events:
        _add_totals(total, event)
        row = by_model.setdefault(event.model, _empty_totals())
        _add_totals(row, event)
    models = _rows_by_key(
        by_model,
        "model",
        total["total_tokens"],
        include_cache_hit_rate=True,
    )
    for row in models:
        row["request_count"] = None
    return {"total": total, "by_model": models}


class UsageAccumulator:
    """Replaceable contributions; report cost depends on groups, not record count."""

    def __init__(self, *, modern: bool) -> None:
        self.modern = modern
        self.items: dict[tuple[Any, ...], UsageRecord | UsageEvent] = {}
        self.total = _empty_totals()
        self.models: dict[str, dict[str, int]] = {}
        self.features: dict[str, dict[str, int]] = {}
        self.model_sizes: Counter = Counter()
        self.requests: Counter = Counter()
        self.turns: Counter = Counter()
        self.model_requests: dict[str, Counter] = {}

    @staticmethod
    def _count(counter: Counter, key: str, delta: int) -> None:
        if key:
            counter[key] += delta
            if not counter[key]:
                del counter[key]

    def _adjust(self, item: UsageRecord | UsageEvent, delta: int) -> None:
        groups = [self.total, self.models.setdefault(item.model, _empty_totals())]
        if isinstance(item, UsageRecord):
            groups.append(self.features.setdefault(item.feature, _empty_totals()))
            self._count(self.requests, item.response_id, delta)
            self._count(self.turns, item.turn_id, delta)
            self._count(self.model_requests.setdefault(item.model, Counter()), item.response_id, delta)
        for group in groups:
            for field in TOKEN_FIELDS:
                group[field] += getattr(item, field) * delta
        self._count(self.model_sizes, item.model, delta)
        if item.model not in self.model_sizes:
            del self.models[item.model]
            self.model_requests.pop(item.model, None)

    def set(self, key: tuple[Any, ...], item: UsageRecord | UsageEvent | None) -> None:
        previous = self.items.get(key)
        if previous == item:
            return
        if previous is not None:
            self._adjust(previous, -1)
            del self.items[key]
        if item is not None:
            self.items[key] = item
            self._adjust(item, 1)

    def report(self) -> dict[str, Any]:
        models = _rows_by_key(self.models, "model", self.total["total_tokens"], True)
        for row in models:
            row["request_count"] = len(self.model_requests.get(row["model"], {})) if self.modern else None
        result = {"total": dict(self.total), "by_model": models}
        if self.modern:
            features = {key: self.features.get(key, _empty_totals()) for key in FEATURE_ORDER}
            result.update(by_feature=_rows_by_key(features, "feature", self.total["total_tokens"]),
                          request_count=len(self.requests), turn_count=len(self.turns))
        return result


class UsageStream:
    """Line-oriented pure state; emits replacements/removals for changed keys.

    Modern model/source metadata applies to the whole file. Keep the raw usage
    contenders needed to reattribute anonymous records when their keys diverge
    or converge after later metadata. Legacy cumulative state stays sequential.
    Ordinary message bodies are never retained.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.credits = CreditEvidence(session_id)
        self.records: dict[tuple[Any, ...], UsageRecord] = {}
        self.events: dict[tuple[Any, ...], UsageEvent] = {}
        self._models: dict[str, str] = {}
        self._source: Any = None
        self._raw: dict[tuple[Any, ...], tuple[int, str, dict[str, Any]]] = {}
        self._dependencies: dict[str, set] = defaultdict(set)
        self._attributed: dict[tuple[Any, ...], UsageRecord] = {}
        self._buckets: dict[tuple[Any, ...], dict] = {}
        self._sequence = 0
        self._legacy_model: str | None = None
        self._legacy_total: dict[str, int] | None = None
        self._record_changes: dict[tuple[Any, ...], UsageRecord | None] = {}
        self._event_changes: dict[tuple[Any, ...], UsageEvent | None] = {}

    def _winner(self, key: tuple[Any, ...]) -> None:
        bucket = self._buckets.get(key, {})
        winner = max(bucket.values(), key=lambda pair: pair[0])[1] if bucket else None
        if self.records.get(key) != winner:
            self._record_changes[key] = winner
            if winner is None:
                self.records.pop(key, None)
            else:
                self.records[key] = winner
        if not bucket:
            self._buckets.pop(key, None)

    def _attribute(self, raw_key: tuple[Any, ...]) -> None:
        previous = self._attributed.get(raw_key)
        if previous is not None:
            key = previous.dedupe_key()
            self._buckets[key].pop(raw_key)
            self._winner(key)
        sequence, timestamp, payload = self._raw[raw_key]
        model = self._models.get(payload["turn_id"]) or self._models.get(payload["root_turn_id"]) or "unknown"
        record = UsageRecord(
            session_id=self.session_id, timestamp=timestamp, response_id=payload["response_id"],
            turn_id=payload["turn_id"], model=model,
            feature=_feature_for_record(self._source, payload, model), **payload["usage"])
        self._attributed[raw_key] = record
        key = record.dedupe_key()
        self._buckets.setdefault(key, {})[raw_key] = (sequence, record)
        self._winner(key)

    def _modern_record(self, timestamp: str, payload: dict[str, Any]) -> None:
        usage = _raw_usage(payload.get("usage"))
        if usage is None:
            return
        normalized = {key: str(payload.get(key) or "") for key in (
            "response_id", "turn_id", "root_turn_id", "thread_id")}
        normalized["session_id"] = str(payload.get("session_id") or self.session_id)
        normalized["usage"] = usage
        raw_key = (("response", normalized["response_id"]) if normalized["response_id"] else
                   ("anonymous", timestamp, json.dumps(normalized, sort_keys=True)))
        previous = self._raw.get(raw_key)
        if previous:
            for turn in {previous[2]["turn_id"], previous[2]["root_turn_id"]}:
                self._dependencies[turn].discard(raw_key)
                if not self._dependencies[turn]:
                    del self._dependencies[turn]
        self._sequence += 1
        self._raw[raw_key] = (self._sequence, timestamp, normalized)
        for turn in {normalized["turn_id"], normalized["root_turn_id"]}:
            self._dependencies[turn].add(raw_key)
        self._attribute(raw_key)

    def feed_line(self, line: str) -> None:
        if not any(marker in line for marker in (
                '"token_usage_record"', '"session_meta"', '"turn_context"', '"token_count"', '"usage"', '"thread_settings_applied"',
                '"history_base"', '"image_generation', '"realtime', '"audio')):
            return
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            if '"token_usage_record"' in line:
                self.credits.limitations.add("malformed_usage_record")
            return
        if not isinstance(entry, dict):
            return
        self.credits.feed(entry)
        kind, payload = entry.get("type"), entry.get("payload")
        if kind == "session_meta" and isinstance(payload, dict):
            source = payload.get("thread_source") or payload.get("source")
            if source != self._source:
                self._source = source
                for key in self._raw:
                    self._attribute(key)
            return
        if kind == "turn_context" and isinstance(payload, dict):
            model = _first_model(payload)
            self._legacy_model = model or self._legacy_model
            turn = payload.get("turn_id")
            if isinstance(turn, str) and model and self._models.get(turn) != model:
                self._models[turn] = model
                for key in self._dependencies.get(turn, ()):
                    self._attribute(key)
            return
        if kind == "token_usage_record" and isinstance(payload, dict):
            self._modern_record(str(entry.get("timestamp") or ""), payload)
            if not any(key in entry for key in ("usage", "data", "result", "response")):
                return

        # Reuse the batch legacy parser on one line plus a zero-contribution
        # cursor prelude. Its alias handling and headless formats stay canonical.
        prefix = json.dumps({"type": "turn_context", "payload": {"model": self._legacy_model}}) + "\n"
        if self._legacy_total is not None:
            prefix += json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": self._legacy_total, "last_token_usage": {}}}}) + "\n"
        for event in parse_usage_text(prefix + line, self.session_id):
            key = event.dedupe_key()
            if self.events.get(key) != event:
                self.events[key] = event
                self._event_changes[key] = event
        if kind == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                total = _raw_usage(info.get("total_token_usage"))
                if total is not None:
                    self._legacy_total = total
                self._legacy_model = _first_model(payload, info) or self._legacy_model

    def drain(self) -> tuple[dict, dict]:
        changes = self._record_changes, self._event_changes
        self._record_changes, self._event_changes = {}, {}
        return changes


class CreditEvidence:
    """Retain only pricing evidence, with chronological tiers and turn-local models."""
    def __init__(self, session_id):
        self.session_id = session_id
        self.execution_id = session_id
        self.provider = None
        self.tier = None
        self.active_turn = None
        self.models = defaultdict(set)
        self.tiers = defaultdict(set)
        self.dependencies = defaultdict(set)
        self.raw = {}
        self.records = {}
        self.changes = {}
        self.limitations = set()
        self.sequence = 0
        self.thread_totals = Counter()
        self.cumulative = {}

    @staticmethod
    def _tier(value):
        if not isinstance(value, str): return None
        return {'default': 'standard', 'priority': 'fast'}.get(value, value)

    def _attribute(self, key):
        raw = self.raw[key]
        turn = raw['turn']
        models = self.models[turn] if key[0] == self.execution_id else set()
        record = {k: v for k, v in raw.items() if k != 'turn'}
        record.update(provider=self.provider,
                      model=next(iter(models)) if len(models) == 1 else None,
                      modelAmbiguous=len(models) > 1,
                      tierAmbiguous=len(self.tiers[turn]) > 1)
        if record != self.records.get(key):
            self.records[key] = record
            self.changes[key] = record

    def _revisit(self, turn):
        for key in self.dependencies[turn]: self._attribute(key)

    def feed(self, entry):
        kind, payload = entry.get('type'), entry.get('payload')
        if not isinstance(payload, dict): return
        if kind == 'session_meta':
            identity = payload.get('id')
            if isinstance(identity, str) and identity:
                self.execution_id = identity
            provider = payload.get('model_provider')
            if isinstance(provider, str) and provider != self.provider:
                self.provider = provider
                for key in self.raw: self._attribute(key)
        elif kind == 'turn_context':
            turn = payload.get('turn_id')
            self.active_turn = turn if isinstance(turn, str) and turn else None
            if self.active_turn:
                model = _first_model(payload)
                if model: self.models[turn].add(model)
                if 'service_tier' in payload: self.tier = self._tier(payload['service_tier'])
                if self.tier is not None: self.tiers[turn].add(self.tier)
                self._revisit(turn)
            if payload.get('realtime_active'): self.limitations.add('unsupported_media')
        elif kind == 'event_msg' and payload.get('type') == 'thread_settings_applied':
            settings = payload.get('thread_settings')
            self.tier = self._tier(settings.get('service_tier')) if isinstance(settings, dict) else None
            # Only tiers observed at a response or context belong to that turn.
        elif kind == 'token_usage_record':
            self.sequence += 1
            thread, response = payload.get('thread_id'), payload.get('response_id')
            identity = all(isinstance(v, str) and v for v in (thread, response))
            key = (thread, response) if identity else ('anonymous', self.session_id, self.sequence)
            turn = payload.get('turn_id')
            turn = turn if isinstance(turn, str) and turn else None
            if key in self.raw:
                self.dependencies[self.raw[key]['turn']].discard(key)
                old_usage = self.raw[key].get('usage') or {}
                old_total = old_usage.get('total_tokens')
                if identity and type(old_total) is int and old_total >= 0:
                    self.thread_totals[thread] -= old_total
            tier = self._tier(payload['service_tier']) if 'service_tier' in payload else self.tier
            old_tiers = len(self.tiers[turn])
            if tier is not None: self.tiers[turn].add(tier)
            usage = payload.get('usage')
            self.raw[key] = {'recordKey': key, 'hasResponseIdentity': identity, 'turn': turn,
                             'serviceTier': tier, 'usage': {k: usage[k] for k in (
                                 *TOKEN_FIELDS, 'cache_write_input_tokens') if k in usage}
                             if isinstance(usage, dict) else None}
            total = usage.get('total_tokens') if isinstance(usage, dict) else None
            if identity and type(total) is int and total >= 0:
                self.thread_totals[thread] += total
            cumulative = payload.get('thread_token_usage')
            cumulative = cumulative.get('total_tokens') if isinstance(cumulative, dict) else None
            if identity and type(cumulative) is int and cumulative >= 0:
                self.cumulative[thread] = cumulative
            if any(total > self.thread_totals[t] for t, total in self.cumulative.items()):
                self.limitations.add('history_detail_missing')
            else:
                self.limitations.discard('history_detail_missing')
            self.dependencies[turn].add(key)
            if len(self.tiers[turn]) > 1 and len(self.tiers[turn]) != old_tiers:
                self._revisit(turn)
            else: self._attribute(key)
        if kind == 'history_base' or payload.get('history_base') is not None:
            self.limitations.add('history_scope_unconfirmed')
        subtype = str(payload.get('type') or '')
        if any(part in subtype for part in ('image_generation', 'realtime', 'audio')):
            self.limitations.add('unsupported_media')

    def drain(self):
        changes, self.changes = self.changes, {}
        return changes
