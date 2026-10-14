"""Transaction-local reuse of the existing PIT calendar candidates.

Read one month at a time and cap both candidate and selected-result caches.
Selection still uses each caller's original simulated knowledge boundary.
Nothing survives the SQL session or turns a missing natural day into a holiday.
"""
from collections import OrderedDict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


class CalendarReader:
    def __init__(self, session):
        from app.calendars.repository import CalendarFactRepository
        self.repository = CalendarFactRepository(session)
        self.priorities = self.repository.list_source_priorities()
        self.definitions, self.months, self.selected = OrderedDict(), OrderedDict(), OrderedDict()

    @staticmethod
    def _cached(cache, key, read, limit):
        if key not in cache:
            cache[key] = read()
            if len(cache) > limit:
                cache.popitem(last=False)
        cache.move_to_end(key)
        return cache[key]

    def resolve(self, calendar_id, day, cutoff):
        key = (calendar_id, day, cutoff)
        return self._cached(self.selected, key, lambda: self._resolve(calendar_id, day, cutoff), 128)

    def _resolve(self, calendar_id, day, cutoff):
        from app.calendars.facts import CalendarPITContext, select_pit_candidate
        from app.calendars.contracts import QueryBoundary
        from .formal_resolver import ZONE
        context = CalendarPITContext.from_query_boundary(QueryBoundary(
            data_cutoff=max(cutoff, datetime.combine(day, time(), ZONE)),
            knowledge_as_of=cutoff, include_cutoff_day=True))
        registry_rows, definition_rows = self._cached(self.definitions, calendar_id,
            lambda: (self.repository.list_registries((calendar_id,)),
                     self.repository.list_definitions((calendar_id,))), 8)
        start = day.replace(day=1)
        end = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
        candidates = self._cached(self.months, (calendar_id, start),
            lambda: self.repository.list_session_facts((calendar_id,), start, end), 4)
        def choose(rows):
            value = select_pit_candidate(rows, effective_day=day, pit_context=context,
                source_priorities=self.priorities)
            value.strict_validate()
            return value
        registry, definition = choose(registry_rows), choose(definition_rows)
        fact = choose(tuple(row for row in candidates if row.session_date == day))
        if (definition.registry_fact_id != registry.fact_id or definition.registry_version != registry.registry_version
                or fact.registry_fact_id != registry.fact_id or fact.registry_version != registry.registry_version
                or fact.definition_fact_id != definition.fact_id or fact.definition_version != definition.definition_version):
            raise ValueError('calendar_reference_mismatch')
        timezone, windows = fact.effective_timezone_and_sessions(definition)
        intervals = tuple((datetime.combine(day, window.start, ZoneInfo(timezone)),
                           datetime.combine(day, window.end, ZoneInfo(timezone))) for window in windows)
        return dict(registry=registry, definition=definition, fact=fact, timezone=timezone, intervals=intervals)
