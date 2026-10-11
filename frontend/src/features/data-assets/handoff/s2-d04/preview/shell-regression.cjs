/* Reuse the exact D02 browser scenarios without editing D02-owned evidence.
 * D04 intentionally relocates status out of the overview metric band and makes
 * empty/unchecked explanation text more precise. Adapt those three presentation
 * assertions; every navigation, cancellation, denial and preview check remains. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const original = path.resolve(__dirname, '../../s2-d02/preview/acceptance.cjs');
let source = fs.readFileSync(original, 'utf8');
const changes = [
  ["assert.ok((await p.locator('.qf-assets-metrics strong').allTextContents()).includes('状态待确认'));",
    "assert.equal(await p.locator('.qf-assets-heading-status').innerText(), '状态待确认');"],
  ["await p.getByText('当前数据集已登记，尚无正式记录。', { exact: true }).waitFor();",
    "await p.getByText('当前数据集已登记，当前物理存储为空；不说明来源或市场上没有这种数据。', { exact: true }).waitFor();"],
  ["await p.getByText('本地来源尚未检查，不能将未检查视为空。', { exact: true }).waitFor();",
    "await p.getByText('本地来源尚未检查，不能将尚未检查视为空。', { exact: true }).waitFor();"]
];
for (const [before, after] of changes) {
  assert.equal(source.split(before).length - 1, 1, 'Review the D02 seam if its source changes');
  source = source.replace(before, after);
}
process.env.FRONTEND_URL ||= 'http://127.0.0.1:5185';
process.env.SCREENSHOT_DIR ||= '/tmp/qf-d04-d02-regression';
process.env.CHROMIUM_PATH ||= '/usr/bin/chromium';
const replay = new Module(original, module);
replay.filename = original;
replay.paths = Module._nodeModulePaths(path.dirname(original));
replay._compile(source, original);
