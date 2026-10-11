"""Installed wheel HTTP/CLI boundary tests; actual D13 is tested separately."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import traceback
import unittest
from uuid import uuid4

from quantfoundry import BacktestClient, BacktestError, ContractError, RunConfig

TOKEN = "d14-transport-test-credential-with-reserved+/="
ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def peer(responder=None):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self): self.respond()
        def do_POST(self): self.respond()
        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append((self.command, self.path, dict(self.headers), json.loads(body) if body else None))
            status, headers, data = responder(calls[-1]) if responder else (200, {}, json.dumps({"items": [], "exact": "1767225600000000123", "money": "0.333333333333"}).encode())
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for key, value in headers.items(): self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


class ClientTests(unittest.TestCase):
    def config(self):
        return RunConfig.from_dict(json.loads((ROOT/"examples/strategies/run_configs.json").read_text())[0]["config"])

    def test_every_route_same_native_config_headers_bounded_pages_and_exact_json(self):
        run, batch = str(uuid4()), str(uuid4())
        config = self.config()
        with peer() as (url, calls):
            client = BacktestClient(base_url=url, token=TOKEN)
            client.capabilities(); client.preflight(config)
            client.submit(config, idempotency_key="route-test")
            client.status(run); client.cancel(run)
            result = client.result(run, kind="trades", limit=2, cursor="cursor+/=")
            client.batch([config], idempotency_key="batch-test")
            client.batch_status(batch, limit=2, cursor="cursor+/=")
            client.cancel_batch(batch); client.retry_failed(batch, idempotency_key="retry-test")
            client.compare([run, batch])
            self.assertEqual(result["exact"], "1767225600000000123")
            self.assertEqual(result["money"], "0.333333333333")
            self.assertEqual(calls[1][3], calls[2][3])
            self.assertEqual(calls[2][3], config.to_dict())
            self.assertEqual(calls[6][3], {"configs": [config.to_dict()]})
            self.assertIn("cursor=cursor%2B%2F%3D", calls[5][1])
            self.assertTrue(all(c[2]["Authorization"] == "Bearer "+TOKEN for c in calls))
            self.assertNotIn(TOKEN, repr(client))
            self.assertNotIn(TOKEN, repr(client._http))

    def test_redirect_never_reaches_destination_or_discloses_credential(self):
        with peer() as (destination, received):
            with peer(lambda c: (307, {"Location": destination+"/"+TOKEN}, b"")) as (url, _):
                with self.assertRaises(BacktestError) as error:
                    BacktestClient(base_url=url, token=TOKEN).submit(self.config(), idempotency_key="redirect-test")
                self.assertEqual(error.exception.code, "REDIRECT_REFUSED")
                self.assertNotIn(TOKEN, "".join(traceback.format_exception(error.exception)))
            self.assertEqual(received, [])

    def test_server_echo_is_redacted_from_errors_success_and_cli(self):
        for status in (200, 409):
            response = dict(detail=dict(code="IDEMPOTENCY_CONFLICT", message="echo "+TOKEN, scope={TOKEN: TOKEN}), nested=[TOKEN])
            with peer(lambda c: (status, {}, json.dumps(response).encode())) as (url, _):
                client = BacktestClient(base_url=url, token=TOKEN)
                if status == 200:
                    text = json.dumps(client.capabilities())
                else:
                    with self.assertRaises(BacktestError) as error: client.capabilities()
                    self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
                    text = json.dumps(error.exception.diagnostic())+"".join(traceback.format_exception(error.exception))
                self.assertNotIn(TOKEN, text)
                process = subprocess.run([sys.executable, "-I", "-m", "quantfoundry.cli", "backtest", "capabilities"],
                    env=dict(os.environ, QF_BASE_URL=url, QF_API_TOKEN=TOKEN), capture_output=True, text=True, cwd=tempfile.gettempdir())
                self.assertEqual(process.returncode, 0 if status == 200 else 2)
                self.assertNotIn(TOKEN, process.stdout+process.stderr)

    def test_invalid_endpoint_token_or_header_never_sends_request(self):
        for url in ("http://remote.invalid", "https://user:secret@host", "https://host/?token=secret", "https://host/#secret", "https://host/api/admin/backtests", "https://host\nsecret"):
            with self.assertRaises(BacktestError): BacktestClient(base_url=url, token=TOKEN)
        with peer() as (url, calls):
            for token in ("", "secret\r\nInjected: yes", "secret space"):
                with self.assertRaises(BacktestError): BacktestClient(base_url=url, token=token)
            client = BacktestClient(base_url=url, token=TOKEN)
            for key in ("", "a\n", TOKEN, "x"*129):
                with self.assertRaises(BacktestError): client.submit(self.config(), idempotency_key=key)
            with self.assertRaises(BacktestError): client.status("../capabilities")
            with self.assertRaises(BacktestError): client.batch([] , idempotency_key="empty")
            with self.assertRaises(ContractError): client.result(str(uuid4()), limit=True)
            with self.assertRaises(BacktestError): client.batch_status(str(uuid4()), limit=257)
            self.assertEqual(calls, [])

    def test_oversized_duplicate_nonfinite_invalid_and_excess_pages_are_rejected(self):
        for data, code in [(b"x"*(1024*1024+1), "RESOURCE_LIMIT"), (b'{"x":1,"x":2}', "INVALID_RESPONSE"),
                           (b'{"x":1e999}', "INVALID_RESPONSE"), (b'{"x":NaN}', "INVALID_RESPONSE"),
                           (b'[]', "INVALID_RESPONSE"), (b'not json', "INVALID_RESPONSE")]:
            with peer(lambda c: (200, {}, data)) as (url, _):
                with self.assertRaises(BacktestError) as error: BacktestClient(base_url=url, token=TOKEN).capabilities()
                self.assertEqual(error.exception.code, code)
        with peer(lambda c: (200, {}, b'{"items":[{},{}]}')) as (url, _):
            with self.assertRaises(BacktestError): BacktestClient(base_url=url, token=TOKEN).result(str(uuid4()), kind="orders", limit=1)

    def test_cli_help_unknown_arguments_and_complete_config_no_token_output(self):
        command = [sys.executable, "-I", "-m", "quantfoundry.cli", "backtest"]
        process = subprocess.run(command+["--token", TOKEN], capture_output=True, text=True)
        self.assertEqual(process.returncode, 2)
        self.assertNotIn(TOKEN, process.stdout+process.stderr)
        process = subprocess.run(command+["--help"], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0)
        self.assertIn("retry-failed", process.stdout)
        with peer() as (url, calls), tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"config.json"
            config=self.config().to_dict(); config.pop("api_schema")
            path.write_text(json.dumps(config))
            process = subprocess.run(command+["preflight", str(path)], env=dict(os.environ,QF_BASE_URL=url,QF_API_TOKEN=TOKEN),capture_output=True,text=True)
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stderr)["code"], "INVALID_RUN_CONFIG")
            self.assertEqual(calls, [])


if __name__ == "__main__": unittest.main()
