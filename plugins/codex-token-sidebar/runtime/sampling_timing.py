"""Bounded Desktop log intake and response-joined IAPI sampling evidence.

No message bodies are retained. This source supplements the canonical rollout
ledger; it cannot supply usage, lifecycle or billing authority.
"""
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time

TARGET = 'codex_api::responses_websocket_timing'
FIELDS = ('response_id', 'timing_scope', 'num_sampled_tokens_total', 'num_engine_calls',
          'engine_iapi_sampling_total_ms', 'engine_iapi_ttft_total_ms',
          'engine_iapi_inference_total_ms', 'engine_iapi_tbt_across_engine_calls_ms')
LOG_NAME = re.compile(r'^(codex-desktop-.+-t0-i\d+-\d+)-(\d+)\.log$')
MAX_BUFFER = 2 * 1024 * 1024


def desktop_logs_directory():
    home = Path.home()
    if sys.platform == 'darwin':
        return home / 'Library/Logs/com.openai.codex'
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')) / 'Codex/Logs'
    return Path(os.environ.get('XDG_STATE_HOME', home / '.local/state')) / 'codex/logs'


def _object(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


def _log_time(value):
    if isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


def _metric_issue(m):
    if m['timing_scope'] != 'logical_turn':
        return 'unsupported_scope'
    n, calls = m['num_sampled_tokens_total'], m['num_engine_calls']
    if type(n) is not int or type(calls) is not int or not 0 < calls < n <= 2**53:
        return 'sampling_intervals_unavailable'
    ms, ttft, inference, tbt = (m['engine_iapi_' + key] for key in (
        'sampling_total_ms', 'ttft_total_ms', 'inference_total_ms', 'tbt_across_engine_calls_ms'))
    if (not all(type(v) in (int, float) and 0 <= v <= 2**53 and math.isfinite(v) for v in (ms, ttft, inference, tbt))
            or ms == 0 or tbt == 0):
        return 'sampling_time_unavailable'
    if not (math.isclose(ms / (n - calls), tbt, rel_tol=1e-9, abs_tol=1e-6)
            and math.isclose(ms + ttft, inference, rel_tol=1e-9, abs_tol=1e-6)):
        return 'inconsistent_timing'
    return None


class _Decoder:
    """The file line and nested stderr JSON each have independent boundaries."""
    def __init__(self):
        self.line = b''
        self.stderr = ''
        self.discard_line = False

    def feed(self, data):
        parts = (self.line + data).split(b'\n')
        self.line = parts.pop()
        for raw in parts:
            if self.discard_line:
                self.discard_line = False
                continue
            marker = b'[AppServerConnection] Codex CLI stderr message='
            if marker not in raw or len(raw) > MAX_BUFFER:
                continue
            try:
                chunk, _ = json.JSONDecoder().raw_decode(raw.split(marker, 1)[1].decode('utf-8'))
            except (ValueError, UnicodeError):
                self.stderr = ''
                continue
            if not isinstance(chunk, str):
                continue
            # A complete new JSON object cannot be a continuation of a partial
            # object. Drop the fragment instead of joining different backends.
            if self.stderr and chunk.startswith('{'):
                try:
                    json.JSONDecoder().raw_decode(chunk)
                    self.stderr = ''
                except ValueError:
                    pass
            self.stderr += chunk
            while self.stderr.strip():
                self.stderr = self.stderr.lstrip()
                try:
                    event, stop = json.JSONDecoder().raw_decode(self.stderr)
                except ValueError:
                    break
                self.stderr = self.stderr[stop:]
                yield event
            if len(self.stderr) > MAX_BUFFER:
                self.stderr = ''
        if len(self.line) > MAX_BUFFER:
            self.line = b''
            self.discard_line = True


class DesktopTimingReader:
    """Poll recent main-process logs incrementally; retain one selected thread.

    At most 32 files from 14 UTC days, 128 turns and 8192 responses in total.
    A bounded read budget makes backfill yield to normal sidebar updates.
    Missing/rotated-out history remains unavailable, never reconstructed.
    """
    def __init__(self, root=None, *, clock=time.monotonic, read_budget=8 * 1024 * 1024):
        self.root = Path(root) if root is not None else desktop_logs_directory()
        self.clock = clock
        self.read_budget = read_budget
        self.revision = 0
        self.bytes_read = 0
        self.thread = None
        self._next_discovery = 0
        self._paths = []
        self._reset()

    def _reset(self):
        self._files = {}
        self._decoders = {}
        self._turns = OrderedDict()
        self._response_turns = {}
        self._record_count = 0
        self.ready = False
        self.read_failed = False
        self.revision += 1

    def _discover(self):
        today = datetime.now(timezone.utc).date()
        directories = [self.root, *(self.root / (today - timedelta(days=n)).strftime('%Y/%m/%d') for n in range(14))]
        found = []
        for directory in directories:
            for path in directory.glob('codex-desktop-*-t0-*.log'):
                match = LOG_NAME.match(path.name)
                if match:
                    found.append((path.stat().st_mtime_ns, path))
        recent = sorted(found)[-32:]
        modified_times = {path: modified for modified, path in recent}
        # Desktop reuses a bounded ring of suffixes (0..4). Modification time
        # orders its retained segments; suffix 0 can be the newest active file.
        family_times = {}
        for modified, path in recent:
            family = LOG_NAME.match(path.name)[1]
            family_times[family] = max(modified, family_times.get(family, modified))
        # Read the most recently active process first, preserving segment order
        # inside each family so nested JSON fragments still join correctly.
        return sorted((p for _, p in recent), key=lambda p: (-family_times[LOG_NAME.match(p.name)[1]],
                       LOG_NAME.match(p.name)[1], modified_times[p], int(LOG_NAME.match(p.name)[2])))

    def poll(self, thread):
        self.bytes_read = 0
        if thread != self.thread:
            self.thread = thread
            self._reset()
            self._next_discovery = 0
        if not thread:
            self.ready = True
            return self.revision
        was_ready = self.ready
        was_failed = self.read_failed
        self.read_failed = False
        try:
            if self.clock() >= self._next_discovery:
                paths = self._discover()
                positions = {path: index for index, path in enumerate(paths)}
                # New historical segments require ordered replay as well.
                inserted_before_cursor = any(
                    LOG_NAME.match(new.name)[1] == LOG_NAME.match(old.name)[1]
                    and positions[new] < positions[old]
                    for new in set(paths) - set(self._paths) for old in self._files if old in positions)
                if set(self._paths) - set(paths) or inserted_before_cursor:
                    self._reset()
                self._paths = paths
                self._next_discovery = self.clock() + 5
            # A rewritten source invalidates derived evidence before replay.
            consumed = {path: index for index, path in enumerate(self._files)}
            for path, state in self._files.items():
                stat = path.stat()
                inode, offset, modified, head, tail = state
                if ((stat.st_dev, stat.st_ino) != inode or stat.st_size < offset
                        or (stat.st_size == offset and stat.st_mtime_ns != modified)):
                    self._reset(); break
                if stat.st_size == offset and stat.st_mtime_ns == modified:
                    continue
                family = LOG_NAME.match(path.name)[1]
                if any(LOG_NAME.match(other.name)[1] == family
                       and consumed[other] > consumed[path] and state[1]
                       for other, state in self._files.items()):
                    self._reset(); break
                with path.open('rb') as stream:
                    if stream.read(len(head)) != head:
                        self._reset(); break
                    stream.seek(max(0, offset - len(tail)))
                    if stream.read(len(tail)) != tail:
                        self._reset(); break
            self.ready = True
            for path in self._paths:
                stat = path.stat()
                inode = (stat.st_dev, stat.st_ino)
                _, offset, _, head, tail = self._files.get(path, (inode, 0, 0, b'', b''))
                if offset == stat.st_size:
                    continue
                family = LOG_NAME.match(path.name)[1]
                decoder = self._decoders.setdefault(family, _Decoder())
                with path.open('rb') as stream:
                    stream.seek(offset)
                    while offset < stat.st_size and self.bytes_read < self.read_budget:
                        data = stream.read(min(65536, stat.st_size-offset, self.read_budget-self.bytes_read))
                        if not data: break
                        offset += len(data); self.bytes_read += len(data)
                        head = (head + data)[:64]; tail = (tail + data)[-64:]
                        for event in decoder.feed(data): self._accept(event)
                self._files[path] = (inode, offset, stat.st_mtime_ns, head, tail)
                if offset < stat.st_size:
                    self.ready = False
                    break
        except OSError:
            self.ready = False
            self.read_failed = True
        if (was_ready, was_failed) != (self.ready, self.read_failed): self.revision += 1
        return self.revision

    def _accept(self, event):
        if _object(event).get('target') != TARGET: return
        f = _object(event.get('fields'))
        if f.get('thread_id') != self.thread or f.get('warmup') is True: return
        turn = _text(f.get('turn_id'))
        if not turn: return
        evidence = self._turns.setdefault(turn, {'records': {}, 'times': {}, 'models': set(), 'conflict': False,
                                                'observedAt': None})
        self._turns.move_to_end(turn)
        observed = _log_time(event.get('timestamp'))
        if observed is not None:
            evidence['observedAt'] = max(observed, evidence['observedAt'] or observed)
        before = (len(evidence['records']), frozenset(evidence['models']), evidence['conflict'])
        evidence['models'].add(_text(f.get('model')) or 'unknown')
        payload = f.get('payload')
        if isinstance(payload, str):
            try: payload = json.loads(payload)
            except ValueError: payload = None
        raw_metric = _object(_object(payload).get('timing_metrics'))
        metric = {key: value if type(value) in (str, int, float) else None
                  for key in FIELDS for value in [raw_metric.get(key)]}
        rid = _text(metric['response_id'])
        previous = f.get('previous_response_id')
        # Desktop encodes the first response's absent predecessor as "".
        if previous == '': previous = None
        record = {'metric': metric, 'previous': previous, 'model': _text(f.get('model'))}
        records = evidence['records']
        if (f.get('warmup') is not False or _object(payload).get('type') != 'responsesapi.websocket_timing'
                or not rid or (previous is not None and not _text(previous))):
            evidence['conflict'] = True
        elif rid in records and records[rid] != record:
            evidence['conflict'] = True
        elif len(records) >= 4096 and rid not in records:
            evidence['conflict'] = True
        else:
            owner = self._response_turns.get(rid)
            if owner is not None and owner != turn:
                evidence['conflict'] = True
                self._turns[owner]['conflict'] = True
                self.revision += 1
            self._response_turns[rid] = turn
            if rid not in records: self._record_count += 1
            records[rid] = record
        if rid in records and observed is not None:
            previous_time = evidence['times'].get(rid)
            first_observed = min(observed, previous_time) if previous_time is not None else observed
            if first_observed != previous_time:
                evidence['times'][rid] = first_observed
                self.revision += 1
        while len(self._turns) > 128 or self._record_count > 8192:
            # Backfill runs after recent logs. Its late arrival must not evict
            # the latest turns merely because they were read first.
            old_turn = min(self._turns, key=lambda key: (
                self._turns[key]['observedAt'] is not None, self._turns[key]['observedAt'] or 0))
            old = self._turns.pop(old_turn)
            self._record_count -= len(old['records'])
            for response in old['records']:
                if self._response_turns.get(response) == old_turn:
                    del self._response_turns[response]
            self.revision += 1
        if before != (len(records), frozenset(evidence['models']), evidence['conflict']):
            self.revision += 1

    def models(self, turn):
        return self._turns.get(turn, {}).get('models', set()) if not self.read_failed else set()

    def latest(self, current):
        """Join the newest completed main-thread segment, including open turns."""
        if self.read_failed or current is None:
            return None
        candidates = sorted(self._turns.items(), key=lambda item: (
            max(item[1]['times'].values(), default=float('-inf')), item[0]), reverse=True)
        best, best_time = None, float('-inf')
        for turn, evidence in candidates:
            if not evidence['times']:
                continue
            upper_time = max(evidence['times'].values(), default=float('-inf'))
            if upper_time < best_time:
                break
            usage = current.turn_usage.get(turn)
            if not usage or usage['invalid'] or usage['conflicts']:
                continue
            models = set(usage['models']) | evidence['models']
            if len(models) != 1 or 'unknown' in models:
                continue
            measured, issue = self.measure(turn, usage['responses'], partial=True)
            segment = measured.get('latestSegment') if not issue else None
            if not segment or not segment['sampledAt']:
                continue
            observed = evidence['times'][segment['responseId']]
            if (observed, turn, segment['responseId']) > (best_time, best['turnId'] if best else '',
                                                        best['responseId'] if best else ''):
                best_time = observed
                best = {'metric':'engine_iapi_sampling_interval', 'selection':'completed_segment',
                        'turnId':turn, **segment}
        return best

    def measure(self, turn, counts, *, partial=False):
        evidence = self._turns.get(turn)
        if self.read_failed or not evidence:
            return {}, 'sampling_timing_unavailable'
        if evidence['conflict']: return {}, 'conflicting_evidence'
        records = evidence['records']
        if partial:
            # Timing and canonical completion usage can arrive on either side
            # first. Only their joined prefix may supply a completed segment.
            records = {rid: record for rid, record in records.items() if rid in counts}
        if not records or (not partial and records.keys() != counts.keys()): return {}, 'response_usage_mismatch'
        roots = [rid for rid, r in records.items() if r['previous'] not in records]
        if partial:
            roots = [rid for rid in roots if records[rid]['metric']['num_sampled_tokens_total'] == counts[rid]]
        if len(roots) != 1: return {}, 'ambiguous_response_chain'
        children = {}
        for rid, r in records.items(): children.setdefault(r['previous'], []).append(rid)
        rid, seen, output, previous = roots[0], set(), 0, (0, 0, 0, 0)
        latest_segment = None
        while rid is not None:
            if rid in seen: return {}, 'ambiguous_response_chain'
            seen.add(rid)
            m = records[rid]['metric']
            issue = _metric_issue(m)
            if issue: return {}, issue
            output += counts[rid]
            if output != m['num_sampled_tokens_total']: return {}, 'response_usage_mismatch'
            current = tuple(m[k] for k in (FIELDS[2], FIELDS[3], FIELDS[4], FIELDS[5]))
            if any(a < b for a, b in zip(current, previous)): return {}, 'cumulative_timing_reset'
            intervals = current[0] - current[1] - previous[0] + previous[1]
            ms = current[2] - previous[2]
            # Only a single engine call supplies a paired segment rate and TTFT.
            # Aggregate evidence still supplies the completed turn's mean.
            if current[1] - previous[1] == 1 and intervals > 0 and ms > 0:
                rate = intervals * 1000 / ms
                if math.isfinite(rate):
                    observed = evidence['times'].get(rid)
                    latest_segment = {'responseId':rid, 'model':records[rid]['model'],
                        'sampledAt':datetime.fromtimestamp(observed, timezone.utc).isoformat(timespec='milliseconds')
                                    if observed is not None else None,
                        'outputTokensPerSecond':rate, 'firstTokenLatencyMs':current[3]-previous[3]}
            previous = current
            successors = children.get(rid, [])
            if len(successors) > 1: return {}, 'ambiguous_response_chain'
            rid = successors[0] if successors else None
        if not partial and len(seen) != len(records): return {}, 'ambiguous_response_chain'
        return {'samplingIntervals': m['num_sampled_tokens_total'] - m['num_engine_calls'],
                'samplingMs': m['engine_iapi_sampling_total_ms'],
                'latestSegment': latest_segment}, None
