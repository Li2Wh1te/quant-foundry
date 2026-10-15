"""In-process pages of the existing SQL dependencies, without new fact fields."""
from copy import deepcopy
from dataclasses import dataclass
from array import array
from datetime import date

from .session_fact_pages import PagedSessionInputs, SessionInputPage, check_input_bytes
from .serialization import encoded


class SqlSessionInputs(PagedSessionInputs):
    # The SQL dependency reader is live, but the existing stored facts cannot
    # produce an ExecutionSessionInput. This code-owned denial is not a caller
    # flag, and a reader's presence must not advertise executable source facts.
    unavailable_reason = 'execution_session_input_producer_missing'


class ScopedChecks(list):
    """Keep one first failure per binding and exact visited-cell counts.

    The original 256 KiB retained diagnostic limit still applies. Successful
    cell diagnostics do not need a whole-run per-cell history to prove that
    every page was visited. A failure is never replaced by a later success.
    """
    def __init__(self, symbols=()):
        super().__init__()
        self.positions = {}
        self.symbol_order = {symbol: i for i, symbol in enumerate(symbols)}

    def _order(self, check):
        scope = check['scope']
        return scope.get('date', ''), self.symbol_order.get(scope.get('security'), -1)

    def _record(self, check, count):
        name = check['binding']
        position = self.positions.get(name)
        if position is None:
            self.positions[name] = len(self)
            value = deepcopy(check)
            super().append(value)
            total = count
        else:
            value = self[position]
            total = int(value['scope']['checked_cells']) + count
            if (value['available'] and not check['available'] or self.symbol_order
                    and not value['available'] and not check['available'] and self._order(check) < self._order(value)):
                value = deepcopy(check)
                self[position] = value
        value['scope']['checked_cells'] = str(total)

    def append(self, check):
        self._record(check, 1)

    def extend(self, checks):
        for check in checks:
            self.append(check)

    def merge(self, checks):
        for check in checks:
            self._record(check, int(check['scope']['checked_cells']))


class SessionFailurePositions:
    """Compact day positions; never retain per-day error payloads or facts.

    Seven uint32 arrays at the existing 3,660-day bound use at most 102,480
    bytes. Keep one detailed error per binding; reread a selected open-session
    error in the same SQL snapshot only when the first error was irrelevant.
    """
    EMPTY = 2**32-1

    def __init__(self, symbols, start, end, bindings):
        self.symbols, self.start = symbols, start
        self.ranks = {symbol: i for i, symbol in enumerate(symbols)}
        days = (end-start).days+1
        self.positions = {name: array('I', [self.EMPTY])*days for name in bindings}
        self.first = {}

    def record(self, check):
        if check['available']:
            return
        name, scope = check['binding'], check['scope']
        offset = (date.fromisoformat(scope['date'])-self.start).days
        rank = self.ranks[scope['security']]
        self.positions[name][offset] = min(self.positions[name][offset], rank)
        previous = self.first.get(name)
        if previous is None or (scope['date'], rank) < (
                previous['scope']['date'], self.ranks[previous['scope']['security']]):
            self.first[name] = deepcopy(check)

    def control_bytes(self, checks):
        return (sum(len(values)*values.itemsize for values in self.positions.values())
                + len(encoded(self.first)) + len(encoded(checks)))

    def select(self, name, days, reread):
        for day in days:
            offset = (date.fromisoformat(day)-self.start).days
            rank = self.positions[name][offset]
            if rank == self.EMPTY:
                continue
            symbol = self.symbols[rank]
            check = self.first[name]
            if check['scope']['date'] != day or check['scope']['security'] != symbol:
                check = reread(symbol, day)[name]
            if check['available']:
                from .runtime_contracts import RuntimeFault
                raise RuntimeFault('INVALID_CONTRACT', '会话缺项复核与同一SQL快照不一致')
            return deepcopy(check)
        return None


@dataclass(frozen=True)
class SqlDependencyPage:
    identities: tuple
    calendars: tuple
    rule_packages: tuple
    trading_status: tuple
    corporate_actions: tuple
    commissions: tuple
    checks: tuple

    def session_inputs(self):
        # Identity/mapping and rule validation already ran through the original
        # repositories. The composer only needs the original identity fields,
        # captured commissions, status, action coverage and rejection checks.
        page = SessionInputPage((),
            tuple((s, d, identity, ()) for s, d, identity, _ in self.identities),
            self.commissions, self.trading_status, self.corporate_actions, self.checks)
        check_input_bytes(page)
        return page
