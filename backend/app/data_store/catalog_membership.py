"""Execution-local proof of a provider directory member changing category.

An absent code never proves that an instrument was delisted. This rule only
compares two actual category claims after complete catalogues at the SAME
provider snapshot positively locate the member in one category and exclude it
from the other. Observation ordering remains local to each original category.
No aliases, cross-category time priority or persistent success ledger is used.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import sys

from app.data_ingestion.tonghuashun.contracts import ASSET_TYPES, CollectionError, validate_ticker

from .adapters.canonical import NativeInputError
from .adapters.contracts import LocalInput
from .adapters.record_adapters import convert


@dataclass(frozen=True)
class _Catalogue:
    source: LocalInput
    version: int
    codes: frozenset[str]


class CatalogMemberships:
    """Small native-head context retained only while one bounded scan runs."""

    def __init__(self, heads):
        self._heads = {}
        scopes = set()
        for number, (source, requests) in enumerate(heads):
            if number >= 64:
                raise NativeInputError('SOURCE_BUDGET_EXCEEDED', '标的目录身份凭据超过有界范围。')
            # Equal observation times with separate heads are not ordered by a
            # UUID. Even equivalent heads conservatively provide no withdrawal
            # proof here; the ordinary merger still handles their actual rows.
            scope = (source.subject, source.variant)
            if scope in scopes:
                self._heads.pop(source.group, None)
                continue
            scopes.add(scope)
            try:
                catalogue = self._complete(source, requests)
            except (NativeInputError, CollectionError, ValueError, TypeError, KeyError):
                continue
            if catalogue is not None:
                self._heads[source.group] = catalogue
                # Keep only the six directory membership sets, not their paid
                # payloads/names. A conservative measured Python-object cap
                # bounds this extra ephemeral context before another head is
                # read; the ordinary process/scratch checks remain in force.
                if sum(sys.getsizeof(head.codes) + sum(sys.getsizeof(code) for code in head.codes)
                       for head in self._heads.values()) > 64 * 1024 * 1024:
                    raise NativeInputError('SOURCE_BUDGET_EXCEEDED', '标的目录身份凭据超过内存预算。')

    @staticmethod
    def _complete(source, requests):
        if ((source.source, source.dataset, source.variant) != ('tonghuashun', 'tickers', 'default')
                or source.subject not in ASSET_TYPES or source.failure or source.order_kind != 'observation'):
            return None
        body = source.content
        if not isinstance(body, dict) or not isinstance(body.get('item'), list) or not body['item']:
            return None
        version = body.get('timestamp')
        if (type(version) is not int or not 0 <= version < 2**63
                or body.get('complete') is False or body.get('has_more') is True
                or body.get('next_cursor') not in (None, '') or body.get('failed_requests')):
            return None
        if not isinstance(requests, list) or not requests or len(requests) > 100:
            return None
        size = None
        for index, request in enumerate(requests):
            if not isinstance(request, dict) or request.get('interface') != 'meta.tickers.list':
                return None
            parameters = request.get('parameters')
            if not isinstance(parameters, dict) or parameters.get('asset_type') != source.subject:
                return None
            limit, offset = parameters.get('limit'), parameters.get('offset')
            if (type(limit) is not int or not 0 < limit <= 10_000
                    or type(offset) is not int or offset != index * limit
                    or size is not None and size != limit):
                return None
            size = limit
        # The checked collector publishes one immutable anchor only after a
        # terminal short page and a consistent provider version on every page.
        # Contiguous request offsets plus the preserved total must agree with
        # that terminal page; one full physical page cannot prove absence.
        if not (len(requests) - 1) * size <= len(body['item']) < len(requests) * size:
            return None
        codes = set()
        for raw in body['item']:
            if not isinstance(raw, dict):
                return None
            validate_ticker(raw, source.subject)
            converted = convert(source, raw)
            code = converted['body']['source_code']
            if code in codes:
                return None
            codes.add(code)
        return _Catalogue(replace(source, content={}), version, frozenset(codes))

    def relation(self, key, left, right):
        """Return an exact membership proof, without comparing category times.

        Each side is (original_group, original_order, original_token). A former
        claim is covered only by a STRICTLY later complete absence in its own
        category. A surviving claim belongs to the independently confirmed
        positive category. The full scan must reach that positive head before
        it can publish; its original head basis is kept in the proof.
        """
        if len(key) != 3 or key[2] != 'current' or left[0] == right[0]:
            return None
        a, b = self._heads.get(left[0]), self._heads.get(right[0])
        if (a is None or b is None or a.version != b.version
                or a.source.representation_key != key[0]
                or b.source.representation_key != key[0]):
            return None
        code = key[1]
        for positive, negative, claim, former in ((a, b, left, right), (b, a, right, left)):
            if code not in positive.codes or code in negative.codes:
                continue
            if not former[1] < negative.source.order_ns:
                continue
            if (claim[1] > positive.source.order_ns
                    or claim[1] == positive.source.order_ns and claim[2] != positive.source.token):
                continue
            return {'key': list(key), 'provider_snapshot': positive.version,
                    'winner': {'group': positive.source.group, 'order': positive.source.order_ns,
                               'token': positive.source.token},
                    'former': {'group': negative.source.group, 'before': negative.source.order_ns,
                               'absence_token': negative.source.token}}
        return None


def proof_covers(proof, key, group, order, winner):
    """A saved execution proof resolves only its exact old classification."""
    if not isinstance(proof, dict) or list(key) != proof.get('key') or winner is None:
        return False
    former, positive = proof.get('former', {}), proof.get('winner', {})
    return bool(winner['validated'] and winner['state'] == 'valid'
                and group == former.get('group') and order < former.get('before', -1)
                and (winner['g'], winner['n'], winner['t']) ==
                    (positive.get('group'), positive.get('order'), positive.get('token')))
