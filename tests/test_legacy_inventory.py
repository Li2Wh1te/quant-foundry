"""Read-only inventory regressions, including the supplied D17 blind spots."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

loader = importlib.util.spec_from_file_location('inventory', Path(__file__).resolve().parents[1]/'scripts/scan_legacy_dependencies.py')
inventory = importlib.util.module_from_spec(loader)
loader.loader.exec_module(inventory)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'backend/app/backtesting/data').mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, body):
        p = self.root/name
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(body)
        return p

    def test_external_relative_and_constant_dynamic_imports(self):
        self.write('backend/app/main.py','from app.backtesting.runtime import Engine\nimportlib.import_module("app.backtesting.execution")\n')
        self.write('backend/app/backtesting/data/adapter.py','from ..runtime import Engine\n')
        r = inventory.scan(self.root)
        self.assertTrue(r['complete'])
        self.assertEqual(r['scanned_files'],2)
        self.assertEqual({x['symbol'] for x in r['references'] if x['kind']=='python_import'}, {'app.backtesting.runtime'})
        self.assertTrue(any(x['kind']=='python_module_string' for x in r['references']))
        self.assertFalse(r['deletion_authorized'])

    def test_entrypoints_container_and_frontend_writes_are_reviewed(self):
        self.write('pyproject.toml','[project.scripts]\nworker="app.backtesting.runner_worker:main"\n')
        self.write('Dockerfile','CMD ["python", "-m", "app.strategy_protocol.worker"]\n')
        self.write('frontend/src/client.ts','fetch("/api/admin/backtest-runs", {method: "POST"})\n')
        kinds = {r['kind'] for r in inventory.scan(self.root)['references']}
        self.assertTrue({'entrypoint_or_container_review','legacy_module_reference','frontend_write_review','legacy_write_or_result_route_review'}<=kinds)

    def test_models_are_not_delete_authorization(self):
        self.write('backend/app/backtesting/models.py','class Account: pass\n')
        c=inventory.scan(self.root)['candidates'][0]
        self.assertEqual(c['category'],'split_preserve_business_data')
        self.assertIsNone(c['safe_to_delete'])

    def test_bad_syntax_and_unreadable_budget_are_not_complete(self):
        self.write('backend/app/broken.py','def :\n')
        self.assertFalse(inventory.scan(self.root)['complete'])
        self.assertFalse(inventory.scan(self.root,max_bytes=1)['complete'])

    def test_symlink_is_not_followed(self):
        p=self.write('backend/app/reference.py','pass\n')
        (self.root/'backend/app/link.py').symlink_to(p)
        self.assertFalse(inventory.scan(self.root)['complete'])

    def test_env_generated_are_not_scanned_and_source_not_mutated(self):
        self.write('backend/.env','DONT_READ=app.backtesting.runtime\n')
        self.write('node_modules/test.js','app.backtesting.runtime')
        self.assertEqual(inventory.scan(self.root)['scanned_files'],0)
        p=self.write('backend/app/main.py','import app.backtesting.runtime\n')
        before=p.read_bytes()
        inventory.scan(self.root)
        self.assertEqual(before,p.read_bytes())

    def test_output_is_exclusive(self):
        out=self.root/'report.json'
        out.write_text('do-not-overwrite')
        self.assertEqual(inventory.main(['--repo',str(self.root),'--output',str(out)]),2)
        self.assertEqual(out.read_text(),'do-not-overwrite')
