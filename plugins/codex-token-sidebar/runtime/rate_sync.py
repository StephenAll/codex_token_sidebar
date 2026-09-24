"""Public-document rate synchronization, validation and last-known-good cache."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import urllib.request

SOURCES = {name: 'https://learn.chatgpt.com/docs/' + path + '.md' for name, path in (
    ('pricing', 'pricing'), ('speed', 'agent-configuration/speed'), ('models', 'models'))}
MAX_BYTES = 2 * 1024 * 1024
ALIASES = {'default': 'standard', 'standard': 'standard', 'priority': 'fast', 'fast': 'fast'}


def validate_card(card):
    if not isinstance(card, dict) or not isinstance(card.get('id'), str) or not card['id']:
        raise ValueError('invalid_rate_card')
    if card.get('unit') != 'credits_per_million_tokens' or card.get('tierAliases') != ALIASES:
        raise ValueError('unsupported_rate_unit_or_tiers')
    if card.get('cacheWritePolicy') != 'included_in_input_no_separate_charge':
        raise ValueError('unsupported_cache_write_policy')
    models = card.get('models')
    if not isinstance(models, dict) or not models or len(models) > 1000:
        raise ValueError('invalid_model_table')
    for model, rates in models.items():
        if not isinstance(model, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,100}', model):
            raise ValueError('invalid_model_id')
        if not isinstance(rates, dict): raise ValueError('invalid_rates')
        for key in ('input', 'cachedInput', 'output', 'fastMultiplier'):
            if key == 'fastMultiplier' and key not in rates: continue
            raw = rates.get(key)
            if not isinstance(raw, str) or not re.fullmatch(r'\d{1,9}(?:\.\d{1,9})?', raw):
                raise ValueError('invalid_numeric_rate')
            value = Decimal(raw)
            if key == 'fastMultiplier' and value < 1: raise ValueError('invalid_fast_multiplier')
    return deepcopy(card)


def bundled_card():
    return validate_card(json.loads(Path(__file__).with_name('credits_rates.json').read_text()))


def version(card):
    fields = {k: card[k] for k in ('unit', 'models', 'tierAliases', 'cacheWritePolicy')}
    return 'official-' + hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.table, self.row, self.cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            if self.table is not None: raise ValueError('nested_table')
            self.table = []
        elif tag == 'tr' and self.table is not None: self.row = []
        elif tag in ('td', 'th') and self.row is not None: self.cell = []

    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == 'table' and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def _fast_rates(text, models):
    # Accept explicit credit-rate clauses only, never speed or API-price multipliers.
    text = ' '.join(text.split())
    names = r'GPT-\d+(?:\.\d+)?(?: [A-Z][a-z]+)?'
    scopes = {}
    scope = names + r'(?:,? (?:and )?' + names + r')*'
    patterns = (
        r'For (?P<scope>' + scope + r'), Fast mode consumes credits at (?P<rate>\d+(?:\.\d+)?)x the Standard rate',
        r'(?P<scope>' + scope + r') consume(?:s)? credits at (?P<rate>\d+(?:\.\d+)?)x the Standard rate',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            scope = match['scope']
            # Do not let a greedy prefix carry unrelated preceding sentences.
            scope = scope.rsplit('. ', 1)[-1].rsplit('; ', 1)[-1]
            ids = [n.lower().replace(' ', '-') for n in re.findall(names, scope)]
            for identity in ids:
                scopes.setdefault(identity, set()).add(match['rate'])
    result = {}
    for model in models:
        matches = scopes.get(model)
        if matches is None:
            family = re.match(r'gpt-\d+(?:\.\d+)?', model)
            matches = scopes.get(family[0], set()) if family and family[0] not in models else set()
        if len(matches) > 1: raise ValueError('conflicting_fast_rates')
        if matches: result[model] = next(iter(matches))
    return result


def parse_documents(documents, previous):
    parser = _Tables()
    parser.feed(documents['pricing'])
    header = ['Credits per 1M tokens', 'Input Tokens', 'Cached input tokens', 'Output Tokens']
    tables = [t for t in parser.tables if t and t[0] == header]
    if len(tables) != 1: raise ValueError('credits_table_not_unique')
    if 'Codex credit billing has no separate cache-write charge.' not in documents['pricing']:
        raise ValueError('cache_write_policy_unconfirmed')
    slugs = set(re.findall(r'<ModelDetails\b.*?\bslug="([^"]+)"', documents['models'], re.S))
    if not slugs: raise ValueError('model_catalogue_unrecognized')
    known = slugs | set(previous['models'])
    models = {}
    for row in tables[0][1:]:
        if len(row) == 1: continue  # Table footnotes.
        if len(row) != 4: raise ValueError('invalid_price_row')
        model = row[0].lower().replace(' ', '-')
        if model not in known: continue
        if model in models: raise ValueError('duplicate_model')
        rates = {}
        for key, raw in zip(('input', 'cachedInput', 'output'), row[1:]):
            if not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)? credits', raw):
                raise ValueError('invalid_price_cell')
            rates[key] = format(Decimal(raw.removesuffix(' credits').replace(',', '')).normalize(), 'f')
        models[model] = rates
    if set(previous['models']) - set(models): raise ValueError('existing_model_missing')
    fast = _fast_rates(documents['speed'], models)
    for model, rate in fast.items(): models[model]['fastMultiplier'] = rate
    # Existing Fast coverage disappearing indicates a parser/source change.
    if any('fastMultiplier' in rates and model not in fast for model, rates in previous['models'].items()):
        raise ValueError('existing_fast_rule_missing')
    card = {'id': 'pending', 'unit': 'credits_per_million_tokens', 'models': models,
            'tierAliases': ALIASES, 'cacheWritePolicy': 'included_in_input_no_separate_charge',
            'basis': 'published_reference_snapshot_not_historical_invoice',
            'sources': list(SOURCES.values()), 'verifiedOn': datetime.now(timezone.utc).date().isoformat(),
            'sourceHashes': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in documents.items()}}
    card = validate_card(card)
    card['id'] = version(card)
    return card


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('source_redirected')


def fetch_document(url):
    if url not in SOURCES.values(): raise ValueError('unapproved_source')
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, headers={'Accept': 'text/markdown', 'User-Agent': 'Codex-Token-Sidebar/1'})
    deadline = time.monotonic() + 12
    with opener.open(request, timeout=4) as response:
        if response.status != 200: raise ValueError('source_status')
        data = bytearray()
        while True:
            if time.monotonic() > deadline: raise TimeoutError('source_deadline')
            block = response.read1(min(65536, MAX_BYTES + 1-len(data)))
            if not block: break
            data.extend(block)
            if len(data) > MAX_BYTES: raise ValueError('source_too_large')
    return data.decode('utf-8')


class RateSync:
    """Nonblocking polling facade. Only its worker fetches or writes cache files."""
    def __init__(self, cache_path, *, fetch=fetch_document, clock=time.monotonic):
        self.path, self.fetch, self.clock = Path(cache_path), fetch, clock
        self._lock = threading.Lock()
        self._card = bundled_card()
        self._previous = self._card
        self._status = 'bundled'
        self._next = 0
        self._last_attempt = float('-inf')
        self._failures = 0
        self._worker = None
        self._closed = False
        try:
            if self.path.stat().st_size > MAX_BYTES: raise ValueError('cache_too_large')
            cache = json.loads(self.path.read_text())
            card = validate_card(cache['current'])
            if card['id'] != version(card): raise ValueError('cache_version_mismatch')
            self._previous = validate_card(cache.get('previous', card))
            self._card, self._status = card, 'cached'
        except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
            pass

    def poll(self, unknown_model=False):
        with self._lock:
            now = self.clock()
            due = now >= self._next or (unknown_model and self._status != 'stale' and now-self._last_attempt >= 300)
            if not self._closed and due and (self._worker is None or not self._worker.is_alive()):
                self._last_attempt = now
                self._worker = threading.Thread(target=self._refresh, name='credits-rates', daemon=True)
                self._worker.start()
            return deepcopy(self._card), self._status

    def _refresh(self):
        try:
            with self._lock: previous = deepcopy(self._card)
            documents = {name: self.fetch(url) for name, url in SOURCES.items()}
            card = parse_documents(documents, previous)
            with self._lock:
                if self._closed: return
                retained = previous if card['id'] != previous['id'] else self._previous
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.rates-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w') as handle:
                    json.dump({'current': card, 'previous': retained}, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                with self._lock:
                    if self._closed: return
                    os.replace(name, self.path)
                    self._card, self._previous, self._status = card, retained, 'current'
                    self._failures = 0
                    self._next = self.clock() + 21600
            finally:
                if os.path.exists(name): os.unlink(name)
        except Exception:
            with self._lock:
                if self._closed: return
                self._status = 'stale'
                self._failures += 1
                self._next = self.clock() + min(21600, 300 * 2**min(self._failures-1, 7))

    def close(self):
        with self._lock: self._closed = True
        # A bounded HTTP request may still finish; it cannot publish after close.
        if self._worker: self._worker.join(timeout=0.1)
