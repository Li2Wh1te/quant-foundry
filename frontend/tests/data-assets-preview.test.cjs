/* Synthetic contract tests exercise D05 decisions without claiming real read permission. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => {
  const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
  });
  module._compile(compiled.outputText, filename);
};
require.extensions['.css'] = () => {};
global.__QF_VERSION__ = '0.3.0';
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { MemoryRouter } = require('react-router-dom');
const { AuthProvider } = require('../src/auth/AuthContext.tsx');
const { PreviewPanel, defaultPreviewColumns, previewColumnOptions, previewScopeKey,
  previewCellValue, previewEmptyMessage, retainPreviewOnFailure } = require('../src/features/data-assets/PreviewPanel.tsx');
const { buildPreviewRequest, DataStoreApiError } = require('../src/features/data-assets/data/index.ts');
const field = column => ({ column, type: 'string', meaning: 'Model.value', logical_type: 'typed_scalar', arithmetic: 'unsupported' });
const dataset = overrides => ({ dataset: 'fixture.daily', name: '隔离示例', domain: 'fixture', source: 'loopback',
  frequency: 'daily', representation: 'typed-object-nodes-v2', schema_id: 'fixture-schema', rule: 'fixture-rule',
  fields: ['representation', 'subject', 'object_key', 'member_key', 'basis_state', 'basis_token', 'row_kind', 'quality_json',
    ...Array.from({ length: 40 }, (_, index) => `f${index}_value`)].map(field),
  business_key: ['representation', 'subject', 'object_key', 'member_key'], limitations: [], status: 'available',
  row_count: 2, generation: 7, issues: 0, updated_at: null, last_update: null, partitions: [], partitions_truncated: false,
  partition_range: { from: null, to: null, precision: 'partition' },
  preview_key: { representation: 'fixture-source-representation', subject: 'fixture-subject', object_key: 'fixture-period:2026', partition: 'fixture-partition' },
  ...overrides });
const render = value => renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: ['/?view=preview'] },
  createElement(AuthProvider, null, createElement(PreviewPanel, { dataset: value }))));

test('defaults bound actual declared columns, retain node identity, and exclude internal confirmation columns', () => {
  const columns = defaultPreviewColumns(dataset());
  assert.deepEqual(columns, ['object_key', 'member_key', ...Array.from({ length: 6 }, (_, i) => `f${i}_value`)]);
  assert.ok(columns.every(column => previewColumnOptions(dataset()).includes(column)));
  assert.deepEqual(defaultPreviewColumns(dataset({ fields: [], business_key: [] })), []);
  assert.deepEqual(defaultPreviewColumns(dataset({ fields: [field('custom_declared_value')], business_key: [] })), ['custom_declared_value']);
});

test('example query uses the real representation and qualified bounded shared request', () => {
  const value = dataset(), columns = defaultPreviewColumns(value);
  const request = buildPreviewRequest(value, { columns, pageSize: 20 });
  assert.equal(request.representation, value.preview_key.representation);
  assert.notEqual(request.representation, value.representation);
  assert.equal(request.allow_partial, false); assert.equal(request.cursor, null);
  assert.equal(request.from_key, 'fixture-period:2026'); assert.equal(request.to_key, request.from_key);
  assert.equal(request.page_size, 20); assert.deepEqual(request.columns, columns);
});

test('schema, keys, representation, subject, declared columns and frequency belong to the condition scope', () => {
  const original = dataset(), key = previewScopeKey(original);
  for (const changed of [{ dataset: 'fixture.other' }, { schema_id: 'next-schema' }, { rule: 'next-rule' }, { frequency: 'report' },
    { fields: original.fields.slice(1) }, { business_key: ['object_key'] },
    { preview_key: { ...original.preview_key, subject: 'next-subject' } },
    { preview_key: { ...original.preview_key, representation: 'next-source' } },
    { preview_key: { ...original.preview_key, object_key: 'next-object' } }]) {
    assert.notEqual(previewScopeKey(dataset(changed)), key);
  }
  assert.equal(previewScopeKey(dataset({ updated_at: 'new-observation-time' })), key);
});

test('decimal, huge integer, ns text, null, undefined, false and zero keep their exact display', () => {
  for (const value of ['12345678901234567890.123456789012345678', '900719925474099312345', '1791619200123456789',
    '2026-10-10T08:00:00.123456789Z', '0.000000000000000001', false, 0]) assert.equal(previewCellValue(value), String(value));
  assert.equal(previewCellValue(null), '—'); assert.equal(previewCellValue(undefined), '—');
});

test('empty page, query miss and descriptor empty remain different facts', () => {
  assert.match(previewEmptyMessage(dataset(), { next_cursor: 'synthetic-next' }), /本页为空.*后续页/);
  assert.match(previewEmptyMessage(dataset(), { next_cursor: null }), /查询范围无匹配记录.*不能.*整个数据集为空/);
  assert.match(previewEmptyMessage(dataset({ status: 'empty' }), { next_cursor: null }), /数据集当前为空.*服务端声明/);
});

test('only connection failures and timeouts retain an explicitly stale page', () => {
  for (const code of ['NETWORK_ERROR', 'QUERY_TIMEOUT']) assert.equal(retainPreviewOnFailure(new DataStoreApiError(0, code, 'fixture')), true);
  for (const code of ['DATA_CHANGED', 'REBUILD_REQUIRED', 'DATA_RESTRICTED', 'DATA_STORE_REBUILDING', 'FILE_INVALID',
    'INVALID_RESPONSE', 'UNKNOWN_QUALITY_FAILURE', 'QUERY_BUDGET_EXCEEDED']) assert.equal(retainPreviewOnFailure(new DataStoreApiError(409, code, 'fixture')), false);
  assert.equal(retainPreviewOnFailure(new Error('raw upstream text')), false);
  for (const status of [401, 403]) assert.equal(retainPreviewOnFailure(new DataStoreApiError(status, 'NETWORK_ERROR', 'fixture')), false);
});

test('missing preview key never creates manual source identity controls or an enabled read action', () => {
  const html = render(dataset({ preview_key: null }));
  assert.match(html, /暂无可预填对象/); assert.match(html, /type="submit" disabled/);
  assert.doesNotMatch(html, /允许部分结果|type="date"|value="typed-object-nodes-v2"/);
  assert.doesNotMatch(html, /<label[^>]*>.*口径标识<input/);
});

test('ordinary UI keeps exact text business keys, bounded page choices and collapsed technical details', () => {
  const html = render(dataset());
  assert.match(html, /当前对象示例/); assert.match(html, /对象键（文本）/);
  assert.match(html, /起始业务键（文本）/); assert.match(html, /业务键的日期语义未声明/);
  assert.match(html, /value="100">100 行/); assert.match(html, /选择预览列（8 \/ 32）/);
  assert.match(html, /<details class="qf-preview-technical"><summary>查询技术信息/);
  assert.match(html, /来源表示：<code>fixture-source-representation/);
  assert.match(html, /allow_partial=false/); assert.doesNotMatch(html, /<details[^>]*open|type="date"|历史版本|自动复权/);
});

test('descriptor restriction and unchecked states remain honest without granting or blocking request permission', () => {
  assert.match(render(dataset({ status: 'restricted' })), /当前对象是否可读以本次服务端判断为准/);
  assert.match(render(dataset({ status: 'not_checked' })), /尚未检查，不能将它视为空/);
  assert.match(render(dataset({ status: 'empty', preview_key: null })), /数据集当前为空/);
});
