"""Pure reference Credits accounting; rates are supplied as immutable snapshots."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal, localcontext


def quote(record, card):
    if not record.get('hasResponseIdentity'):
        return None, 'missing_response_identity'
    if record.get('provider') != 'openai':
        return None, 'unsupported_provider'
    if record.get('modelAmbiguous'):
        return None, 'ambiguous_model'
    rates = card['models'].get(record.get('model'))
    if rates is None:
        return None, 'no_public_model_rate'
    if record.get('tierAmbiguous'):
        return None, 'ambiguous_service_tier'
    tier = card['tierAliases'].get(record.get('serviceTier'))
    if tier is None:
        return None, 'unknown_service_tier'
    if tier == 'fast' and not rates.get('fastMultiplier'):
        return None, 'unknown_fast_rate'
    usage = record.get('usage')
    if not isinstance(usage, dict):
        return None, 'invalid_or_missing_usage'
    names = ('input_tokens', 'cached_input_tokens', 'output_tokens')
    if any(type(usage.get(n)) is not int or not 0 <= usage[n] < 10**18 for n in names):
        return None, 'invalid_or_missing_usage'
    inp, cached, out = (usage[n] for n in names)
    if cached > inp:
        return None, 'invalid_or_missing_usage'
    total, reasoning = usage.get('total_tokens'), usage.get('reasoning_output_tokens')
    if total is not None and (type(total) is not int or total != inp + out):
        return None, 'inconsistent_usage'
    if reasoning is not None and (type(reasoning) is not int or not 0 <= reasoning <= out):
        return None, 'inconsistent_usage'
    write = usage.get('cache_write_input_tokens', 0)
    if type(write) is not int or write < 0:
        return None, 'invalid_or_missing_usage'
    if write and card.get('cacheWritePolicy') != 'included_in_input_no_separate_charge':
        return None, 'unresolved_cache_write_rate'
    with localcontext() as context:
        context.prec = 80
        amount = (Decimal(inp-cached)*Decimal(rates['input']) + Decimal(cached)*Decimal(rates['cachedInput'])
                  + Decimal(out)*Decimal(rates['output'])) / Decimal(1_000_000)
        return amount * (Decimal(rates['fastMultiplier']) if tier == 'fast' else 1), None


class CreditsLedger:
    """Replace response contributions in constant time; reprice only on a rate change."""
    def __init__(self, card):
        self.card = deepcopy(card)
        self.records = {}
        self._quotes = {}
        self.total = Decimal(0)
        self.priced = 0
        self.reasons = Counter()

    def set(self, key, record):
        with localcontext() as context:
            context.prec = 80
            if key in self._quotes:
                amount, reason = self._quotes.pop(key)
                if reason:
                    self.reasons[reason] -= 1
                    if not self.reasons[reason]: del self.reasons[reason]
                else:
                    self.total -= amount
                    self.priced -= 1
                self.records.pop(key)
            if record is None: return
            self.records[key] = record
            amount, reason = quote(record, self.card)
            self._quotes[key] = amount, reason
            if reason: self.reasons[reason] += 1
            else:
                self.total += amount
                self.priced += 1

    def set_card(self, card):
        if card['id'] == self.card['id']: return
        records = list(self.records.items())
        self.__init__(card)
        for key, record in records: self.set(key, record)

    def report(self, limitations=()):
        limits = sorted(set(limitations))
        count = len(self.records)
        status = ('unavailable' if not self.priced and (count or limits) else 'waiting' if not count
                  else 'partial' if self.reasons or limits else 'complete')
        by_model = {}
        with localcontext() as context:
            context.prec = 80
            for key, record in self.records.items():
                model = record.get('model')
                if not isinstance(model, str) or not model or record.get('modelAmbiguous'):
                    continue
                group = by_model.setdefault(model, {'total': Decimal(0), 'priced': 0, 'unpriced': 0})
                amount, reason = self._quotes[key]
                if reason:
                    group['unpriced'] += 1
                else:
                    group['total'] += amount
                    group['priced'] += 1
        model_reports = {}
        for model, group in sorted(by_model.items()):
            priced, unpriced = group['priced'], group['unpriced']
            model_reports[model] = {
                'status': 'partial' if priced and unpriced else 'complete' if priced else 'unavailable',
                'estimatedCredits': format(group['total'], 'f') if priced else None,
                'pricedResponses': priced,
                'unpricedResponses': unpriced,
            }
        return {'status': status, 'estimatedCredits': format(self.total, 'f') if self.priced else None,
                'rateCardId': self.card['id'], 'observedResponses': count,
                'pricedResponses': self.priced, 'unpricedResponses': count-self.priced,
                'unpricedReasons': dict(sorted(self.reasons.items())), 'limitations': limits,
                'byModel': model_reports}
