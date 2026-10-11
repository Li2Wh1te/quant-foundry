"""Real D11 spools cross the D12 trust boundary without a second format."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from quantfoundry import _native
from quantfoundry._supervised import _transfer
from quantfoundry._worker import _run_module
from quantfoundry.api import log
from support_sdk import spec


class RuntimeResultTests(unittest.TestCase):
    def test_transfer_retains_exact_result_and_upstream_truncation(self):
        source = spec(days=5, securities=("A.SH",), frequency="1m")
        module = SimpleNamespace(
            handle_data=lambda c, d: [log.info("") for _ in range(600)]
        )
        with TemporaryDirectory() as directory:
            result = _run_module(module, source)
            self.addCleanup(result.close)
            writer = _native.TrustedResultWriter(
                directory, source["run_id"], source["result_budget_bytes"]
            )
            sequence = 1

            def call(op, body):
                nonlocal sequence
                self.assertEqual(op, "result")
                writer.write_json(
                    json.dumps(
                        dict(first_sequence=str(sequence), records=body["records"])
                    )
                )
                sequence += len(body["records"])

            summary = _transfer(result, SimpleNamespace(call=call))
            upstream = summary["results"]["notices"]
            self.assertTrue(upstream["logs_truncated"])
            writer.notices_json(json.dumps(upstream))
            writer.seal(json.dumps(summary["outcome"]))
            name = writer.retain()
            writer = None
            reader = _native.RetainedResultReader(directory, name, source["run_id"])
            transferred = json.loads(reader.summary_json())
            self.assertEqual(
                transferred["performance"], summary["results"]["performance"]
            )
            self.assertEqual(
                transferred["notices"]["logs_dropped"], upstream["logs_dropped"]
            )
            self.assertTrue(transferred["notices"]["logs_truncated"])
            page = json.loads(reader.page_json("equity", None, 1))
            self.assertEqual(page["items"][0]["equity"], "10000")
            self.assertTrue((Path(directory) / name / "manifest.json").is_file())

    def test_invalid_worker_terminal_and_required_records_refused(self):
        for bad in [
            {"status": "succeeded", "result_id": "r", "path": "/etc/passwd"},
            {"status": "unknown"},
            {"status": "cancelled", "partial": 0},
        ]:
            with self.assertRaises(_native.ContractError):
                _native.TrustedResultWriter.validate_outcome_json(json.dumps(bad))
        with TemporaryDirectory() as directory:
            writer = _native.TrustedResultWriter(
                directory, "isolated-writer", 1024 * 1024
            )
            with self.assertRaises(_native.ContractError):
                writer.write_json(
                    json.dumps(
                        dict(
                            first_sequence="1",
                            records=[dict(kind="unknown", record={})],
                        )
                    )
                )
            del writer
            self.assertEqual(list(Path(directory).iterdir()), [])
