"""Build only diagnostic hooks on a hash-verified R01 image, never whole main.

Source comes from the pinned installed image or an already retained local test
root. Code/hooks come from this merged repository checkout. No source upload,
base history publication, dependency install, migration or live-file edit.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

BASE_HASHES = {'app/data_ingestion/clients/tonghuashun.py': '67fa9d4b74715ab45e6dece7d0ee18a4ea5bd7c73e5e0560bd1b411e0a96c557', 'app/data_ingestion/tonghuashun/control.py': 'd43fe3d870c4e99291d5e9b5051424da19ccee089438261583a220df1d5fe9a6', 'app/data_ingestion/scheduler_tasks/tonghuashun.py': 'cc8371db18586ed00bb9356ff05c10b26ae49b4fed0e26bb37777890e403ee94', 'app/data_ingestion/tonghuashun/service.py': '40fdaf534a1a327c651012d2500e1516cbd42b34cc2c0987bd7965f0ae4d8bce', 'app/core/logging.py': 'c66e7bb13fcbb94ae8af924cd8228b5185b047ee5d2c758bc76aeb127adc7761'}
PATCHES = {
 'app/data_ingestion/clients/tonghuashun.py': [
  ('import re\n', 'import re\nimport sys\n'),
  ('from app.data_sources.errors import SourceError\n', 'from app.data_sources.errors import SourceError\nfrom app.data_ingestion.tonghuashun import diagnostics\n'),
  ('    def _get(self, path: str, params: dict, deadline: float) -> TonghuashunResponse:\n',
   '    def _get(self, path: str, params: dict, deadline: float) -> TonghuashunResponse:\n        diagnostic_attempt = diagnostics.begin_request()\n        diagnostic_status, raw, body = None, None, None\n        diagnostic_complete, diagnostic_parsed = False, False\n'),
  ('                retry_after = self._retry_after(response.headers.get("Retry-After"))\n',
   '                diagnostic_status = response.status_code\n                retry_after = self._retry_after(response.headers.get("Retry-After"))\n'),
  ('                    raise TonghuashunError(kind, retry_after=retry_after)\n',
   '                    raw, diagnostic_complete = diagnostics.read_error_body(\n                        diagnostic_attempt, response, deadline, self.max_response_bytes)\n                    raise TonghuashunError(kind, retry_after=retry_after)\n'),
  ('                body = json.loads(raw, parse_float=Decimal, parse_constant=_reject_json_constant)\n',
   '                diagnostic_complete = True\n                body = json.loads(raw, parse_float=Decimal, parse_constant=_reject_json_constant)\n                diagnostic_parsed = True\n'),
  ('        except (ValueError, TypeError, RecursionError):\n            raise TonghuashunError("invalid_response") from None\n',
   '        except (ValueError, TypeError, RecursionError):\n            raise TonghuashunError("invalid_response") from None\n        finally:\n            diagnostics.finish_request(diagnostic_attempt, params, self._api_key,\n                diagnostic_status, raw, diagnostic_complete, body, diagnostic_parsed, sys.exc_info()[1])\n'),
 ],
 'app/data_ingestion/tonghuashun/control.py': [
  ('    def begin(self, dataset, subject, variant, revision, parameters, *, cache=False):\n',
   '    def begin(self, dataset, subject, variant, revision, parameters, *, cache=False):\n        from app.data_ingestion.tonghuashun.diagnostics import begin_object\n        begin_object(dataset, revision)\n'),
 ],
 'app/data_ingestion/scheduler_tasks/tonghuashun.py': [
  ('    token = active_control.set(monitor)\n',
   '    token = active_control.set(monitor)\n    from app.data_ingestion.tonghuashun.diagnostics import begin_run, end_run\n    diagnostic_token = None\n'),
  ('    try:\n        result = collect(dataset, CollectionParameters.model_validate(parameters.model_dump()),\n',
   '    try:\n        diagnostic_token = begin_run(dataset, context.run_id, getattr(context, "task_id", None),\n                                     parameters.model_dump(mode="json"))\n        result = collect(dataset, CollectionParameters.model_validate(parameters.model_dump()),\n'),
  ('        active_control.reset(token)\n',
   '        if diagnostic_token is not None:\n            end_run(diagnostic_token)\n        active_control.reset(token)\n'),
 ],
 'app/data_ingestion/tonghuashun/service.py': [
  ('    start = (data or {}).get("requested_start") or (data or {}).get("observed_start") or (parameters.start_date.isoformat() if parameters.start_date else None)\n',
   '    from app.data_ingestion.tonghuashun.diagnostics import record_outcome\n    record_outcome(spec.key, succeeded, kind, error_message, result.get("version_id"))\n    start = (data or {}).get("requested_start") or (data or {}).get("observed_start") or (parameters.start_date.isoformat() if parameters.start_date else None)\n'),
 ],
 'app/core/logging.py': [
  ('from app.core.config import Settings\n',
   'from app.core.config import Settings\nfrom app.data_ingestion.tonghuashun.diagnostics import LocalOnlyDiagnostics, render_json\n'),
  ('            processor=structlog.processors.JSONRenderer(),\n',
   '            processor=render_json,\n'),
  ('        console_handler.setFormatter(formatter)\n',
   '        console_handler.setFormatter(formatter)\n        console_handler.addFilter(LocalOnlyDiagnostics())\n'),
 ],
}


def apply_hooks(name, text):
    for before, after in PATCHES[name]:
        if text.count(before) != 1:
            raise ValueError('R01 diagnostic hook anchor mismatch: ' + name)
        text = text.replace(before, after)
    ast.parse(text)
    return text


def prepare(source, output, base_image):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', base_image):
        raise ValueError('An exact local base image SHA256 is required')
    if output.exists():
        raise ValueError('Refusing to overwrite an existing build directory')
    # Verify ALL inputs before creating any output.
    original = {}
    for name, digest in BASE_HASHES.items():
        data = (source / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('R01 base source hash mismatch: ' + name)
        original[name] = apply_hooks(name, data.decode())
    module = Path(__file__).resolve().parents[1] / 'backend/app/data_ingestion/tonghuashun/diagnostics.py'
    original['app/data_ingestion/tonghuashun/diagnostics.py'] = module.read_text()
    output.mkdir(parents=True)
    hashes = {}
    for name, text in original.items():
        ast.parse(text)
        file = output / name; file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)
        hashes[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    (output / 'Dockerfile').write_text('FROM ' + base_image + '\n' + ''.join(
        'COPY ' + name + ' /app/' + name + '\n' for name in original))
    receipt = dict(base_image=base_image, expected_base_source_hashes=BASE_HASHES,
                   diagnostic_overlay_hashes=hashes, dependencies_or_migrations_changed=False,
                   whole_main_copied=False, body_capture_default=False)
    (output / 'overlay-manifest.json').write_text(json.dumps(receipt, indent=2)+'\n')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-image', required=True)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--build-tag')
    args = parser.parse_args()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', args.base_image):
        parser.error('exact base image required')
    if args.source_root:
        receipt = prepare(args.source_root, args.output, args.base_image)
    else:
        # A never-started container exposes only the five required base files.
        subprocess.run(['docker', 'image', 'inspect', args.base_image], check=True, stdout=subprocess.DEVNULL)
        container = subprocess.check_output(['docker', 'create', '--network', 'none', args.base_image], text=True).strip()
        try:
            with tempfile.TemporaryDirectory(prefix='r01-diagnostic-base-') as temp:
                source = Path(temp)
                for name in BASE_HASHES:
                    target = source / name; target.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(['docker', 'cp', container + ':/app/' + name, str(target)], check=True)
                receipt = prepare(source, args.output, args.base_image)
        finally:
            subprocess.run(['docker', 'rm', container], check=True, stdout=subprocess.DEVNULL)
    if args.build_tag:
        if not re.fullmatch(r'[a-z0-9][a-z0-9/_.:-]{0,150}', args.build_tag):
            parser.error('invalid build tag')
        subprocess.run(['docker', 'build', '--network=none', '--pull=false', '-t', args.build_tag, str(args.output)], check=True)
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
