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
    sign_token,
)


TS = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
KEY_KINDS = ("ts", "uuid")
UPPER_COLUMNS = {"submitted_at": "ts", "order_id": "uuid"}
SIGNING_KEY = "unit-test-signing-key"


def token_for(last_key, bound, digest="d" * 64, signing_key=SIGNING_KEY) -> str:
    return build_cursor(
        signing_key=signing_key,
        query_digest=digest,
        key_kinds=KEY_KINDS,
        last_sort_key=last_key,
        upper_bound_columns=UPPER_COLUMNS,
        query_upper_bound=bound,
    )


def parse(token, **overrides):
    kwargs = {
        "signing_key": SIGNING_KEY,
        "expected_query_digest": "d" * 64,
        "key_kinds": KEY_KINDS,
        "upper_bound_columns": UPPER_COLUMNS,
    }
    kwargs.update(overrides)
    return parse_cursor(token, **kwargs)


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
        parsed = parse(token)
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
            signing_key=SIGNING_KEY,
            query_digest=digest,
            key_kinds=("ts", "uuid"),
            last_sort_key=(TS, fill_id),
            upper_bound_columns=columns,
            query_upper_bound={"timestamp": TS, "fill_id": fill_id},
        )
        b = build_cursor(
            signing_key=SIGNING_KEY,
            query_digest=digest,
            key_kinds=("ts", "uuid"),
            last_sort_key=(TS, fill_id),
            upper_bound_columns=columns,
            query_upper_bound={"timestamp": TS, "fill_id": fill_id},
        )
        self.assertEqual(a, b)


def _split(token: str) -> tuple[str, dict, str]:
    encoded_payload, signature = token.split(".")
    padded = encoded_payload + "=" * (-len(encoded_payload) % 4)
    payload = json.loads(urlsafe_b64decode(padded))
    return encoded_payload, payload, signature


def _reencode_without_signature(payload: dict) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return urlsafe_b64encode(canonical.encode()).decode().rstrip("=")


class CursorRejectionTestCase(unittest.TestCase):
    def test_malformed_tokens_are_rejected(self) -> None:
        for bad in (
            "",
            "not-base64!!",
            urlsafe_b64encode(b"[not-json]").decode(),
            # Unsigned legacy tokens must not be accepted either.
            urlsafe_b64encode(b"{}").decode().rstrip("="),
        ):
            with self.assertRaises(MalformedCursorError):
                parse(bad)

    def test_missing_or_garbled_signature_is_rejected(self) -> None:
        token = token_for((TS, uuid4()), {"submitted_at": TS, "order_id": uuid4()})
        encoded_payload, _, _ = _split(token)
        with self.assertRaises(MalformedCursorError):
            parse(encoded_payload)

    def test_wrong_signing_key_is_rejected(self) -> None:
        token = token_for((TS, uuid4()), {"submitted_at": TS, "order_id": uuid4()})
        with self.assertRaises(MalformedCursorError):
            parse(token, signing_key="a-different-key")

    def test_legitimate_value_tampering_is_rejected(self) -> None:
        """Swapping in another valid UUID/timestamp must not be accepted."""

        original_bound_id = uuid4()
        token = token_for(
            (TS, original_bound_id), {"submitted_at": TS, "order_id": original_bound_id}
        )
        _, payload, signature = _split(token)

        # Rewrite the bound to a different but perfectly valid UUID and
        # timestamp, keeping the original signature untouched.
        forged_payload = json.loads(json.dumps(payload))
        forged_payload["query_upper_bound"]["order_id"] = {
            "k": "uuid",
            "v": str(uuid4()),
        }
        forged_payload["last_sort_key"][0] = {
            "k": "ts",
            "v": "2026-05-02T08:00:00+00:00",
        }
        forged = f"{_reencode_without_signature(forged_payload)}.{signature}"
        with self.assertRaises(MalformedCursorError):
            parse(forged)

    def test_re_signing_with_a_foreign_key_does_not_validate(self) -> None:
        token = token_for((TS, uuid4()), {"submitted_at": TS, "order_id": uuid4()})
        _, payload, _ = _split(token)
        foreign = build_cursor(
            signing_key="attacker-key",
            query_digest=payload["query_digest"],
            key_kinds=KEY_KINDS,
            last_sort_key=(TS, uuid4()),
            upper_bound_columns=UPPER_COLUMNS,
            query_upper_bound={"submitted_at": TS, "order_id": uuid4()},
        )
        with self.assertRaises(MalformedCursorError):
            parse(foreign)

    def test_unsupported_version_is_rejected(self) -> None:
        # A properly signed token with an unsupported version must still be
        # rejected by the version check itself.
        forged_payload = {
            "version": CURSOR_VERSION + 1,
            "query_digest": "d" * 64,
            "last_sort_key": [],
            "query_upper_bound": {},
        }
        token = sign_token(forged_payload, SIGNING_KEY)
        with self.assertRaises(UnsupportedCursorVersionError):
            parse(token)

    def test_tampered_payload_is_detected_by_structure_checks(self) -> None:
        token = token_for((TS, uuid4()), {"submitted_at": TS, "order_id": uuid4()})
        encoded_payload, payload, _ = _split(token)
        payload["last_sort_key"] = [{"k": "int", "v": 3}, {"k": "str", "v": "x"}]
        tampered = f"{_reencode_without_signature(payload)}.{token.split('.')[1]}"
        with self.assertRaises(CursorError):
            parse(tampered)

    def test_wrong_length_sort_key_is_rejected(self) -> None:
        token = build_cursor(
            signing_key=SIGNING_KEY,
            query_digest="d" * 64,
            key_kinds=("ts",),
            last_sort_key=(TS,),
            upper_bound_columns=UPPER_COLUMNS,
            query_upper_bound={"submitted_at": TS, "order_id": uuid4()},
        )
        with self.assertRaises(MalformedCursorError):
            parse(token)

    def test_query_digest_mismatch_is_rejected(self) -> None:
        token = token_for((TS, uuid4()), {"submitted_at": TS, "order_id": uuid4()})
        with self.assertRaises(CursorQueryMismatchError):
            parse(token, expected_query_digest="e" * 64)

    def test_upper_bound_column_mismatch_is_rejected(self) -> None:
        token = token_for((TS, uuid4()), {"submitted_at": TS, "order_id": uuid4()})
        with self.assertRaises(MalformedCursorError):
            parse(token, upper_bound_columns={"other_column": "ts"})

    def test_naive_timestamps_are_refused_at_encoding_time(self) -> None:
        naive = datetime(2026, 5, 1, 8, 0)
        with self.assertRaises(ValueError):
            build_cursor(
                signing_key=SIGNING_KEY,
                query_digest="d" * 64,
                key_kinds=("ts",),
                last_sort_key=(naive,),
                upper_bound_columns={"c": "dec"},
                query_upper_bound={"c": Decimal("1.5")},
            )

    def test_non_ascii_tokens_map_to_malformed_cursor(self) -> None:
        # Non-ASCII content must surface as CursorError (HTTP 400), never as
        # an underlying UnicodeEncodeError leaking through as HTTP 422.
        for bad in ("é.00", "abc.é", "日本語.test", "café"):
            with self.assertRaises(MalformedCursorError):
                parse(bad)

    def test_blank_signing_keys_are_refused(self) -> None:
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                build_cursor(
                    signing_key=bad,
                    query_digest="d" * 64,
                    key_kinds=(),
                    last_sort_key=(),
                    upper_bound_columns={},
                    query_upper_bound={},
                )


class EnvelopeTestCase(unittest.TestCase):
    def test_empty_page_shape(self) -> None:
        page = CursorPage(items=(), next_cursor=None, has_more=False)
        self.assertEqual(page.items, ())
        self.assertIsNone(page.next_cursor)
        self.assertFalse(page.has_more)


if __name__ == "__main__":
    unittest.main()
