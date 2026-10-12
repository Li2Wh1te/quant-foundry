/* Public synthetic proof metadata only; no source originals or real IDs. */
module.exports = () => ({
  method: 'complete-prior-failed-refresh-read@1', whole_report_rows: 3, whole_report_verified: true,
  current_confirmation: { native_id: '00000000-0000-4000-8000-000000000001',
    observed_at: '2026-09-10T08:00:00.123456+00:00', content_hash: 'a'.repeat(64) },
  failed_refresh: { reason: 'SOURCE_REFRESH_FAILED', blocking: false,
    native_id: '00000000-0000-4000-8000-000000000002',
    observed_at: '2026-10-10T08:00:00.654321+00:00', content_hash: 'b'.repeat(64) },
  domain_read_restriction: 'INPUT_COVERAGE_UNPROVEN',
  financial_quality_limitations: { field: ['SOURCE_VALUE_INVALID', 'unit unconfirmed'] },
  latest_source_confirmation: false, full_domain_accepted: false
});
