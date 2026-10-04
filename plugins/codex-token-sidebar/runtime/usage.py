"""Pure rollout parsing and token aggregation; no I/O."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
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


def _identifier(value):
    return value if isinstance(value, str) and value else ""


def _model_evidence(models, turn, root_turn, *, owned=True, explicit=None):
    if explicit:
        return explicit, False
    candidates = (models.get(turn) or models.get(root_turn) or set()) if owned else set()
    return (next(iter(candidates)) if len(candidates) == 1 else "unknown", len(candidates) > 1)


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
    if "total_tokens" not in value:
        total_tokens = input_tokens + output_tokens
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


def _legacy_usage(current, previous, last):
    """Return observed contribution and any uncertainty in cumulative evidence."""
    if current is None or previous is None:
        return (last if last is not None else current), None
    delta = {key: current[key] - previous[key] for key in TOKEN_FIELDS}
    if not any(delta.values()):
        return _empty_totals(), None
    if all(value >= 0 for value in delta.values()):
        inconsistent = last is not None and any(last[key] > delta[key] for key in TOKEN_FIELDS)
        return delta, "legacy_cumulative_inconsistent" if inconsistent else None
    if all(value <= 0 for value in delta.values()) and last == current:
        return current, None
    return (last if last is not None else _empty_totals()), "legacy_cumulative_inconsistent"


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
    observation: tuple = ()
    feature: str = FEATURE_TASKS

    def dedupe_key(self) -> tuple[Any, ...]:
        return (
            self.session_id,
            self.model,
            self.observation or self.timestamp,
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
    metadata_owned: bool = True
    identity_complete: bool = True
    local_identity: tuple = ()
    model_ambiguous: bool = False
    feature_known: bool = True

    def dedupe_key(self) -> tuple[Any, ...]:
        if self.response_id:
            return (self.session_id, self.response_id)
        if self.local_identity:
            return self.local_identity
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
        model=model or "unknown",
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
    generation = 0
    thread_source = None

    for line in text.splitlines():
        # Most rollout lines are messages, tool calls, or reasoning content.
        # Avoid JSON-decoding those large lines before checking for one of the
        # small set of usage/model markers we understand.
        if (
            '"token_count"' not in line
            and '"turn_context"' not in line
            and '"usage"' not in line
            and '"session_meta"' not in line
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
        if entry_type == "session_meta" and isinstance(payload, dict):
            session_id = _identifier(payload.get("id")) or session_id
            thread_source = payload.get("thread_source") or payload.get("source")
            continue
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
            usage, _ = _legacy_usage(total_usage, previous_total, last_usage)
            if (total_usage is not None and previous_total is not None
                    and total_usage["total_tokens"] < previous_total["total_tokens"]):
                generation += 1
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
                event = replace(event, feature=_feature_for_record(thread_source, {}, event.model))
                if total_usage is not None:
                    event = replace(event, observation=(generation, *(total_usage[k] for k in TOKEN_FIELDS)))
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
                event = replace(event, feature=_feature_for_record(thread_source, {}, event.model))
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


def parse_usage_records(text: str, session_id: str, *, source_id: str | None = None) -> list[UsageRecord]:
    """Parse per-response usage records and attach feature/model metadata."""
    model_by_turn: dict[str, set[str]] = defaultdict(set)
    thread_source: Any = None
    execution_id = session_id
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
            if isinstance(payload.get("id"), str) and payload["id"]:
                execution_id = payload["id"]
        elif entry_type == "turn_context":
            turn_id = payload.get("turn_id")
            model = _first_model(payload)
            if isinstance(turn_id, str) and model:
                model_by_turn[turn_id].add(model)
        elif entry_type == "token_usage_record":
            raw_records.append((str(entry.get("timestamp") or ""), payload))

    records: dict[tuple[Any, ...], UsageRecord] = {}
    for sequence, (timestamp, payload) in enumerate(raw_records, 1):
        usage = _raw_usage(payload.get("usage"))
        if usage is None:
            continue
        turn_id = str(payload.get("turn_id") or "")
        root_turn_id = str(payload.get("root_turn_id") or "")
        thread_id = _identifier(payload.get("thread_id"))
        owned = not thread_id or thread_id == execution_id
        model, ambiguous = _model_evidence(model_by_turn, turn_id, root_turn_id,
                                   owned=owned, explicit=_first_model(payload))
        record = UsageRecord(
            session_id=thread_id or execution_id,
            timestamp=timestamp,
            response_id=_identifier(payload.get("response_id")),
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
            metadata_owned=owned,
            identity_complete=bool(thread_id and _identifier(payload.get("response_id"))),
            local_identity=("anonymous", source_id or session_id, sequence),
            model_ambiguous=ambiguous, feature_known=thread_source is not None,
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


def merge_usage_sources(records: Iterable[UsageRecord]) -> UsageRecord | None:
    """Merge evidence for one response; missing metadata is not a retraction."""
    records = list(records)
    if not records:
        return None
    owned = [record for record in records if record.metadata_owned]
    candidates = owned or records
    winner = candidates[-1]
    models = {record.model for record in candidates if record.model != "unknown"}
    ambiguous = len(models) > 1 or any(record.model_ambiguous for record in candidates)
    feature = next((record.feature for record in reversed(candidates) if record.feature_known), winner.feature)
    return replace(winner, model=next(iter(models)) if len(models) == 1 and not ambiguous else "unknown",
                   model_ambiguous=ambiguous, feature=feature,
                   feature_known=any(record.feature_known for record in candidates),
                   identity_complete=any(record.identity_complete for record in candidates))


def aggregate_usage_records(records: Iterable[UsageRecord]) -> dict[str, Any]:
    total = _empty_totals()
    by_model: dict[str, dict[str, int]] = {}
    by_feature: dict[str, dict[str, int]] = {}
    model_request_ids: dict[str, set[str]] = {}
    selected = defaultdict(list)
    for record in records:
        selected[record.dedupe_key()].append(record)
    materialized = [merge_usage_sources(bucket) for bucket in selected.values()]
    for record in materialized:
        _add_totals(total, record)
        _add_totals(by_model.setdefault(record.model, _empty_totals()), record)
        _add_totals(by_feature.setdefault(record.feature, _empty_totals()), record)
        if record.response_id:
            model_request_ids.setdefault(record.model, set()).add(record.dedupe_key())
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
        "request_count": len({record.dedupe_key() for record in materialized if record.response_id}),
        "turn_count": len({(record.session_id, record.turn_id) for record in materialized if record.turn_id}),
    }


def aggregate_events(events: Iterable[UsageEvent]) -> dict[str, Any]:
    total = _empty_totals()
    by_model: dict[str, dict[str, int]] = {}
    features = {}
    for event in events:
        _add_totals(total, event)
        row = by_model.setdefault(event.model, _empty_totals())
        _add_totals(row, event)
        _add_totals(features.setdefault(event.feature, _empty_totals()), event)
    models = _rows_by_key(
        by_model,
        "model",
        total["total_tokens"],
        include_cache_hit_rate=True,
    )
    for row in models:
        row["request_count"] = None
    result = {"total": total, "by_model": models}
    if features:
        result["by_feature"] = _rows_by_key(features, "feature", total["total_tokens"])
    return result


class UsageAccumulator:
    """Replaceable contributions; report cost depends on groups, not record count."""

    def __init__(self, *, modern: bool) -> None:
        self.modern = modern
        self.incomplete_identities = 0
        self.items: dict[tuple[Any, ...], UsageRecord | UsageEvent] = {}
        self.total = _empty_totals()
        self.models: dict[str, dict[str, int]] = {}
        self.features: dict[str, dict[str, int]] = {}
        self.feature_sizes = Counter()
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
        groups = [self.total, self.models.setdefault(item.model, _empty_totals()),
                  self.features.setdefault(item.feature, _empty_totals())]
        if isinstance(item, UsageRecord):
            self.incomplete_identities += delta * (not item.identity_complete)
            request = item.dedupe_key() if item.response_id else None
            turn = (item.session_id, item.turn_id) if item.turn_id else None
            self._count(self.requests, request, delta)
            self._count(self.turns, turn, delta)
            self._count(self.model_requests.setdefault(item.model, Counter()), request, delta)
        for group in groups:
            for field in TOKEN_FIELDS:
                group[field] += getattr(item, field) * delta
        self._count(self.feature_sizes, item.feature, delta)
        if item.feature not in self.feature_sizes:
            self.features.pop(item.feature, None)
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
        elif self.items:
            result["by_feature"] = _rows_by_key(self.features, "feature", self.total["total_tokens"])
        return result


class UsageByThread:
    """Index replaceable contributions; only execution threads own counters."""

    def __init__(self, *, modern: bool) -> None:
        self.modern = modern
        self.threads: dict[str, UsageAccumulator] = {}
        self._owners: dict[tuple, str] = {}

    def set(self, key: tuple[Any, ...], item: UsageRecord | UsageEvent | None) -> None:
        previous = self._owners.get(key)
        owner = item.session_id if item is not None else None
        if previous is not None and previous != owner:
            group = self.threads[previous]
            group.set(key, None)
            if not group.items:
                del self.threads[previous]
            del self._owners[key]
        if item is not None:
            if owner not in self.threads:
                self.threads[owner] = UsageAccumulator(modern=self.modern)
            self.threads[owner].set(key, item)
            self._owners[key] = owner


def summarize_coverage(modern: UsageByThread, legacy: UsageByThread, *, scopes=()) -> dict:
    """Select overlapping formats per execution, then combine disjoint threads."""
    total, models, features = _empty_totals(), {}, {}
    requests = turns = events = 0
    model_requests = {}
    usage_limits, credit_limits = set(), set()
    evidence = defaultdict(list)
    for scope in scopes:
        evidence[scope.execution_id].append(scope)
    for thread in modern.threads.keys() | legacy.threads.keys():
        current, old = modern.threads.get(thread), legacy.threads.get(thread)
        group = current or old
        report = group.report()
        usage_limits.update(source_scope_reasons(thread, current, evidence[thread], legacy=old))
        if current:
            if current.incomplete_identities:
                usage_limits.add("response_identity_missing")
                requests = turns = None
            if requests is not None:
                requests += report["request_count"]
                turns += report["turn_count"]
        else:
            requests = turns = None
            events += len(old.items)
            credit_limits.add("missing_response_records")
        for field in TOKEN_FIELDS:
            total[field] += report["total"][field]
        for row in report["by_model"]:
            target = models.setdefault(row["model"], _empty_totals())
            for field in TOKEN_FIELDS:
                target[field] += row[field]
            count = None if current and current.incomplete_identities else row["request_count"]
            previous = model_requests.get(row["model"], 0)
            model_requests[row["model"]] = None if count is None or previous is None else previous + count
        rows = report.get("by_feature", [{"feature": FEATURE_TASKS, **report["total"]}])
        for row in rows:
            target = features.setdefault(row["feature"], _empty_totals())
            for field in TOKEN_FIELDS:
                target[field] += row[field]
    model_rows = _rows_by_key(models, "model", total["total_tokens"], True)
    for row in model_rows:
        row["request_count"] = model_requests[row["model"]]
    if modern.threads:
        for feature in FEATURE_ORDER:
            features.setdefault(feature, _empty_totals())
    else:
        requests = turns = None
    return {"total": total, "by_model": model_rows,
            "by_feature": _rows_by_key(features, "feature", total["total_tokens"]),
            "request_count": requests, "turn_count": turns, "event_count": events,
            "usage_limits": usage_limits, "credit_limits": credit_limits,
            "completeness": {
                "status": ("partial" if usage_limits & {"modern_cumulative_inconsistent", "legacy_cumulative_inconsistent"}
                           else "unknown" if usage_limits else "complete"),
                "reasons": sorted(usage_limits)}}


class SourceScope:
    """Source-local endpoints; response values remain owned by the canonical ledger.

    A checkpoint describes this format's cumulative scope, never the other
    format's counter. Only metadata and endpoint evidence are retained here.
    """
    def __init__(self, execution_id):
        self.execution_id = execution_id
        self.turn = None
        self.has_header = False
        self.has_modern = False
        self.anchored = False
        self.checkpoint = None
        self.endpoint = None
        self.legacy_turns = set()
        self.previous_legacy = None
        self.legacy_limitations = set()
        self.history_unknown = False

    @staticmethod
    def vector(usage):
        if (not isinstance(usage, dict)
                or any(type(usage.get(key)) is not int or usage[key] < 0 for key in TOKEN_FIELDS)):
            return None
        return tuple(usage[key] for key in TOKEN_FIELDS)

    def feed(self, entry):
        kind, payload = entry.get("type"), entry.get("payload")
        if not isinstance(payload, dict):
            return
        if kind == "session_meta":
            identity = _identifier(payload.get("id"))
            if identity:
                if identity != self.execution_id and (self.has_modern or self.legacy_turns):
                    self.history_unknown = True
                self.execution_id = identity
                self.has_header = True
        elif kind == "turn_context":
            self.turn = _identifier(payload.get("turn_id"))
        elif kind == "token_usage_record" and payload.get("thread_id") == self.execution_id:
            usage = self.vector(payload.get("usage"))
            checkpoint = self.vector(payload.get("thread_token_usage"))
            response = _identifier(payload.get("response_id"))
            if not self.has_modern:
                self.anchored = bool(self.has_header and response and usage is not None and usage == checkpoint)
            self.has_modern = True
            # Replayed earlier responses do not move a cumulative endpoint back.
            # A revision of the endpoint itself replaces its previous evidence.
            if (self.checkpoint is None or checkpoint is None
                    or checkpoint[-1] >= self.checkpoint[-1]
                    or response == self.endpoint[0]):
                self.checkpoint = checkpoint
                self.endpoint = (response, usage)
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                total = _raw_usage(info.get("total_token_usage"))
                _, limitation = _legacy_usage(total, self.previous_legacy, _raw_usage(info.get("last_token_usage")))
                if limitation:
                    self.legacy_limitations.add(limitation)
                self.previous_legacy = total if total is not None else self.previous_legacy
        if kind == "history_base" or payload.get("history_base") is not None:
            self.history_unknown = True


def source_scope_reasons(thread, current, scopes, *, legacy=None):
    """Validate the selected ledger against its own observed endpoints."""
    if current is None:
        reasons = set().union(*(scope.legacy_limitations for scope in scopes))
        if not scopes or any(not scope.has_header or scope.history_unknown for scope in scopes):
            reasons.add("history_scope_unconfirmed")
        checkpoints = [scope.previous_legacy for scope in scopes if scope.previous_legacy is not None]
        if not checkpoints:
            reasons.add("checkpoint_missing")
        elif not reasons and max(checkpoints, key=lambda total: total["total_tokens"]) != legacy.total:
            reasons.add("history_scope_unconfirmed")
        return reasons
    reasons = set()
    owned = [scope for scope in scopes if scope.has_modern]
    if not owned or any(scope.history_unknown for scope in scopes):
        reasons.add("history_scope_unconfirmed")
    for scope in scopes:
        if any(not turn or (thread, turn) not in current.turns for turn in scope.legacy_turns):
            reasons.add("legacy_only_turns")
    checkpoints = [scope for scope in owned if scope.checkpoint is not None]
    if not checkpoints:
        reasons.add("checkpoint_missing")
    if checkpoints:
        # Cumulative checkpoints are monotonic within an execution. Older
        # retained copies cannot override the furthest observed endpoint.
        latest = max(checkpoints, key=lambda scope: scope.checkpoint[-1])
        endpoint = current.items.get((thread, latest.endpoint[0]))
        values = tuple(getattr(endpoint, key) for key in TOKEN_FIELDS) if endpoint else None
        closed = latest.checkpoint == tuple(current.total[key] for key in TOKEN_FIELDS)
        if values != latest.endpoint[1]:
            reasons.add("source_conflict")
        elif not closed:
            reasons.add("modern_cumulative_inconsistent" if latest.anchored else "history_scope_unconfirmed")
        if not any(scope.anchored for scope in owned) and not (latest.has_header and closed):
            reasons.add("history_scope_unconfirmed")
    return reasons


class UsageStream:
    """Line-oriented pure state; emits replacements/removals for changed keys.

    Modern model/source metadata applies to the whole file. Keep the raw usage
    contenders needed to reattribute anonymous records when their keys diverge
    or converge after later metadata. Legacy cumulative state stays sequential.
    Ordinary message bodies are never retained.
    """

    def __init__(self, session_id: str, *, source_id: str | None = None) -> None:
        self.session_id = session_id
        self.source_id = source_id or session_id
        self.execution_id = session_id
        self.credits = CreditEvidence()
        self.scope = SourceScope(session_id)
        self.records: dict[tuple[Any, ...], UsageRecord] = {}
        self.events: dict[tuple[Any, ...], UsageEvent] = {}
        self._models: dict[str, set[str]] = defaultdict(set)
        self._source: Any = None
        self._raw: dict[tuple[Any, ...], tuple[int, str, dict[str, Any]]] = {}
        self._dependencies: dict[str, set] = defaultdict(set)
        self._attributed: dict[tuple[Any, ...], UsageRecord] = {}
        self._buckets: dict[tuple[Any, ...], dict] = {}
        self._sequence = 0
        self._legacy_model: str | None = None
        self._legacy_total: dict[str, int] | None = None
        self._legacy_generation = 0
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
        owned = not payload["thread_id"] or payload["thread_id"] == self.execution_id
        model, ambiguous = _model_evidence(self._models, payload["turn_id"], payload["root_turn_id"],
                                  owned=owned, explicit=payload.get("model"))
        record = UsageRecord(
            session_id=payload["thread_id"] or self.execution_id,
            timestamp=timestamp, response_id=payload["response_id"],
            turn_id=payload["turn_id"], model=model,
            metadata_owned=owned,
            identity_complete=bool(payload["thread_id"] and payload["response_id"]),
            local_identity=("anonymous", self.source_id, payload["local_sequence"]),
            model_ambiguous=ambiguous, feature_known=self._source is not None,
            feature=_feature_for_record(self._source, payload, model), **payload["usage"])
        self._attributed[raw_key] = record
        key = record.dedupe_key()
        self._buckets.setdefault(key, {})[raw_key] = (sequence, record)
        self._winner(key)

    def _modern_record(self, timestamp: str, payload: dict[str, Any], raw_key: tuple) -> None:
        usage = _raw_usage(payload.get("usage"))
        if usage is None:
            return
        normalized = {key: _identifier(payload.get(key)) for key in (
            "response_id", "turn_id", "root_turn_id", "thread_id")}
        normalized["session_id"] = str(payload.get("session_id") or self.session_id)
        normalized["usage"] = usage
        normalized["local_sequence"] = self._sequence
        normalized["model"] = _first_model(payload)
        previous = self._raw.get(raw_key)
        if previous:
            for turn in {previous[2]["turn_id"], previous[2]["root_turn_id"]}:
                self._dependencies[turn].discard(raw_key)
                if not self._dependencies[turn]:
                    del self._dependencies[turn]
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
        kind, payload = entry.get("type"), entry.get("payload")
        previous_execution = self.execution_id
        response_key = None
        if isinstance(payload, dict):
            if kind == "session_meta":
                self.execution_id = _identifier(payload.get("id")) or self.execution_id
            elif kind == "token_usage_record":
                # Count even unusable records so both consumers share stable
                # source-local identities when an anonymous response is skipped.
                self._sequence += 1
                thread, response = _identifier(payload.get("thread_id")), _identifier(payload.get("response_id"))
                response_key = ((thread or self.execution_id, response) if response
                                else ("anonymous", self.source_id, self._sequence))
        self.scope.feed(entry)
        self.credits.feed(entry, execution_id=self.execution_id, response_key=response_key)
        if kind == "session_meta" and isinstance(payload, dict):
            source = payload.get("thread_source") or payload.get("source")
            if source != self._source or previous_execution != self.execution_id:
                self._source = source
                for key in self._raw:
                    self._attribute(key)
            return
        if kind == "turn_context" and isinstance(payload, dict):
            model = _first_model(payload)
            self._legacy_model = model or self._legacy_model
            turn = payload.get("turn_id")
            if isinstance(turn, str) and model and model not in self._models[turn]:
                self._models[turn].add(model)
                for key in self._dependencies.get(turn, ()):
                    self._attribute(key)
            return
        if kind == "token_usage_record" and isinstance(payload, dict):
            self._modern_record(str(entry.get("timestamp") or ""), payload, response_key)
            if not any(key in entry for key in ("usage", "data", "result", "response")):
                return

        # Reuse the batch legacy parser on one line plus a zero-contribution
        # cursor prelude. Its alias handling and headless formats stay canonical.
        prefix = json.dumps({"type": "session_meta", "payload": {"thread_source": self._source}}) + "\n"
        prefix += json.dumps({"type": "turn_context", "payload": {"model": self._legacy_model}}) + "\n"
        if self._legacy_total is not None:
            prefix += json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": self._legacy_total, "last_token_usage": {}}}}) + "\n"
        total = None
        if kind == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                total = _raw_usage(info.get("total_token_usage"))
                if (total is not None and self._legacy_total is not None
                        and total["total_tokens"] < self._legacy_total["total_tokens"]):
                    self._legacy_generation += 1
        for event in parse_usage_text(prefix + line, self.execution_id):
            self.scope.legacy_turns.add(self.scope.turn)
            if event.observation:
                event = replace(event, observation=(self._legacy_generation, *event.observation[1:]))
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
    def __init__(self):
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

    @staticmethod
    def _tier(value):
        if not isinstance(value, str): return None
        return {'default': 'standard', 'priority': 'fast'}.get(value, value)

    def _attribute(self, key, execution_id):
        raw = self.raw[key]
        turn = raw['turn']
        owned = key[0] == execution_id
        # Pricing requires evidence for the actual turn. A root-turn fallback
        # may describe an upstream model, not the model that served this call.
        model, ambiguous = _model_evidence(self.models, turn, None,
                                            owned=owned, explicit=raw.get('explicitModel'))
        record = {k: v for k, v in raw.items() if k not in ('turn', 'rootTurn', 'explicitModel')}
        record.update(provider=self.provider if owned else None,
                      metadataOwned=owned,
                      model=model if model != 'unknown' else None,
                      modelAmbiguous=ambiguous,
                      tierAmbiguous=owned and len(self.tiers[turn]) > 1)
        if record != self.records.get(key):
            self.records[key] = record
            self.changes[key] = record

    def _revisit(self, turn, execution_id):
        for key in self.dependencies[turn]: self._attribute(key, execution_id)

    def feed(self, entry, *, execution_id, response_key):
        kind, payload = entry.get('type'), entry.get('payload')
        if not isinstance(payload, dict): return
        if kind == 'session_meta':
            provider = payload.get('model_provider')
            if isinstance(provider, str):
                self.provider = provider
            # A corrected execution identity also changes metadata ownership.
            for key in self.raw: self._attribute(key, execution_id)
        elif kind == 'turn_context':
            turn = payload.get('turn_id')
            self.active_turn = turn if isinstance(turn, str) and turn else None
            if self.active_turn:
                model = _first_model(payload)
                if model: self.models[turn].add(model)
                if 'service_tier' in payload: self.tier = self._tier(payload['service_tier'])
                if self.tier is not None: self.tiers[turn].add(self.tier)
                self._revisit(turn, execution_id)
            if payload.get('realtime_active'): self.limitations.add('unsupported_media')
        elif kind == 'event_msg' and payload.get('type') == 'thread_settings_applied':
            settings = payload.get('thread_settings')
            self.tier = self._tier(settings.get('service_tier')) if isinstance(settings, dict) else None
            # Only tiers observed at a response or context belong to that turn.
        elif kind == 'token_usage_record':
            thread, response = _identifier(payload.get('thread_id')), _identifier(payload.get('response_id'))
            identity = bool(thread and response)
            key = response_key
            if not isinstance(payload.get('usage'), dict) and key in self.raw:
                # Missing usage cannot retract the existing canonical value.
                return
            turn = payload.get('turn_id')
            turn = turn if isinstance(turn, str) and turn else None
            if key in self.raw:
                for dependency in (self.raw[key]['turn'], self.raw[key].get('rootTurn')):
                    self.dependencies[dependency].discard(key)
            tier = self._tier(payload['service_tier']) if 'service_tier' in payload else self.tier
            old_tiers = len(self.tiers[turn])
            if tier is not None: self.tiers[turn].add(tier)
            usage = payload.get('usage')
            root_turn = payload.get('root_turn_id')
            root_turn = root_turn if isinstance(root_turn, str) and root_turn else None
            self.raw[key] = {'recordKey': key, 'hasResponseIdentity': identity, 'turn': turn,
                             'rootTurn': root_turn, 'explicitModel': _first_model(payload),
                             'serviceTier': tier, 'usage': {k: usage[k] for k in (
                                 *TOKEN_FIELDS, 'cache_write_input_tokens') if k in usage}
                             if isinstance(usage, dict) else None}
            self.dependencies[turn].add(key)
            self.dependencies[root_turn].add(key)
            if len(self.tiers[turn]) > 1 and len(self.tiers[turn]) != old_tiers:
                self._revisit(turn, execution_id)
            else: self._attribute(key, execution_id)
        subtype = str(payload.get('type') or '')
        if any(part in subtype for part in ('image_generation', 'realtime', 'audio')):
            self.limitations.add('unsupported_media')

    def drain(self):
        changes, self.changes = self.changes, {}
        return changes
