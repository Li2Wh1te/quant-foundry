"""Tests for the named-calendar domain model and ``strict_compatible@1``."""

import unittest
from dataclasses import FrozenInstanceError
from datetime import date, time, timedelta, timezone

from app.backtesting.calendar_axis import (
    _format_time,
    POLICY_KEY_STRICT_COMPATIBLE,
    POLICY_VERSION_STRICT_COMPATIBLE,
    CalendarAxisDifference,
    CalendarAxisDifferenceField,
    CalendarAxisResolution,
    CalendarAxisStatus,
    CalendarDefinition,
    CalendarSessionFact,
    InMemoryCalendarAxisDataProvider,
    SessionPoint,
    SessionWindow,
    normalize_session_windows,
    register_calendar_axis_policy,
    resolve_calendar_axis,
    resolve_strict_compatible_axis,
)
from app.backtesting.domain import DomainValidationError

CHINA_SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))
FULL_DAY = ((time(9, 30), time(15, 0)),)
VERSION = "china_exchange_daily@1"

D16 = date(2026, 8, 16)
D17 = date(2026, 8, 17)
D18 = date(2026, 8, 18)
D19 = date(2026, 8, 19)


def make_definition(
    calendar_id="SSE",
    *,
    definition_version=VERSION,
    timezone="Asia/Shanghai",
    default_sessions=CHINA_SESSIONS,
    valid_from=None,
    valid_to=None,
):
    return CalendarDefinition(
        calendar_id=calendar_id,
        definition_version=definition_version,
        timezone=timezone,
        default_sessions=default_sessions,
        valid_from=valid_from,
        valid_to=valid_to,
        source="test",
    )


def make_fact(
    calendar_id,
    session_date,
    is_open,
    *,
    definition_version=VERSION,
    timezone_override=None,
    sessions_override=None,
):
    return CalendarSessionFact(
        calendar_id=calendar_id,
        session_date=session_date,
        is_open=is_open,
        definition_version=definition_version,
        timezone_override=timezone_override,
        sessions_override=sessions_override,
        source="test",
    )


def build_provider(schedule):
    """Build an in-memory provider from ``{calendar_id: [(date, is_open)]}``."""

    definitions = []
    facts = []
    for calendar_id, days in schedule.items():
        definitions.append(make_definition(calendar_id))
        for day, is_open in days:
            facts.append(make_fact(calendar_id, day, is_open))
    return InMemoryCalendarAxisDataProvider(definitions, facts)


def resolve(provider, start=D17, end=D19, calendar_ids=("SSE", "SZSE")):
    return resolve_calendar_axis(
        provider,
        policy_key=POLICY_KEY_STRICT_COMPATIBLE,
        policy_version=POLICY_VERSION_STRICT_COMPATIBLE,
        start_date=start,
        end_date=end,
        calendar_ids=calendar_ids,
    )


def make_resolution(**overrides):
    """Build a minimal self-consistent resolution for invariant tests."""

    values = dict(
        policy_key="strict_compatible",
        policy_version="1",
        start_date=D17,
        end_date=D17,
        calendar_ids=("SSE",),
        session_signature="a" * 64,
        timezone="Asia/Shanghai",
        resolved_sessions=(
            SessionPoint(D17, "2026-08-17", "Asia/Shanghai", CHINA_SESSIONS),
        ),
        status=CalendarAxisStatus.COMPATIBLE,
        differences=(),
    )
    values.update(overrides)
    return CalendarAxisResolution(**values)


class SessionWindowTestCase(unittest.TestCase):
    def test_rejects_reversed_window(self) -> None:
        with self.assertRaises(DomainValidationError):
            SessionWindow(time(15, 0), time(9, 30))

    def test_rejects_empty_window(self) -> None:
        with self.assertRaises(DomainValidationError):
            SessionWindow(time(9, 30), time(9, 30))

    def test_rejects_non_time_boundaries(self) -> None:
        with self.assertRaises(DomainValidationError):
            SessionWindow("09:30", time(11, 30))

    def test_blank_label_becomes_none(self) -> None:
        window = SessionWindow(time(9, 30), time(11, 30), label="   ")
        self.assertIsNone(window.label)

    def test_normalization_sorts_equivalent_representations(self) -> None:
        ordered = normalize_session_windows(CHINA_SESSIONS, "sessions")
        shuffled = normalize_session_windows(
            ((time(13, 0), time(15, 0)), (time(9, 30), time(11, 30))), "sessions"
        )
        self.assertEqual(ordered, shuffled)

    def test_normalization_rejects_overlaps(self) -> None:
        with self.assertRaises(DomainValidationError):
            normalize_session_windows(
                ((time(9, 30), time(12, 0)), (time(11, 30), time(15, 0))),
                "sessions",
            )

    def test_normalization_converts_malformed_entries_to_domain_error(self) -> None:
        for bad_value in (
            (time(9, 30), time(11, 30), "extra", "another"),  # too many fields
            (42,),  # neither SessionWindow nor tuple
            42,  # not iterable at all
        ):
            with self.assertRaises(DomainValidationError):
                normalize_session_windows(bad_value, "sessions")

    def test_minute_precision_normalizes_to_hh_mm(self) -> None:
        self.assertEqual(_format_time(time(9, 30)), "09:30")
        self.assertEqual(_format_time(time(13, 0)), "13:00")

    def test_sub_minute_precision_is_preserved(self) -> None:
        self.assertEqual(_format_time(time(9, 30, 30)), "09:30:30")
        self.assertEqual(_format_time(time(9, 30, 30, 500000)), "09:30:30.500000")

    def test_rejects_timezone_aware_boundaries(self) -> None:
        aware = time(9, 30, tzinfo=timezone(timedelta(hours=8)))
        with self.assertRaises(DomainValidationError):
            SessionWindow(aware, time(11, 30))

    def test_mixed_naive_and_aware_entries_raise_domain_error(self) -> None:
        aware = time(10, 0, tzinfo=timezone.utc)
        with self.assertRaises(DomainValidationError):
            normalize_session_windows(
                ((time(9, 30), time(10, 30)), (aware, time(11, 30))),
                "sessions",
            )


class CalendarDefinitionTestCase(unittest.TestCase):
    def test_rejects_blank_calendar_id(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_definition("   ")

    def test_rejects_blank_definition_version(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_definition(definition_version="")

    def test_rejects_unresolvable_timezone(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_definition(timezone="Mars/Olympus_Mons")

    def test_rejects_invalid_validity_range(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_definition(valid_from=D19, valid_to=D17)

    def test_applies_to_respects_validity_window(self) -> None:
        definition = make_definition(valid_from=D18)
        self.assertFalse(definition.applies_to(D17))
        self.assertTrue(definition.applies_to(D18))


class CalendarSessionFactTestCase(unittest.TestCase):
    def test_requires_explicit_is_open(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_fact("SSE", D18, None)

    def test_rejects_non_boolean_is_open(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_fact("SSE", D18, "open")

    def test_rejects_non_date_session_date(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_fact("SSE", "2026-08-18", False)

    def test_rejects_invalid_timezone_override(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_fact("SSE", D18, True, timezone_override="Not/AZone")

    def test_rejects_invalid_sessions_override(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_fact(
                "SSE",
                D18,
                True,
                sessions_override=((time(10, 0), time(11, 0)), (time(9, 30), time(10, 30))),
            )

    def test_closed_day_may_carry_overrides_without_effect(self) -> None:
        fact = make_fact(
            "SSE",
            D18,
            False,
            timezone_override="Asia/Shanghai",
            sessions_override=FULL_DAY,
        )
        self.assertFalse(fact.is_open)


class ImmutabilityTestCase(unittest.TestCase):
    def test_domain_objects_are_frozen(self) -> None:
        resolution = resolve(build_provider({
            "SSE": [(D17, True), (D18, False), (D19, True)],
            "SZSE": [(D17, True), (D18, False), (D19, True)],
        }))
        # Frozen slots dataclasses raise FrozenInstanceError when an
        # existing field is reassigned.
        objects = [
            (SessionWindow(time(9, 30), time(11, 30)), "start_time"),
            (make_definition(), "calendar_id"),
            (make_fact("SSE", D17, True), "is_open"),
            (
                SessionPoint(D17, "2026-08-17", "Asia/Shanghai", CHINA_SESSIONS),
                "session_id",
            ),
            (resolution, "status"),
        ]
        if resolution.differences:
            objects.append((resolution.differences[0], "actual_value"))
        for obj, field_name in objects:
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, field_name, None)

    def test_resolution_rejects_sessions_on_incompatible_result(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_resolution(
                status=CalendarAxisStatus.INCOMPATIBLE,
                session_signature="",
                timezone=None,
                resolved_sessions=(
                    SessionPoint(D17, "2026-08-17", "Asia/Shanghai", CHINA_SESSIONS),
                ),
                differences=(
                    CalendarAxisDifference(
                        date=D17,
                        calendar_id="SSE",
                        field=CalendarAxisDifferenceField.MISSING_FACT,
                        actual_value="missing",
                        expected_value="present",
                    ),
                ),
            )

    def test_resolution_rejects_start_after_end(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_resolution(start_date=D19, end_date=D17)

    def test_resolution_rejects_compatible_result_with_differences(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_resolution(
                differences=(
                    CalendarAxisDifference(
                        date=D17,
                        calendar_id="SSE",
                        field=CalendarAxisDifferenceField.TIMEZONE,
                        actual_value="Asia/Tokyo",
                        expected_value="Asia/Shanghai",
                    ),
                ),
            )

    def test_resolution_rejects_incompatible_result_without_differences(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_resolution(
                status=CalendarAxisStatus.INCOMPATIBLE,
                session_signature="",
                timezone=None,
                resolved_sessions=(),
            )

    def test_resolution_rejects_session_outside_requested_range(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_resolution(
                end_date=D17,
                resolved_sessions=(
                    SessionPoint(D19, "2026-08-19", "Asia/Shanghai", CHINA_SESSIONS),
                ),
            )

    def test_resolution_rejects_session_id_not_matching_date(self) -> None:
        with self.assertRaises(DomainValidationError):
            make_resolution(
                resolved_sessions=(
                    SessionPoint(D17, "not-a-date-id", "Asia/Shanghai", CHINA_SESSIONS),
                ),
            )


class StrictCompatibleHappyPathTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = build_provider({
            "SSE": [(D17, True), (D18, False), (D19, True)],
            "SZSE": [(D17, True), (D18, False), (D19, True)],
        })

    def test_identical_calendars_are_compatible(self) -> None:
        resolution = resolve(self.provider)
        self.assertIs(resolution.status, CalendarAxisStatus.COMPATIBLE)
        self.assertEqual(resolution.differences, ())
        self.assertEqual(resolution.policy_key, "strict_compatible")
        self.assertEqual(resolution.policy_version, "1")
        self.assertEqual(resolution.calendar_ids, ("SSE", "SZSE"))
        self.assertEqual(resolution.timezone, "Asia/Shanghai")
        # The closed day never enters the common session sequence.
        self.assertEqual(
            [point.session_date for point in resolution.resolved_sessions],
            [D17, D19],
        )
        for point in resolution.resolved_sessions:
            self.assertEqual(point.session_id, point.session_date.isoformat())
            self.assertEqual(point.sessions, normalize_session_windows(CHINA_SESSIONS, "s"))

    def test_single_named_calendar_passes(self) -> None:
        provider = build_provider({
            "SSE": [(D17, True), (D18, False), (D19, True)],
        })
        resolution = resolve(provider, calendar_ids=("SSE",))
        self.assertIs(resolution.status, CalendarAxisStatus.COMPATIBLE)
        self.assertEqual(len(resolution.resolved_sessions), 2)

    def test_input_order_does_not_change_result_or_signature(self) -> None:
        first = resolve(self.provider, calendar_ids=("SSE", "SZSE"))
        second = resolve(self.provider, calendar_ids=("SZSE", "SSE"))
        self.assertEqual(first.status, second.status)
        self.assertEqual(first.resolved_sessions, second.resolved_sessions)
        self.assertEqual(first.session_signature, second.session_signature)
        self.assertEqual(first.calendar_ids, second.calendar_ids)

    def test_signature_is_repeatable_and_excludes_non_business_fields(self) -> None:
        first = resolve(self.provider).session_signature
        second = resolve(self.provider).session_signature
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)  # SHA-256 hex digest

    def test_equivalent_session_representations_share_signature(self) -> None:
        base = resolve(self.provider).session_signature
        provider = InMemoryCalendarAxisDataProvider(
            [make_definition(), make_definition("SZSE")],
            [
                make_fact(
                    "SSE",
                    D17,
                    True,
                    sessions_override=((time(13, 0), time(15, 0)), (time(9, 30), time(11, 30))),
                ),
                make_fact("SSE", D18, False),
                make_fact("SSE", D19, True),
                make_fact("SZSE", D17, True),
                make_fact("SZSE", D18, False),
                make_fact("SZSE", D19, True),
            ],
        )
        self.assertEqual(resolve(provider).session_signature, base)


    def test_label_does_not_affect_compatibility(self) -> None:
        provider = InMemoryCalendarAxisDataProvider(
            [
                make_definition("SSE"),
                make_definition(
                    "SZSE",
                    default_sessions=(
                        SessionWindow(time(9, 30), time(11, 30), label="morning"),
                        SessionWindow(time(13, 0), time(15, 0), label="afternoon"),
                    ),
                ),
            ],
            [
                make_fact(cid, day, True)
                for cid in ("SSE", "SZSE")
                for day in (D17, D19)
            ]
            + [make_fact(cid, D18, False) for cid in ("SSE", "SZSE")],
        )
        resolution = resolve(provider)
        self.assertIs(resolution.status, CalendarAxisStatus.COMPATIBLE)

    def test_second_level_boundaries_change_the_signature(self) -> None:
        minute_provider = build_provider({"SSE": [(D17, True)]})
        second_provider = InMemoryCalendarAxisDataProvider(
            [make_definition()],
            [
                make_fact(
                    "SSE",
                    D17,
                    True,
                    sessions_override=((time(9, 30, 30), time(15, 0)),),
                )
            ],
        )
        minute_resolution = resolve(minute_provider, end=D17, calendar_ids=("SSE",))
        second_resolution = resolve(second_provider, end=D17, calendar_ids=("SSE",))
        self.assertNotEqual(
            minute_resolution.session_signature,
            second_resolution.session_signature,
        )


class StrictCompatibleRejectionTestCase(unittest.TestCase):
    def assert_incompatible_with_field(
        self, resolution, field, *, dates=None
    ) -> None:
        self.assertIs(resolution.status, CalendarAxisStatus.INCOMPATIBLE)
        self.assertEqual(resolution.resolved_sessions, ())
        self.assertIsNone(resolution.timezone)
        self.assertEqual(resolution.session_signature, "")
        fields = {difference.field for difference in resolution.differences}
        self.assertIn(field, fields)
        if dates is not None:
            self.assertEqual(
                {difference.date for difference in resolution.differences}, set(dates)
            )
        for difference in resolution.differences:
            self.assertIsNotNone(difference.actual_value)
            self.assertIsNotNone(difference.expected_value)

    def test_example_b_open_status_mismatch_blocks(self) -> None:
        provider = build_provider({
            "SSE": [(D17, True), (D18, True), (D19, True)],
            "SZSE": [(D17, True), (D18, False), (D19, True)],
        })
        resolution = resolve(provider)
        self.assert_incompatible_with_field(
            resolution, CalendarAxisDifferenceField.IS_OPEN, dates={D18}
        )
        mismatch = next(
            d
            for d in resolution.differences
            if d.field is CalendarAxisDifferenceField.IS_OPEN
        )
        self.assertIn(mismatch.calendar_id, {"SSE", "SZSE"})
        self.assertEqual(
            {mismatch.actual_value, mismatch.expected_value}, {"true", "false"}
        )

    def test_no_implicit_union_of_disjoint_open_days(self) -> None:
        provider = build_provider({
            "SSE": [(D17, True), (D18, False), (D19, False)],
            "SZSE": [(D17, False), (D18, False), (D19, True)],
        })
        resolution = resolve(provider)
        self.assert_incompatible_with_field(
            resolution,
            CalendarAxisDifferenceField.IS_OPEN,
            dates={D17, D19},
        )
        self.assertEqual(resolution.resolved_sessions, ())

    def test_example_c_session_mismatch_blocks(self) -> None:
        definitions = [
            make_definition("SSE"),
            make_definition("SZSE", default_sessions=FULL_DAY),
        ]
        facts = [
            make_fact(cid, day, True)
            for cid in ("SSE", "SZSE")
            for day in (D17, D19)
        ] + [
            make_fact(cid, D18, False) for cid in ("SSE", "SZSE")
        ]
        resolution = resolve(InMemoryCalendarAxisDataProvider(definitions, facts))
        self.assert_incompatible_with_field(
            resolution, CalendarAxisDifferenceField.SESSIONS, dates={D17, D19}
        )

    def test_timezone_mismatch_on_open_day_blocks(self) -> None:
        definitions = [
            make_definition("SSE"),
            make_definition("SZSE", timezone="Asia/Tokyo"),
        ]
        facts = [
            make_fact(cid, day, True)
            for cid in ("SSE", "SZSE")
            for day in (D17, D19)
        ] + [make_fact(cid, D18, False) for cid in ("SSE", "SZSE")]
        resolution = resolve(InMemoryCalendarAxisDataProvider(definitions, facts))
        self.assert_incompatible_with_field(
            resolution, CalendarAxisDifferenceField.TIMEZONE, dates={D17, D19}
        )

    def test_example_d_missing_fact_blocks_even_with_template(self) -> None:
        # The definition declares default sessions, but 8/18 has no explicit
        # fact for SSE; the template must not be used to guess openness.
        provider = InMemoryCalendarAxisDataProvider(
            [make_definition("SSE"), make_definition("SZSE")],
            [
                make_fact("SSE", D17, True),
                make_fact("SSE", D19, True),
                make_fact("SZSE", D17, True),
                make_fact("SZSE", D18, False),
                make_fact("SZSE", D19, True),
            ],
        )
        resolution = resolve(provider)
        self.assert_incompatible_with_field(
            resolution, CalendarAxisDifferenceField.MISSING_FACT, dates={D18}
        )

    def test_missing_definition_version_blocks(self) -> None:
        provider = InMemoryCalendarAxisDataProvider(
            [make_definition("SSE"), make_definition("SZSE")],
            [
                make_fact("SSE", D17, True, definition_version="unknown@9"),
                make_fact("SSE", D18, False),
                make_fact("SSE", D19, True),
                make_fact("SZSE", D17, True),
                make_fact("SZSE", D18, False),
                make_fact("SZSE", D19, True),
            ],
        )
        resolution = resolve(provider)
        self.assert_incompatible_with_field(
            resolution, CalendarAxisDifferenceField.MISSING_DEFINITION, dates={D17}
        )

    def test_definition_outside_validity_window_blocks(self) -> None:
        provider = InMemoryCalendarAxisDataProvider(
            [make_definition("SSE", valid_to=D16), make_definition("SZSE")],
            [
                make_fact("SSE", D17, True),
                make_fact("SSE", D18, False),
                make_fact("SSE", D19, True),
                make_fact("SZSE", D17, True),
                make_fact("SZSE", D18, False),
                make_fact("SZSE", D19, True),
            ],
        )
        resolution = resolve(provider)
        # Definition resolution is required on every natural day, including
        # closed ones, so the expired definition blocks the whole range.
        self.assert_incompatible_with_field(
            resolution,
            CalendarAxisDifferenceField.MISSING_DEFINITION,
            dates={D17, D18, D19},
        )

    def test_ambiguous_applicable_definitions_block(self) -> None:
        provider = InMemoryCalendarAxisDataProvider(
            [
                make_definition("SSE"),
                make_definition("SSE"),
                make_definition("SZSE"),
            ],
            [
                make_fact("SSE", D17, True),
                make_fact("SSE", D18, False),
                make_fact("SSE", D19, True),
                make_fact("SZSE", D17, True),
                make_fact("SZSE", D18, False),
                make_fact("SZSE", D19, True),
            ],
        )
        resolution = resolve(provider)
        # Definition resolution is required on every natural day, including
        # closed ones, so the ambiguity blocks the whole range.
        self.assert_incompatible_with_field(
            resolution,
            CalendarAxisDifferenceField.MISSING_DEFINITION,
            dates={D17, D18, D19},
        )

    def test_open_day_without_resolvable_sessions_blocks(self) -> None:
        provider = InMemoryCalendarAxisDataProvider(
            [make_definition("SSE", default_sessions=()), make_definition("SZSE")],
            [
                make_fact("SSE", D17, True),
                make_fact("SSE", D18, False),
                make_fact("SSE", D19, True),
                make_fact("SZSE", D17, True),
                make_fact("SZSE", D18, False),
                make_fact("SZSE", D19, True),
            ],
        )
        resolution = resolve(provider)
        self.assert_incompatible_with_field(
            resolution, CalendarAxisDifferenceField.UNRESOLVED_SESSION, dates={D17, D19}
        )

    def test_closed_day_irrelevant_overrides_do_not_block(self) -> None:
        provider = InMemoryCalendarAxisDataProvider(
            [make_definition("SSE"), make_definition("SZSE")],
            [
                make_fact("SSE", D17, True),
                make_fact(
                    "SSE",
                    D18,
                    False,
                    timezone_override="Asia/Tokyo",
                    sessions_override=FULL_DAY,
                ),
                make_fact("SSE", D19, True),
                make_fact("SZSE", D17, True),
                make_fact("SZSE", D18, False),
                make_fact("SZSE", D19, True),
            ],
        )
        resolution = resolve(provider)
        self.assertIs(resolution.status, CalendarAxisStatus.COMPATIBLE)

    def test_sub_minute_precision_is_preserved_in_reports(self) -> None:
        definitions = [
            make_definition("SSE"),
            make_definition(
                "SZSE",
                default_sessions=(
                    (time(9, 30, 30), time(11, 30)),
                    (time(13, 0), time(15, 0)),
                ),
            ),
        ]
        facts = [
            make_fact(cid, day, True)
            for cid in ("SSE", "SZSE")
            for day in (D17, D19)
        ] + [make_fact(cid, D18, False) for cid in ("SSE", "SZSE")]
        resolution = resolve(InMemoryCalendarAxisDataProvider(definitions, facts))
        self.assert_incompatible_with_field(
            resolution, CalendarAxisDifferenceField.SESSIONS, dates={D17, D19}
        )
        session_diff = next(
            d
            for d in resolution.differences
            if d.field is CalendarAxisDifferenceField.SESSIONS
        )
        # The second-level difference must be visible in the report instead
        # of collapsing into identical HH:MM texts.
        self.assertNotEqual(session_diff.actual_value, session_diff.expected_value)
        self.assertIn("09:30:30", session_diff.actual_value or "")

    def test_differences_are_stably_sorted(self) -> None:
        provider = build_provider({
            "SSE": [(D17, True), (D18, True), (D19, True)],
            "SZSE": [(D17, False), (D18, False), (D19, True)],
        })
        forward = resolve(provider, calendar_ids=("SSE", "SZSE"))
        backward = resolve(provider, calendar_ids=("SZSE", "SSE"))
        keys = [difference.sort_key for difference in forward.differences]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(keys, [d.sort_key for d in backward.differences])


class RequestValidationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = build_provider({
            "SSE": [(D17, True), (D18, False), (D19, True)],
        })

    def test_rejects_start_after_end(self) -> None:
        with self.assertRaises(DomainValidationError):
            resolve(self.provider, start=D19, end=D17)

    def test_rejects_empty_calendar_ids(self) -> None:
        with self.assertRaises(DomainValidationError):
            resolve(self.provider, calendar_ids=())

    def test_rejects_blank_calendar_id(self) -> None:
        with self.assertRaises(DomainValidationError):
            resolve(self.provider, calendar_ids=("SSE", "  "))

    def test_duplicate_calendar_ids_are_deduplicated(self) -> None:
        resolution = resolve(self.provider, calendar_ids=("SSE", "SSE"))
        self.assertEqual(resolution.calendar_ids, ("SSE",))
        self.assertIs(resolution.status, CalendarAxisStatus.COMPATIBLE)


class PolicyRegistryTestCase(unittest.TestCase):
    def test_strict_compatible_v1_is_registered(self) -> None:
        provider = build_provider({"SSE": [(D17, True)]})
        resolution = resolve_calendar_axis(
            provider,
            policy_key="strict_compatible",
            policy_version="1",
            start_date=D17,
            end_date=D17,
            calendar_ids=("SSE",),
        )
        self.assertIs(resolution.status, CalendarAxisStatus.COMPATIBLE)

    def test_unknown_policy_is_rejected(self) -> None:
        provider = build_provider({"SSE": [(D17, True)]})
        with self.assertRaises(DomainValidationError):
            resolve_calendar_axis(
                provider,
                policy_key="composite_union",
                policy_version="1",
                start_date=D17,
                end_date=D17,
                calendar_ids=("SSE",),
            )

    def test_duplicate_registration_is_rejected(self) -> None:
        with self.assertRaises(DomainValidationError):
            register_calendar_axis_policy(
                "strict_compatible", "1", resolve_strict_compatible_axis
            )


if __name__ == "__main__":
    unittest.main()
