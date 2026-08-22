"""Tests for opaque cursor tokens, digests, and the page envelope."""

import json
import unittest
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.backtesting.pagination import (
    CURSOR_VERSION,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CursorError,
    CursorPage,
    CursorQueryMismatchError,
    MalformedCursorError,
    UnsupportedCursorVersionError,
    build_cursor,
    compute_query_digest,
    normalize_limit,
    parse_cursor,
)


TS = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
KEY_KINDS = ("ts", "uuid")
UPPER_COLUMNS = {"submitted_at": "ts", "order_id": "uuid"}


def token_for(last_key, bound, digest="d" * 64) -> str:
    return build_cursor(
        query_digest=digest,
        key_kinds=KEY_KINDS,
        last_sort_key=last_key,
        upper_bound_columns=UPPER_COLUMNS,
        query_upper_bound=bound,
    )


class LimitPolicyTestCase(unittest.TestCase):
    def test_default_and_max_page_size_policy(self) -> None:
        self.assertEqual(DEFAULT_PAGE_SIZE, 100)
        self.assertEqual(MAX_PAGE_SIZE, 500)

    def test_rejects_out_of_range_or_non_integer_limits(self) -> None:
        for bad in (0, -1, 501, True, "100", 1.5):
            with self.assertRaises(ValueError):
                normalize_limit(bad)
        self.assertEqual(normalize_limit(1), 1)
        self.assertEqual(normalize_limit(500), 500)


class QueryDigestTestCase(unittest.TestCase):
    def base_payload(self) -> dict:
        return {
            "kind": "orders",
            "run_id": "0f9c6d5e-1111-4222-8333-444455556666",
            "filters": {},
            "direction": "asc",
            "sort_keys": ["submitted_at", "order_id"],
            "page_size_policy": {"default": 100, "max": 500},
            "limit": 100,
        }

    def test_digest_is_deterministic_and_order_insensitive(self) -> None:
        first = compute_query_digest(self.base_payload())
        second = compute_query_digest(dict(reversed(list(self.base_payload().items()))))
        self.assertEqual(first, second)
        self.assertEqual(first, compute_query_digest(self.base_payload()))

    def test_any_condition_change_changes_the_digest(self) -> None:
        base = compute_query_digest(self.base_payload())
        variants = []
        changed = self.base_payload(); changed["run_id"] = str(uuid4()); variants.append(changed)
        changed = self.base_payload(); changed["filters"] = {"side": "buy"}; variants.append(changed)
        changed = self.base_payload(); changed["limit"] = 200; variants.append(changed)
        changed = self.base_payload(); changed["direction"] = "desc"; variants.append(changed)
        for variant in variants:
            self.assertNotEqual(base, compute_query_digest(variant))


class CursorRoundTripTestCase(unittest.TestCase):
    def test_round_trip_preserves_typed_values(self) -> None:
        order_id = uuid4()
        token = token_for((TS, order_id), {"submitted_at": TS, "order_id": order_id})
        parsed = parse_cursor(
            token,
            expected_query_digest="d" * 64,
            key_kinds=KEY_KINDS,
            upper_bound_columns=UPPER_COLUMNS,
        )
        self.assertEqual(parsed.version, CURSOR_VERSION)
        self.assertEqual(parsed.last_sort_key[0], TS)
        self.assertEqual(parsed.last_sort_key[1], order_id)
        self.assertEqual(parsed.query_upper_bound["submitted_at"], TS)

    def test_equivalent_instants_in_different_timezones_match(self) -> None:
        from datetime import timedelta

        tz_plus_eight = timezone(timedelta(hours=8))
        local = TS.astimezone(tz_plus_eight)
        order_id = uuid4()
        token_a = token_for((TS, order_id), {"submitted_at": TS, "order_id": order_id})
        token_b = token_for(
            (local, order_id), {"submitted_at": local, "order_id": order_id}
        )
        self.assertEqual(token_a, token_b)

    def test_identical_queries_produce_identical_tokens(self) -> None:
        digest = compute_query_digest({"kind": "fills", "limit": 10})
        fill_id = uuid4()
        columns = {"timestamp": "ts", "fill_id": "uuid"}
        a = build_cursor(
            query_digest=digest,
            key_kinds=("ts", "uuid"),
            last_sort_key=(TS, fill_id),
            upper_bound_columns=columns,
            query_upper_bound={"timestamp": TS, "fill_id": fill_id},
        )
        b = build_cursor(
            query_digest=digest,
            key_kinds=("ts", "uuid"),
            last_sort_key=(TS, fill_id),
            upper_bound_columns=columns,
            query_upper_bound={"timestamp": TS, "fill_id": fill_id},
        )
        self.assertEqual(a, b)


class CursorRejectionTestCase(unittest.TestCase):
    def test_malformed_tokens_are_rejected(self) -> None:
        for bad in ("", "not-base64!!", urlsafe_b64encode(b"[not-json]").decode()):
            with self.assertRaises(MalformedCursorError):
                parse_cursor(bad)

    def test_unsupported_version_is_rejected(self) -> None:
        payload = {
            "version": CURSOR_VERSION + 1,
            "query_digest": "d" * 64,
            "last_sort_key": [],
            "query_upper_bound": {},
        }
        token = urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        with self.assertRaises(UnsupportedCursorVersionError):
            parse_cursor(token)

    def test_tampered_payload_is_detected_by_structure_checks(self) -> None:
        token = token_for((TS, uuid4()), {"submitted_at": TS, "order_id": uuid4()})
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(urlsafe_b64decode(padded))
        payload["last_sort_key"] = [{"k": "int", "v": 3}, {"k": "str", "v": "x"}]
        tampered = (
            urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        )
        with self.assertRaises(CursorError):
            parse_cursor(
                tampered,
                expected_query_digest="d" * 64,
                key_kinds=KEY_KINDS,
                upper_bound_columns=UPPER_COLUMNS,
            )

    def test_wrong_length_sort_key_is_rejected(self) -> None:
        token = build_cursor(
            query_digest="d" * 64,
            key_kinds=("ts",),
            last_sort_key=(TS,),
            upper_bound_columns=UPPER_COLUMNS,
            query_upper_bound={"submitted_at": TS, "order_id": uuid4()},
        )
        with self.assertRaises(MalformedCursorError):
            parse_cursor(
                token,
                expected_query_digest="d" * 64,
                key_kinds=KEY_KINDS,
                upper_bound_columns=UPPER_COLUMNS,
            )

    def test_query_digest_mismatch_is_rejected(self) -> None:
        token = token_for((TS, uuid4()), {"submitted_at": TS, "order_id": uuid4()})
        with self.assertRaises(CursorQueryMismatchError):
            parse_cursor(
                token,
                expected_query_digest="e" * 64,
                key_kinds=KEY_KINDS,
                upper_bound_columns=UPPER_COLUMNS,
            )

    def test_upper_bound_column_mismatch_is_rejected(self) -> None:
        token = token_for((TS, uuid4()), {"submitted_at": TS, "order_id": uuid4()})
        with self.assertRaises(MalformedCursorError):
            parse_cursor(
                token,
                expected_query_digest="d" * 64,
                key_kinds=KEY_KINDS,
                upper_bound_columns={"other_column": "ts"},
            )

    def test_naive_timestamps_are_refused_at_encoding_time(self) -> None:
        naive = datetime(2026, 5, 1, 8, 0)
        with self.assertRaises(ValueError):
            build_cursor(
                query_digest="d" * 64,
                key_kinds=("ts",),
                last_sort_key=(naive,),
                upper_bound_columns={"c": "dec"},
                query_upper_bound={"c": Decimal("1.5")},
            )


class EnvelopeTestCase(unittest.TestCase):
    def test_empty_page_shape(self) -> None:
        page = CursorPage(items=(), next_cursor=None, has_more=False)
        self.assertEqual(page.items, ())
        self.assertIsNone(page.next_cursor)
        self.assertFalse(page.has_more)


if __name__ == "__main__":
    unittest.main()
