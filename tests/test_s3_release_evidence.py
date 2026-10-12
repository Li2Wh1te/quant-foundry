"""Evidence reuse guards only; these metadata cases are not engine benchmarks."""
from copy import deepcopy
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("s3_release", Path(__file__).resolve().parents[1]/"scripts/s3_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def full_record():
    source = dict(measurement_source=dict(sha256="business-source", files={}),
        measurement_checkout_commit="2"*40, measurement_worktree_dirty=False,
        build=dict(native_source="native-source", build_architecture="x86_64", python="3.12.2",
            rust_compiler="locked", tracked_worktree_dirty=False, checkout_commit="1"*40,
            wheel=dict(native_sha256="native-binary")))
    cases = []
    # The approved fixed workloads are independent gold metadata, not outputs
    # of a replacement engine or a claim that these workloads were executed.
    for name, frequency, rows, events, callbacks, runs in (
        ("B1", "1d", 12600000, 12600000, 2520, 1),
        ("B2", "1m", 12096000, 12096000, 60480, 1),
        ("B2", "60m", 12096000, 201600, 1008, 1),
        ("B3-trade", "tick", 10000000, 10000000, 10000000, 1),
        ("B3-quote", "tick", 10000000, 10000000, 10000000, 1),
        ("B4", "1d", 37800, 37800, 1260, 16),
    ):
        kind = "handle_tick" if name.startswith("B3") else "handle_data"
        case = dict(load=dict(name=name), frequency=frequency, fixture=dict(rows=rows), passed=True,
            results=[dict(source_rows=rows, completed_events=events,
                telemetry=dict(python_callbacks=dict(by_kind={kind:dict(count=callbacks)}))) for _ in range(runs)])
        if name.startswith("B3"):
            case["hot_correction"] = dict(probe_status="failed", probe_error="DATA_CHANGED",
                probe_python_callbacks=100, corrected_read_verified=True)
        if name == "B4":
            case["first"] = dict(states=[dict(status="succeeded") for _ in range(15)]+[dict(status="failed")], peak_workers=2)
            case["retry"] = dict(states=[dict(status="succeeded")])
        cases.append(case)
    return dict(mode="full", passed=True, full_B1_B4=True,
        topology="runner_container_to_sibling_workers", resource_budget=dict(cpu=4, memory_bytes=8*1024**3),
        source=source, cases=cases)


class EvidenceReuseTests(unittest.TestCase):
    def test_account_fee_fixture_change_rejects_reuse_with_unchanged_native(self):
        with TemporaryDirectory(prefix="s3-evidence-helper-") as directory:
            root = Path(directory)
            (root/"tests").mkdir()
            helper = root/"tests/s3_runservice_suite.py"
            helper.write_text("minimum_commission = '1'\n")
            with patch.object(release, "ROOT", root):
                before = release.sources(native=False)
                native = release.sources()
                helper.write_text("minimum_commission = '9'\n")
                after = release.sources(native=False)
                self.assertEqual(native, release.sources())
            record = full_record()
            record["source"]["measurement_source"] = before
            current = deepcopy(record["source"])
            current["measurement_source"] = after
            with self.assertRaises(AssertionError):
                release.verify_full_record(record, current)

    def test_unchanged_clean_measurement_can_keep_distinct_original_wheel_origin(self):
        record = full_record()
        current = deepcopy(record["source"])
        current["measurement_checkout_commit"] = "3"*40  # documentation-only checkout
        release.verify_full_record(record, current)
        self.assertNotEqual(record["source"]["measurement_checkout_commit"], record["source"]["build"]["checkout_commit"])

    def test_clean_original_wheel_never_covers_dirty_or_unknown_measurement(self):
        for target in ("record", "current"):
            for value in (True, None, "false"):
                with self.subTest(target=target, value=value):
                    record = full_record();current = deepcopy(record["source"])
                    changed = record["source"] if target == "record" else current
                    changed["measurement_worktree_dirty"] = value
                    with self.assertRaisesRegex(AssertionError, "dirty or unknown measurement"):
                        release.verify_full_record(record, current)

    def test_full_marker_never_covers_reduced_tick_callbacks_or_partial_cases(self):
        for reduction in ("tick", "cases", "probe"):
            with self.subTest(reduction=reduction):
                record = full_record();current = deepcopy(record["source"])
                if reduction == "tick":
                    record["cases"][3]["results"][0]["telemetry"]["python_callbacks"]["by_kind"]["handle_tick"]["count"] = 9999999
                elif reduction == "cases":record["cases"].pop()
                else:record["filesystem_probe_substitute"] = True
                with self.assertRaises(AssertionError):release.verify_full_record(record, current)
