/* Synthetic fixtures for isolated tests only. Never import this module into the app. */
const marker = 'S2_D01_SYNTHETIC_ONLY_NOT_PRODUCTION_EVIDENCE';
function dataset(overrides = {}) {
  return { dataset: 'isolated/source/example', name: '隔离示例 · 当前对象', domain: 'isolated.example',
    source: 'isolated/source', frequency: 'report', representation: 'typed-object-nodes-v2',
    schema_id: 'isolated-schema', rule: 'isolated-rule',
    fields: [{ column: 'object_key', type: 'string', meaning: 'current_identity', logical_type: 'typed_scalar', arithmetic: 'type_only_units_require_domain_contract' }],
    limitations: ['units_not_declared'], business_key: ['representation', 'subject', 'object_key', 'member_key'],
    status: 'available', row_count: 3, generation: 1, updated_at: '2026-10-10T07:00:00.123456789Z', issues: 0,
    last_update: null, partitions: ['isolated-partition'], partitions_truncated: false,
    partition_range: { from: 'isolated-partition', to: 'isolated-partition', precision: 'partition' },
    preview_key: { representation: 'isolated-representation', subject: 'isolated-subject', object_key: 'isolated-key', partition: 'isolated-partition' },
    ...overrides };
}
function catalog(items = [dataset()], overrides = {}) {
  return { items, total: items.length, next_offset: null, phase: 'ready', ...overrides };
}
function preview(overrides = {}) {
  return { dataset: dataset().dataset, status: 'available', request_satisfied: false,
    business_date_coverage_verified: false, partial_requested: false,
    rows: [{ object_key: 'isolated-key', member_key: 'isolated-member', exact_decimal: '12345678901234567890.000000000123456789',
      large_integer: '9223372036854775807', timestamp_ns: '1791615600123456789', missing: null }],
    next_cursor: null, generation: 1, actual_range: { from: 'isolated-key', to: 'isolated-key' },
    selected_partitions: ['isolated-partition'], limitations: ['coverage_not_declared'], ...overrides };
}
const scenarios = {
  normal: dataset(), empty: dataset({ status: 'empty', row_count: 0, preview_key: null }),
  notChecked: dataset({ status: 'not_checked', row_count: null, generation: null, issues: null, preview_key: null }),
  restricted: dataset({ status: 'restricted', issues: 2 }), maintenance: dataset({ status: 'rebuilding', row_count: null, preview_key: null }),
  unknown: dataset({ status: 'future_server_state' }),
  updateFailedWithCurrent: dataset({ last_update: { state: 'failed', complete: false, qualified: false,
    reason: 'SOURCE_RANGES_BLOCKED', source_rows: null, committed_partitions: null, updated_at: '2026-10-10T07:00:00Z' } }),
  incompleteCatalog: catalog([dataset()], { total: 101, next_offset: 1 }),
  dataChanged: { detail: { code: 'DATA_CHANGED', message: 'Untrusted server message must not be displayed' } }
};
module.exports = { marker, dataset, catalog, preview, scenarios };
