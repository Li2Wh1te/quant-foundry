/* D04 contract checks use synthetic descriptors only. They cannot establish
 * production availability, field units, business coverage or read permission. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) {
  require.extensions[extension] = (module, filename) => {
    const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename
    });
    module._compile(compiled.outputText, filename);
  };
}
require.extensions['.css'] = () => {};
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const { DatasetView } = require('../src/features/data-assets/DatasetView.tsx');
const { FieldTable, fieldDescription, filterFields } = require('../src/features/data-assets/FieldTable.tsx');
const field = overrides => ({ column: 'f0_value', type: 'string', meaning: 'Model.value',
  logical_type: 'typed_scalar', arithmetic: 'type_only_units_require_domain_contract', ...overrides });
const dataset = overrides => ({ dataset: 'fixture.daily', name: '隔离示例：日线（来源报告值／未锚定）',
  domain: 'fixture', source: 'fixture-source', frequency: 'daily', representation: 'typed-object-nodes-v2',
  schema_id: 'fixture-schema', rule: 'fixture-rule', fields: [field()], limitations: ['来源报告值，单位未声明。'],
  business_key: ['representation', 'subject', 'object_key'], status: 'available', row_count: 1234567,
  generation: 7, updated_at: '2026-10-10T08:00:00.123456789Z', issues: 0, last_update: null,
  partitions: ['2026-01.b01'], partitions_truncated: true,
  partition_range: { from: '2026-01.b01', to: null, precision: 'partition' },
  preview_key: { representation: 'fixture-source-key', subject: 'fixture-object', object_key: '2026-01-02', partition: '2026-01.b01' },
  ...overrides });
const render = value => renderToStaticMarkup(createElement(DatasetView, { dataset: value }));

test('overview preserves descriptor qualifiers and separates physical count, record time and business cutoff', () => {
  const html = render(dataset());
  assert.match(html, /日线（来源报告值／未锚定）/);
  assert.match(html, /用途<\/dt><dd>未声明/);
  assert.match(html, /物理存储行<\/span><strong[^>]*>1,234,567/);
  assert.match(html, /目录\/记录更新时间<\/dt><dd>2026-10-10T08:00:00\.123456789Z/);
  assert.match(html, /业务数据截至日<\/span><strong[^>]*>未声明/);
  assert.match(html, /最近更新结果<\/span><strong[^>]*>暂无更新记录/);
  assert.doesNotMatch(html, /当前记录|最近提交|已验证完整覆盖|\d+%/);
});

test('storage and diagnostic identity remain within a closed technical disclosure', () => {
  const html = render(dataset());
  const start = html.indexOf('class="qf-assets-sheet qf-dataset-technical"');
  const visible = html.slice(0, start), technical = html.slice(start);
  for (const value of ['fixture-schema', 'fixture-rule', 'typed-object-nodes-v2', '2026-01.b01', 'fixture-source-key']) {
    assert.ok(!visible.includes(value), `${value} must not enter the default overview`);
    assert.ok(technical.includes(value));
  }
  assert.match(technical, /<details><summary>展开定位与存储信息/);
  assert.doesNotMatch(technical, /<details open/);
  assert.match(technical, /终点：<code>未声明/);
  assert.match(technical, /不证明业务日期连续覆盖/);
  assert.match(technical, /分区列表已截断/);
  assert.match(technical, /aria-label="复制数据集标识"/);
});

test('raw limitation identifiers stay technical while missing business interpretation is explicit', () => {
  const html = render(dataset({ limitations: ['source_local_identity_only', 'units_and_formulas_unverified', '来源报告值，单位未声明。'] }));
  const technical = html.indexOf('class="qf-assets-sheet qf-dataset-technical"');
  assert.match(html.slice(0, technical), /另有 2 项服务端限制仅提供技术标识，业务解释未提供/);
  assert.ok(!html.slice(0, technical).includes('source_local_identity_only'));
  assert.match(html.slice(0, technical), /来源报告值，单位未声明/);
  assert.match(html.slice(technical), /source_local_identity_only/);
  assert.match(html.slice(technical), /units_and_formulas_unverified/);
});

test('daily, NAV and complete-report descriptors share a framework without a unified business contract', () => {
  for (const [name, frequency, restriction] of [
    ['隔离示例：日线（来源报告值）', 'daily', '价格基准未声明'],
    ['隔离示例：净值（来源报告值／未锚定）', 'daily', '净值单位未声明'],
    ['隔离示例：完整报告（来源报告值）', 'report', '单页节点不代表完整报告']
  ]) {
    const html = render(dataset({ name, frequency, limitations: [restriction] }));
    assert.ok(html.includes(name)); assert.ok(html.includes(restriction));
    assert.match(html, /服务端判断实际对象与范围的读取权限/);
    assert.doesNotMatch(html, /自动复权|跨源融合|可直接算|全范围验证通过/);
  }
});

test('every current state stays distinct and a failed update does not rewrite current availability', () => {
  for (const [status, expected] of [
    ['not_checked', '不能将尚未检查视为空'], ['empty', '当前物理存储为空'],
    ['restricted', '也不表示所有范围均不可读取'], ['rebuilding', '暂不可读取／处理中'],
    ['rebuild_required', '页面不会执行重建'], ['future_state', '当前状态尚未确认']
  ]) assert.ok(render(dataset({ status, row_count: null })).includes(expected));
  const html = render(dataset({ last_update: { state: 'failed', complete: false, qualified: false,
    reason: null, source_rows: null, committed_partitions: null, updated_at: null } }));
  assert.match(html, /当前可用（按服务端声明）/); assert.match(html, /本次更新失败/);
});

test('identifier paths and unknown f0_ names never become business meaning or invented units', () => {
  for (const meaning of ['', '  ', 'Model.field', 'current_identity_or_confirmation', 'provider_unknown']) {
    const value = fieldDescription(field({ meaning }));
    assert.equal(value.meaning, '未提供业务说明');
    assert.equal(value.calculation, '计算口径需领域契约确认');
  }
  const value = fieldDescription(field({ meaning: '来源报告值；单位未声明' }));
  assert.equal(value.meaning, '来源报告值；单位未声明');
  const html = renderToStaticMarkup(createElement(FieldTable, { fields: [field({ column: 'f0_percent_usd_close' })] }));
  assert.match(html, /f0_percent_usd_close/); assert.match(html, /未提供业务说明/);
  assert.doesNotMatch(html, /美元|百分比|人民币|可直接计算|可直接算/);
});

test('decimal text and unknown arithmetic never grant calculation ability', () => {
  for (const overrides of [{ logical_type: 'exact_decimal_text', arithmetic: 'future_supported' },
    { type: 'decimal_text', arithmetic: '' }, { arithmetic: 'unsupported' }]) {
    assert.equal(fieldDescription(field(overrides)).calculation, '不支持直接计算');
  }
  assert.equal(fieldDescription(field({ arithmetic: 'future_supported' })).calculation, '计算能力未声明');
  const html = renderToStaticMarkup(createElement(FieldTable, { fields: [field({ logical_type: 'exact_decimal_text' })] }));
  assert.match(html, /精确十进制文本；保留文本精度/);
});

test('metadata searches use only supplied field attributes and retain exact source text', () => {
  const fields = [field(), field({ column: 'f1_nav', meaning: '已声明净值字段', type: 'decimal128(38, 18)' }),
    field({ column: 'f2_text', meaning: 'Model.text', logical_type: 'exact_decimal_text', arithmetic: 'unsupported' })];
  assert.deepEqual(filterFields(fields, '  MODEL.VALUE  '), [fields[0]]);
  assert.deepEqual(filterFields(fields, '净值'), [fields[1]]);
  assert.deepEqual(filterFields(fields, 'DECIMAL128'), [fields[1]]);
  assert.deepEqual(filterFields(fields, 'exact_decimal_text'), [fields[2]]);
  assert.deepEqual(filterFields(fields, 'missing'), []);
  assert.equal(filterFields(fields, '  '), fields);
  assert.equal(fields[0].meaning, 'Model.value');
});

test('many declared fields are bounded locally and absent field metadata does not hide overview', () => {
  const fields = Array.from({ length: 47 }, (_, i) => field({ column: `f${i}_value` }));
  const html = renderToStaticMarkup(createElement(FieldTable, { fields }));
  assert.equal((html.match(/<th scope="row">/g) ?? []).length, 20);
  assert.match(html, /字段 1–20 \/ 47/); assert.match(html, /下一页字段/);
  assert.match(html, /role="region" aria-label="数据集字段表格"/);
  const empty = render(dataset({ fields: [], limitations: [] }));
  assert.match(empty, /不能据此判断数据集为空/); assert.match(empty, /能提供什么/);
  assert.match(empty, /服务端未提供具体限制说明；这不代表没有限制/);
});
