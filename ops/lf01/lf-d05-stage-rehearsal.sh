#!/usr/bin/env bash
# Rehearse the LF-D05 command chain inside a disposable, named test database.
# This script is intentionally absent from the production backend image. Mount
# it only into an isolated container with a dedicated PostgreSQL and ext4 volume.
set -euo pipefail

if [[ "${QF_LFD05_ISOLATED:-}" != "1" || "${QF_ENVIRONMENT:-}" != "test" ||
      "${QF_DATABASE_HOST:-}" != "postgres" || "${QF_DATABASE_NAME:-}" != lfd05_stage*_test ||
      -z "${QF_AUDIT_API_TOKEN:-}" ]]; then
  echo 'LF-D05 rehearsal requires the exact isolated test database and API token.' >&2
  exit 2
fi

WORK_ROOT=/work/lfd05-current
EVIDENCE_ROOT=/evidence/lf-d05-stage
if [[ -e "$WORK_ROOT" || -e "$EVIDENCE_ROOT" ]]; then
  echo 'LF-D05 rehearsal requires new current-store and evidence directories.' >&2
  exit 2
fi
mkdir -m 0700 "$WORK_ROOT" "$EVIDENCE_ROOT"

# Ordinary installation is additive. The source fixture is a single local
# observation, inserted after migration and before the explicit legacy reset.
alembic upgrade head >"$EVIDENCE_ROOT/migration.log" 2>&1
python - <<'PY'
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import text
from app.db.session import get_engine
from app.data_store.adapters.contracts import digest, native_json

content = {'item': [{'company_id': 'LF-D05-ONLY',
                     'company_name': 'isolated local source', 'fund_count': 1}]}
with get_engine().begin() as connection:
    connection.execute(text('''INSERT INTO tonghuashun_observations
        (id,dataset,subject,variant,observed_at,request_json,data_json,
         content_hash,row_count,chain_depth)
        VALUES (:id,'fund_company','LF-D05-ONLY','default',:observed_at,
                '[]',:data_json,:content_hash,1,0)'''),
        {'id': uuid4(), 'observed_at': datetime(2026, 1, 2, tzinfo=timezone.utc),
         'data_json': native_json(content), 'content_hash': digest(content)})
PY

python -m app.legacy_reset status --expect-database "$QF_DATABASE_NAME" >"$EVIDENCE_ROOT/status-before.json"
python -m app.legacy_reset enter --expect-database "$QF_DATABASE_NAME" >"$EVIDENCE_ROOT/enter.json"
python -m app.legacy_reset plan --expect-database "$QF_DATABASE_NAME" \
  --out "$EVIDENCE_ROOT/plan.json" >"$EVIDENCE_ROOT/plan-result.json"
python -m app.legacy_reset export --expect-database "$QF_DATABASE_NAME" \
  --plan "$EVIDENCE_ROOT/plan.json" --rescue "$EVIDENCE_ROOT/rescue.jsonl.gz" \
  --manifest "$EVIDENCE_ROOT/rescue-manifest.json" >"$EVIDENCE_ROOT/export.json"
python -m app.legacy_reset verify --expect-database "$QF_DATABASE_NAME" \
  --plan "$EVIDENCE_ROOT/plan.json" --rescue "$EVIDENCE_ROOT/rescue.jsonl.gz" \
  --manifest "$EVIDENCE_ROOT/rescue-manifest.json" >"$EVIDENCE_ROOT/verify.json"
PLAN_SHA256=$(python - "$EVIDENCE_ROOT/plan.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8'))['digest'])
PY
)
python -m app.legacy_reset apply --expect-database "$QF_DATABASE_NAME" \
  --plan "$EVIDENCE_ROOT/plan.json" --sha256 "$PLAN_SHA256" \
  --rescue "$EVIDENCE_ROOT/rescue.jsonl.gz" \
  --manifest "$EVIDENCE_ROOT/rescue-manifest.json" >"$EVIDENCE_ROOT/apply.json"
python -m app.legacy_reset status --expect-database "$QF_DATABASE_NAME" >"$EVIDENCE_ROOT/status-after.json"

python -m app.data_store rebuild --root "$WORK_ROOT" --initialize \
  --output "$EVIDENCE_ROOT/rebuild.json" >/dev/null
python -m app.data_store update --root "$WORK_ROOT" \
  --output "$EVIDENCE_ROOT/update.json" >/dev/null
python -m app.data_store retry --root "$WORK_ROOT" \
  --output "$EVIDENCE_ROOT/retry.json" >/dev/null
python -m app.data_store cleanup --root "$WORK_ROOT" \
  --output "$EVIDENCE_ROOT/cleanup.json" >/dev/null
python -m app.legacy_reset finish --expect-database "$QF_DATABASE_NAME" >"$EVIDENCE_ROOT/finish.json"
python -m app.legacy_reset restore --expect-database "$QF_DATABASE_NAME" >"$EVIDENCE_ROOT/restore.json"

# The actual HTTP route is exercised by audit-export. The bearer token stays
# in an environment variable and is never copied into the evidence directory.
python -m app >"$EVIDENCE_ROOT/api.log" 2>&1 &
API_PID=$!
trap 'kill "$API_PID" 2>/dev/null || true; wait "$API_PID" 2>/dev/null || true' EXIT
API_READY=0
for attempt in {1..30}; do
  if python - <<'PY'
import urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2)
except OSError:
    raise SystemExit(1)
PY
  then API_READY=1; break; fi
  sleep 1
done
if [[ "$API_READY" != "1" ]]; then
  echo 'Isolated API did not become ready.' >&2
  exit 2
fi
python -m app.data_store audit-export --root "$WORK_ROOT" \
  --output "$EVIDENCE_ROOT/audit" --api-base-url http://127.0.0.1:8000 \
  --api-token-env QF_AUDIT_API_TOKEN >"$EVIDENCE_ROOT/audit-result.json"

python - "$EVIDENCE_ROOT" <<'PY'
import json, pathlib, sys
evidence = pathlib.Path(sys.argv[1])
for name in ('rebuild', 'update', 'retry', 'cleanup'):
    result = json.loads((evidence / f'{name}.json').read_text())
    assert result['complete'], (name, result.get('reason'))
audit = json.loads((evidence / 'audit-result.json').read_text())
assert audit['complete'], audit
print(json.dumps({'stage_rehearsal': 'passed', 'api_audit': 'complete',
                  'current_files': audit['checked_files'],
                  'current_rows': audit['checked_rows']}))
PY
