"""Correlated diagnostics; business originals are opt-in and file-local only.

This module never issues a request or changes a collection checkpoint. The
operator can enable originals for two run UUIDs or two frozen task scopes
for 30 minutes. Each frozen scope admits only one run in this process.
Credentials are excluded; business symbols, values and response text are kept.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
import base64
import copy
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import logging
import os
import re
from threading import Lock
import time
from urllib.parse import quote, quote_plus
from uuid import UUID

import structlog


DATASETS = frozenset({'fund_income', 'fund_performance_history'})
BUSINESS_CODES = frozenset({0, 1001, 1002, 1003, 1004, 2001, 2003, 3001, 3002,
                            3004, 4001, 5001, 5002, 5003})
KINDS = frozenset({'unauthenticated', 'forbidden', 'invalid_parameters', 'not_found',
                   'data_not_ready', 'unsupported', 'rate_limited', 'upstream_error',
                   'network_error', 'timeout', 'invalid_response', 'response_too_large',
                   'rejected', 'invalid_data', 'partial_reports'})
LOCAL_FIELDS = frozenset({'business_parameters', 'raw_response'})
FIELDS = frozenset({'event', 'timestamp', 'level', 'run_id', 'dataset',
    'object_seq', 'request_seq', 'started_at', 'finished_at', 'duration_ms',
    'http_status', 'business_code', 'data_type', 'item_empty', 'has_more',
    'complete', 'error_branch', 'checkpoint_advanced', 'source_revision',
    'published_version', 'application_version', 'diagnostic_version',
    'raw_response_state', 'raw_response_bytes', 'raw_response_sha256',
    'raw_response_format', 'credential_redactions', 'body_capture_limit_bytes',
    'business_parameters', 'raw_response'})
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_RUN_BYTES = 16 * 1024 * 1024
MAX_CAPTURE_REQUESTS = 120
_active = ContextVar('tonghuashun_diagnostic_run', default=None)
_scope_claims = {}
_scope_claim_lock = Lock()
_SECRET_KEYS = frozenset({'password', 'passwd', 'pwd', 'token', 'accesstoken',
    'refreshtoken', 'apikey', 'authorization', 'proxyauthorization', 'cookie',
    'setcookie', 'secret', 'clientsecret', 'sessiontoken', 'sessionid', 'xapikey',
    'xauthtoken', 'csrftoken', 'xcsrftoken', 'xsrftoken', 'passwordhash',
    'credentials', 'credential', 'privatekey', 'encryptionkey'})
_CREDENTIAL_TEXT = re.compile(
    r'(?i)(\b(?:password|passwd|pwd|(?:access[_-]?|refresh[_-]?)?token|api[_-]?key|'
    r'authorization|proxy-authorization|cookie|set-cookie|client[_-]?secret|secret)'
    r'\b[\s"\x27]*[:=][\s"\x27]*)([^"\x27<>&,;}\r\n]+)')
_BEARER = re.compile(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/_.=:-]+')
_AUTH_LINE = re.compile(r'(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*[:=])[^\r\n]*')
_URL_USERINFO = re.compile(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/<>"\x27]+@')
_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S)


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def scope_key(dataset, task_id, parameters):
    """Hash the complete validated parameter snapshot, without logging it."""
    task = _uuid(task_id)
    if dataset not in DATASETS or task is None or not isinstance(parameters, dict):
        return None
    try:
        raw = json.dumps([dataset, task, parameters], sort_keys=True,
                         separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(raw.encode()).hexdigest()
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        return None


def _capture_selection(now):
    try:
        raw = os.environ.get('QF_THS_DIAGNOSTIC_RUN_IDS', '')
        scoped = os.environ.get('QF_THS_DIAGNOSTIC_SCOPE_KEYS', '')
        if bool(raw) == bool(scoped):
            return None
        until = datetime.fromisoformat(os.environ.get('QF_THS_DIAGNOSTIC_UNTIL', '').replace('Z', '+00:00'))
        if until.tzinfo is None or not 0 < (until - now).total_seconds() <= 1800:
            return None
        if scoped:
            keys = scoped.split(',')
            if not 1 <= len(keys) <= 2 or any(re.fullmatch('[0-9a-f]{64}', x) is None for x in keys):
                return None
            return 'scope', set(keys)
        ids = raw.split(',') if raw else []
        if not 1 <= len(ids) <= 2 or any(_uuid(x.strip()) is None for x in ids):
            return None
        return 'run', {_uuid(x.strip()) for x in ids}
    except (ValueError, TypeError, OverflowError):
        return None


def _capture_enabled(run_id, now, key=None):
    selection = _capture_selection(now)
    if selection is None:
        return False
    mode, values = selection
    if mode == 'run':
        return key is None and run_id in values
    with _scope_claim_lock:
        return key in values and _scope_claims.get(key) == run_id


def _claim_scope(run_id, key, now):
    selection = _capture_selection(now)
    if selection is None or selection[0] != 'scope' or key not in selection[1]:
        return
    with _scope_claim_lock:
        if key not in _scope_claims and len(_scope_claims) < 2:
            _scope_claims[key] = run_id


def _scope_configured(key):
    raw = os.environ.get('QF_THS_DIAGNOSTIC_SCOPE_KEYS', '')
    keys = raw.split(',') if raw else []
    return (key is not None and 1 <= len(keys) <= 2
            and all(re.fullmatch('[0-9a-f]{64}', x) for x in keys) and key in keys)


def check_scope():
    """A selected diagnostic attempt must not execute after its grant closes."""
    run = _active.get()
    if (run is not None and run.scope_key is not None
            and (not run.capture or not _capture_enabled(run.run_id, datetime.now(UTC), run.scope_key))):
        from app.data_ingestion.tonghuashun.control import CollectionYield
        raise CollectionYield('stopped')


@dataclass
class Run:
    run_id: str
    dataset: str
    capture: bool
    scope_key: str | None = None
    object_seq: int = 0
    request_seq: int = 0
    source_revision: int | None = None
    used_bytes: int = 0
    last: dict = field(default_factory=dict)


def begin_run(dataset, run_id, task_id=None, parameters=None):
    identifier = _uuid(run_id)
    now = datetime.now(UTC)
    key = scope_key(dataset, task_id, parameters)
    if identifier and dataset in DATASETS:
        _claim_scope(identifier, key, now)
    if not _scope_configured(key):
        key = None
    run = (Run(identifier, dataset, _capture_enabled(identifier, now, key), scope_key=key)
           if dataset in DATASETS and identifier else None)
    return _active.set(run)


def end_run(token):
    _active.reset(token)


def begin_object(dataset, revision):
    run = _active.get()
    if run is not None and run.dataset == dataset:
        run.object_seq += 1
        run.source_revision = revision if type(revision) is int and revision >= 0 else None
        run.last = {}


def begin_request():
    run = _active.get()
    if run is None:
        return None
    run.request_seq += 1
    return (run, run.request_seq, time.monotonic(), datetime.now(UTC).isoformat())


def wants_body(attempt):
    return bool(attempt and attempt[0].capture and attempt[1] <= MAX_CAPTURE_REQUESTS
                and attempt[0].used_bytes < MAX_RUN_BYTES
                and _capture_enabled(attempt[0].run_id, datetime.now(UTC), attempt[0].scope_key))


def read_error_body(attempt, response, deadline, limit):
    """Best-effort read of the SAME error response; never retry or change its error."""
    if not wants_body(attempt):
        return None, False
    data = bytearray()
    try:
        for chunk in response.iter_content(1):
            if time.monotonic() >= deadline or len(data) + len(chunk) > min(limit, MAX_BODY_BYTES):
                return data, False
            data.extend(chunk)
        return data, True
    except Exception:
        return data, False


def _credential_key(key):
    return re.sub('[^a-z0-9]', '', key.lower()) in _SECRET_KEYS


def _scrub(value, secrets, depth=0):
    if depth > 64:
        raise ValueError('credential_scrub_depth')
    if isinstance(value, dict):
        result, count = {}, 0
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError('credential_scrub_key')
            safe_key, changed = _scrub(key, secrets, depth + 1)
            if _credential_key(key):
                result[safe_key] = '[credential removed]'
                count += changed + 1
            else:
                result[safe_key], changed_item = _scrub(item, secrets, depth + 1)
                count += changed + changed_item
        return result, count
    if isinstance(value, list):
        result, count = [], 0
        for item in value:
            safe, changed = _scrub(item, secrets, depth + 1)
            result.append(safe); count += changed
        return result, count
    if isinstance(value, str):
        original = value
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[credential removed]')
        value = _AUTH_LINE.sub(r'\1[credentials removed]', value)
        value = _PRIVATE_KEY.sub('[private key removed]', value)
        value = _CREDENTIAL_TEXT.sub(r'\1[credential removed]', value)
        value = _BEARER.sub('[authorization removed]', value)
        value = _URL_USERINFO.sub(r'\1[credentials removed]@', value)
        return value, int(value != original)
    if value is None or type(value) in (bool, int, float) or isinstance(value, Decimal):
        if any(secret and secret in str(value) for secret in secrets):
            return '[credential removed]', 1
        return value, 0
    raise ValueError('credential_scrub_type')


def _encode_original(value):
    # Preserve Decimal business values as JSON numbers if credential removal
    # requires rewriting the envelope. Never turn a financial number to text.
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('non_json_number')
        return str(value)
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k, ensure_ascii=False)+':'+_encode_original(v)
                              for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(_encode_original(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _reject_constant(value):
    raise ValueError('non_json_constant')


def _originals(raw, parameters, api_key):
    secrets = {api_key, quote(api_key, safe=''), quote_plus(api_key),
               base64.b64encode(api_key.encode()).decode()}
    params, removed_params = _scrub(parameters, secrets)
    text = bytes(raw).decode('utf-8')
    try:
        parsed = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except ValueError:
        safe, removed = _scrub(text, secrets)
        return params, safe, 'text', removed + removed_params
    safe, removed = _scrub(parsed, secrets)
    # With no credentials removed retain the exact original UTF-8 JSON text,
    # including whitespace and numeric representation, not a summarized view.
    body = _encode_original(safe) if removed else text
    return params, body, 'json', removed + removed_params


def render_json(logger, method, event):
    if event.get('event') in ('tonghuashun_request_diagnostic', 'tonghuashun_object_diagnostic'):
        # Only the explicitly supplied payload is trusted. Ambient contextvars
        # may contain legacy symbols/headers; even matching key names cannot
        # inject values into a diagnostic event.
        payload = event.get('_ths_diagnostic', {})
        event = {**{key: value for key, value in payload.items() if key in FIELDS},
                 **{key: event[key] for key in ('event', 'timestamp', 'level') if key in event}}
    return structlog.processors.JSONRenderer()(logger, method, event)


class LocalOnlyDiagnostics(logging.Filter):
    """Python3.12 per-handler copy: originals go to app.jsonl, never stderr."""
    def filter(self, record):
        event = record.msg.get('event') if isinstance(record.msg, dict) else getattr(record, 'event', None)
        if event in ('tonghuashun_request_diagnostic', 'tonghuashun_object_diagnostic'):
            safe = copy.copy(record)
            if isinstance(record.msg, dict):
                safe.msg = dict(record.msg)
                safe.msg['_ths_diagnostic'] = {key: value for key, value in
                    record.msg.get('_ths_diagnostic', {}).items() if key not in LOCAL_FIELDS}
            else:
                safe._ths_diagnostic = {key: value for key, value in
                    getattr(record, '_ths_diagnostic', {}).items() if key not in LOCAL_FIELDS}
            return safe
        return True


def _emit(event, fields, warning=False):
    # Never let diagnostics alter collection, publication, retry or cancellation.
    try:
        from app.core.logging import get_logging_runtime
        runtime = get_logging_runtime()
        if runtime is None or runtime._stopped:
            fields = {key: value for key, value in fields.items() if key not in LOCAL_FIELDS}
            fields['raw_response_state'] = 'omitted_local_file_channel_unavailable'
        logger = logging.getLogger(__name__)
        (logger.warning if warning else logger.info)(event, extra={'event': event, '_ths_diagnostic': fields})
    except Exception:
        pass
    return fields


def _base(run):
    try:
        app_version = version('quant-foundry')
    except PackageNotFoundError:
        app_version = 'unknown'
    return dict(run_id=run.run_id, dataset=run.dataset, object_seq=run.object_seq,
                source_revision=run.source_revision, application_version=app_version,
                diagnostic_version=3)


def finish_request(attempt, parameters, api_key, status, raw, complete, body, parsed, failure):
    if attempt is None:
        return
    try:
        run, number, started, stamp = attempt
        # A non-200 response is rejected by status before the transport parser.
        # Parsing its already-received diagnostic copy changes no error/retry.
        if run.capture and not parsed and complete and raw is not None:
            try:
                body = json.loads(bytes(raw), parse_float=Decimal, parse_constant=_reject_constant)
                parsed = True
            except (ValueError, TypeError, RecursionError):
                pass
        data = body.get('data') if parsed and isinstance(body, dict) else None
        item = data.get('item') if isinstance(data, dict) else None
        code = body.get('code') if parsed and isinstance(body, dict) else None
        kind = getattr(failure, 'kind', None)
        kind = kind if kind in KINDS else 'unknown' if failure is not None else 'response_received'
        fields = {**_base(run), 'request_seq': number, 'started_at': stamp,
            'finished_at': datetime.now(UTC).isoformat(),
            'duration_ms': round(max(0, time.monotonic() - started) * 1000, 3),
            'http_status': status if type(status) is int and 100 <= status <= 599 else None,
            'business_code': code if type(code) is int and code in BUSINESS_CODES else None,
            'data_type': 'object' if isinstance(data, dict) else 'array' if isinstance(data, list) else 'null' if parsed and data is None else 'unknown',
            'item_empty': not item if isinstance(item, list) else None,
            'has_more': data.get('has_more') if isinstance(data, dict) and type(data.get('has_more')) is bool else None,
            'complete': data.get('complete') if isinstance(data, dict) and type(data.get('complete')) is bool else None,
            'error_branch': kind, 'checkpoint_advanced': None,
            'raw_response_state': 'disabled', 'body_capture_limit_bytes': MAX_BODY_BYTES}
        if raw is not None:
            fields['raw_response_bytes'] = len(raw)
        if run.capture:
            fields['raw_response_state'] = ('capture_limit_or_expiry' if not wants_body(attempt)
                else 'response_incomplete_or_not_consumed' if not complete or raw is None
                else 'omitted_size_limit' if len(raw) > MAX_BODY_BYTES else 'pending')
            if wants_body(attempt):
                try:
                    secrets = {api_key, quote(api_key, safe=''), quote_plus(api_key),
                               base64.b64encode(api_key.encode()).decode()}
                    params, removed = _scrub(parameters, secrets)
                    fields.update(business_parameters=params, credential_redactions=removed)
                except Exception:
                    fields['raw_response_state'] = 'omitted_credential_filter_or_encoding_error'
            if fields['raw_response_state'] == 'pending':
                fields['raw_response_sha256'] = hashlib.sha256(raw).hexdigest()
                try:
                    params, text, fmt, removed = _originals(raw, parameters, api_key)
                    local = dict(business_parameters=params, raw_response=text,
                                 raw_response_format=fmt, credential_redactions=removed)
                    size = len(json.dumps(local, ensure_ascii=True, default=str).encode())
                    if run.used_bytes + size > MAX_RUN_BYTES:
                        fields['raw_response_state'] = 'omitted_run_size_limit'
                        fields.pop('business_parameters', None)
                    else:
                        fields.update(local); run.used_bytes += size
                        fields['raw_response_state'] = 'captured_credentials_filtered'
                except Exception:
                    fields['raw_response_state'] = 'omitted_credential_filter_or_encoding_error'
            if 'business_parameters' in fields and 'raw_response' not in fields:
                size = len(json.dumps(fields['business_parameters'], ensure_ascii=True, default=str).encode())
                if run.used_bytes + size > MAX_RUN_BYTES:
                    fields.pop('business_parameters')
                    fields['raw_response_state'] = 'omitted_run_size_limit'
                else:
                    run.used_bytes += size
        emitted = _emit('tonghuashun_request_diagnostic', fields, warning=run.capture or failure is not None)
        run.last = {key: value for key, value in (emitted or fields).items() if key not in LOCAL_FIELDS}
    except Exception:
        pass


def record_outcome(dataset, succeeded, kind, message, published_version):
    run = _active.get()
    if run is None or run.dataset != dataset:
        return
    try:
        reason = {'同花顺返回空记录，尚不能确认数据已就绪。': 'empty_record',
                  '历史业绩范围未返回数据，尚不能确认覆盖。': 'empty_history_range'}.get(message)
        fields = {**run.last, **_base(run), 'checkpoint_advanced': bool(succeeded),
                  'published_version': _uuid(published_version),
                  'error_branch': 'published' if succeeded else reason or (kind if kind in KINDS else 'unknown')}
        _emit('tonghuashun_object_diagnostic', fields, warning=not succeeded)
    except Exception:
        pass
