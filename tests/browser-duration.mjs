// Read-only regression against the running local service. Every API mutation is
// blocked before sending; CSV exports only download already saved records.
import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import {mkdir, readFile, writeFile} from 'node:fs/promises';

const base = process.env.BOND_TEST_URL || 'http://127.0.0.1:8765';
const target = process.env.BOND_TEST_DATE || '2026-09-11';
const origin = new URL(base).origin;
assert(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(base).hostname), 'Use a local service only');
const output = 'test-results';
const checks = [], errors = [], writes = [], requests = [], exports = [], screenshots = [];
const report = {checkedAt: new Date().toISOString(), base, target, checks, errors, writes, requests, exports, screenshots};
await mkdir(output, {recursive: true});
let browser, page;

async function get(path) {
  requests.push({method: 'GET', path, via: 'preflight'});
  const response = await fetch(base + path);
  assert(response.ok, `${path}: HTTP ${response.status}`);
  return response.json();
}

// Parse quoted CSV, including escaped quotes, commas and multiline fields.
function parseCsv(text) {
  const rows = [], row = [];
  let field = '', quoted = false;
  text = text.replace(/^\uFEFF/, '');
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '"') {
      if (quoted && text[i + 1] === '"') { field += '"'; i++; }
      else quoted = !quoted;
    } else if (!quoted && char === ',') {
      row.push(field); field = '';
    } else if (!quoted && (char === '\r' || char === '\n')) {
      if (char === '\r' && text[i + 1] === '\n') i++;
      row.push(field); rows.push([...row]); row.length = 0; field = '';
    } else field += char;
  }
  assert.equal(quoted, false, 'CSV must close every quoted field');
  if (row.length || field) { row.push(field); rows.push([...row]); }
  assert(rows.length >= 2, 'CSV must contain a header and data');
  for (const [index, values] of rows.entries()) assert.equal(values.length, rows[0].length, `CSV row ${index + 1} column count`);
  return rows;
}

async function screenshot(name) {
  const path = `${output}/duration-${name}.png`;
  await page.screenshot({path, fullPage: true});
  screenshots.push(path);
}

async function download(button, name) {
  const event = page.waitForEvent('download');
  await page.getByRole('button', {name: button, exact: true}).click();
  const value = await event;
  const path = `${output}/duration-${name}.csv`;
  await value.saveAs(path);
  const rows = parseCsv(await readFile(path, 'utf8'));
  exports.push({path, suggestedFilename: value.suggestedFilename(), dataRows: rows.length - 1, columns: rows[0].length});
  return rows;
}

function csvColumn(rows, name) {
  const index = rows[0].indexOf(name);
  assert(index >= 0, `Missing CSV column ${name}`);
  return rows.slice(1).map(row => row[index]);
}

try {
  const [summary, estimated, unavailable, job] = await Promise.all([
    get(`/api/datasets/${target}/available/summary`),
    get(`/api/datasets/${target}/available/101948.IB`),
    get(`/api/datasets/${target}/available/199285.IB`),
    get(`/api/akshare/sync?target=${target}`),
  ]);
  assert.equal(summary.source, 'akshare');
  assert.equal(summary.groupingBasis, 'modified_duration');
  assert.equal(summary.counts.bonds, 18540);
  assert.equal(summary.coverage.observed, 469);
  assert.equal(summary.counts.eligible, 461);
  assert.equal(summary.counts.durationEstimated, 461);
  assert.equal(summary.counts.durationCalculated, 0);
  assert.equal(job.status, 'partial');
  assert.equal(estimated.name, '23四川18');
  assert.equal(estimated.duration, '13.05282010');
  assert.equal(estimated.termYears, 15);
  assert.equal(estimated.durationCalculation.status, 'estimated');
  assert.equal(unavailable.name, '25湖北债55');
  assert.equal(unavailable.duration, null);
  assert.equal(unavailable.termYears, null);
  assert.equal(unavailable.durationCalculation.status, 'unavailable');
  assert.match(unavailable.durationCalculation.inputs.cashflowConstraintEvidence.join('\n'), /用户原表.*提前偿还/);
  report.counts = summary.counts;
  report.observed = summary.coverage.observed;
  report.sync = {jobId: job.jobId, status: job.status, completed: job.completed, marketEligible: job.marketEligible};
  report.examples = {estimated: {code: estimated.code, duration: estimated.duration, bucket: estimated.termYears},
    unavailable: {code: unavailable.code, reason: unavailable.durationCalculation.reason,
      constraintEvidence: unavailable.durationCalculation.inputs.cashflowConstraintEvidence}};
  checks.push('Local snapshot has 469 dated trades and 461 eligible bonds, all using estimated individual duration');

  browser = await chromium.launch({channel: 'chrome', headless: true});
  page = await browser.newPage({viewport: {width: 1440, height: 1080}, acceptDownloads: true});
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) {
      errors.push(`Blocked external connection: ${url.origin}${url.pathname}`);
      return route.abort();
    }
    if (url.pathname.startsWith('/api/')) {
      requests.push({method: request.method(), path: url.pathname, query: url.search, via: 'browser'});
      if (request.method() !== 'GET') {
        writes.push(`${request.method()} ${url.pathname}`);
        return route.abort();
      }
    }
    return route.continue();
  });
  const table = () => page.getByRole('table', {name: '已保存个券数据', exact: true});
  const settled = () => page.locator('.available-data[aria-busy="false"]').waitFor();
  const switchView = name => page.getByRole('tablist', {name: '数据展示范围', exact: true}).getByRole('tab', {name, exact: true}).click();
  const search = async code => {
    await page.getByLabel('搜索债券代码或名称', {exact: true}).fill(code);
    await settled();
    if (code) await table().getByText(code, {exact: true}).waitFor();
  };
  const openBond = async name => {
    await page.getByRole('button', {name: `查看 ${name} 数据来源`, exact: true}).click();
    const drawer = page.getByRole('dialog', {name: `${name} · 数据来源`, exact: true});
    await drawer.locator('.saved-source-fields section').first().waitFor();
    return drawer;
  };
  await page.goto(`${base}/workbench?date=${target}&cohort=before_20250808&tier=all&scope=all&region=`);
  await table().waitFor();
  await settled();
  const coverage = page.getByLabel('当前数据快照覆盖情况', {exact: true});
  assert.match(await coverage.innerText(), /18,540/);
  assert.match(await coverage.locator('div').filter({hasText: '已有当日个券收益率'}).locator('dd').innerText(), /469/);
  assert.match(await coverage.locator('div').filter({hasText: '可汇总'}).locator('dd').innerText(), /461/);
  assert.match(await table().locator('thead').innerText(), /修正久期/);
  assert.match(await table().locator('thead').innerText(), /久期档位/);
  assert.match(await page.locator('.saved-duration-notice').innerText(), /11 年归入 10 年档/);
  assert.match(await page.locator('.saved-duration-notice').innerText(), /全表估算 461 只/);
  await screenshot('overview');
  checks.push('Workbench coverage, separate remaining term and duration columns, bucket rule and estimate counts render');

  await page.getByLabel('个券发行组', {exact: true}).selectOption('all');
  await settled();
  await search('101948.IB');
  assert.equal(await table().locator('tbody tr').count(), 1);
  assert.match(await table().locator('tbody').innerText(), /13\.0528/);
  assert.match(await table().locator('tbody').innerText(), /15 年档/);
  let drawer = await openBond('23四川18');
  assert.match(await drawer.locator('.duration-calculation').innerText(), /个券修正久期 · 估算/);
  assert.match(await drawer.locator('.duration-calculation').innerText(), /13\.0528/);
  assert.match(await drawer.locator('.duration-calculation').innerText(), /15 年档/);
  assert.match(await drawer.innerText(), /13\.05282010/);
  assert.match(await drawer.locator('.duration-calculation').innerText(), /采用的假设[\s\S]*本金偿还安排未核验/);
  await screenshot('estimated-detail');
  await drawer.getByRole('button', {name: '关闭详情', exact: true}).click();
  let rows = await download('导出已有个券', 'estimated-bond');
  assert.equal(rows.length, 2);
  assert.deepEqual(csvColumn(rows, '代码'), ['101948.IB']);
  assert.deepEqual(csvColumn(rows, '修正久期(年)'), ['13.05282010']);
  assert.deepEqual(csvColumn(rows, '修正久期档位(年)'), ['15']);
  assert.deepEqual(csvColumn(rows, '久期状态'), ['估算']);
  assert.match(csvColumn(rows, '久期假设')[0], /本金偿还安排未核验/);
  checks.push('23四川18 detail and filtered CSV preserve 13.05282010 years, 15-year bucket, estimate label and cashflow assumptions');

  await search('199285.IB');
  assert.equal(await table().locator('tbody tr').count(), 1);
  assert.match(await table().locator('tbody').innerText(), /不可计算/);
  drawer = await openBond('25湖北债55');
  const unavailableText = await drawer.locator('.duration-calculation').innerText();
  assert.match(unavailableText, /个券修正久期 · 不可计算/);
  assert.match(unavailableText, /已知现金流约束及来源/);
  assert.match(unavailableText, /用户原表[\s\S]*提前偿还/);
  assert.match(unavailableText, /缺少可靠还本计划/);
  assert.doesNotMatch(unavailableText, /采用的假设/, 'Rejected duration inputs must not be presented as adopted assumptions');
  await screenshot('unavailable-detail');
  await drawer.getByRole('button', {name: '关闭详情', exact: true}).click();
  rows = await download('导出已有个券', 'unavailable-bond');
  assert.equal(rows.length, 2);
  assert.deepEqual(csvColumn(rows, '代码'), ['199285.IB']);
  assert.deepEqual(csvColumn(rows, '修正久期(年)'), ['']);
  assert.deepEqual(csvColumn(rows, '修正久期档位(年)'), ['']);
  assert.deepEqual(csvColumn(rows, '久期状态'), ['不可计算']);
  assert.deepEqual(csvColumn(rows, '久期假设'), ['']);
  assert.match(csvColumn(rows, '久期不可计算原因')[0], /缺少可靠还本计划/);
  checks.push('25湖北债55 remains unavailable with its workbook repayment warning; rejected assumptions stay hidden and CSV duration/bucket/assumptions remain empty');

  await search('');
  await page.getByLabel('个券数据状态', {exact: true}).selectOption('eligible');
  await settled();
  assert.match(await page.locator('.saved-pagination > span').innerText(), /461/);
  rows = await download('导出已有个券', 'all-eligible');
  assert.equal(rows.length - 1, 461);
  assert(csvColumn(rows, '久期状态').every(value => value === '估算'));
  assert(csvColumn(rows, '修正久期(年)').every(value => Number(value) > 0));
  assert(csvColumn(rows, '修正久期档位(年)').every(value => ['3', '5', '7', '10', '15', '20', '30'].includes(value)));
  assert(csvColumn(rows, '久期假设').every(value => value.includes('本金偿还安排未核验')));
  checks.push('All-issuance eligible export contains 461 rows with consistent column counts and duration/status/assumption fields');

  await switchView('已有样本汇总');
  const summaryTable = page.getByRole('table', {name: '已有样本汇总', exact: true});
  await summaryTable.waitFor();
  assert.match(await summaryTable.locator('thead').innerText(), /修正久期档位/);
  assert.match(await summaryTable.locator('thead').innerText(), /其中估算久期/);
  const summaryValues = await summaryTable.locator('tbody tr').evaluateAll(elements => elements.map(row => [...row.querySelectorAll('td')].map(cell => cell.textContent)));
  assert(summaryValues.length > 0);
  assert(summaryValues.every(row => Number(row[4]) === Number(row[5])), 'Every summarized sample uses estimated duration');
  await screenshot('summary');
  rows = await download('导出样本汇总', 'summary');
  assert(csvColumn(rows, '其中估算久期样本数').every((value, index) => value === csvColumn(rows, '样本数')[index]));
  assert(csvColumn(rows, '修正久期档位(年)').every(value => ['3', '5', '7', '10', '15', '20', '30'].includes(value)));
  await switchView('地区汇总视图');
  await page.locator('.valuation-table').waitFor();
  assert.equal(await page.locator('.valuation-table .region-row').count(), 37);
  assert.match(await page.locator('.valuation-table caption').textContent(), /按久期档位分组/);
  assert.match(await page.locator('.matrix-caption').innerText(), /7 个久期档位/);
  const cell = page.locator('.valuation-table button.cell-button').filter({hasNotText: '—'}).first();
  assert.match(await cell.getAttribute('title'), /其中估算久期/);
  await cell.click();
  drawer = page.getByRole('dialog');
  await drawer.waitFor();
  assert.match(await drawer.innerText(), /修正久期档位范围/);
  assert.match(await drawer.innerText(), /等距归入较大档/);
  assert.match(await drawer.innerText(), /本组可包含估算久期样本/);
  await screenshot('matrix-detail');
  await drawer.getByRole('button', {name: '关闭详情', exact: true}).click();
  checks.push('Summary table/CSV show estimated sample counts; the 37-region matrix explains duration bucket ranges and assumptions');

  await page.setViewportSize({width: 390, height: 844});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile matrix must not overflow the document');
  await screenshot('mobile-matrix');
  await switchView('已有个券');
  await table().waitFor();
  await page.getByRole('button', {name: '筛选', exact: true}).click();
  await page.getByLabel('个券发行组', {exact: true}).selectOption('all');
  await page.getByLabel('个券数据状态', {exact: true}).selectOption('all');
  await settled();
  await search('199285.IB');
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile bond table must stay inside its scroll container');
  drawer = await openBond('25湖北债55');
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile detail must not overflow the document');
  assert(await drawer.locator('.drawer-body').evaluate(element => element.scrollWidth <= element.clientWidth + 1), 'Long constraint evidence must wrap within the mobile drawer');
  await screenshot('mobile-unavailable');
  checks.push('390px mobile table, matrix and long repayment-evidence drawer have no document or drawer overflow');
  assert.deepEqual(errors, []);
  assert.deepEqual(writes, []);
  checks.push('No page errors, external requests or non-GET API requests; synchronization was not started or retried');
  report.status = 'passed';
} catch (error) {
  report.status = 'failed';
  report.failure = {message: error.message, stack: error.stack, url: page?.url()};
  if (page) await screenshot('failure').catch(() => {});
  process.exitCode = 1;
} finally {
  report.passed = checks.length;
  await writeFile(`${output}/browser-duration-report.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify({status: report.status, passed: report.passed, checks, failure: report.failure, errors, writes, exports}, null, 2));
  await browser?.close();
}
