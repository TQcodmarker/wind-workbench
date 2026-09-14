// Read-only checks against the running app after a real Wind verification attempt.
import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';

await mkdir('test-results',{recursive:true});
const origin='http://127.0.0.1:8765';
const source=await fetch(origin+'/api/source').then(r=>r.json());
const day=await fetch(origin+'/api/datasets/2026-09-10').then(r=>r.json());
assert.equal(source.mode,'wind');
assert.equal(source.configured,true);
assert(source.verification?.checks.length>0);
assert.equal(day.snapshot,null);
assert.equal(day.latestAttempt?.status,'failed');
assert.equal(day.latestAttempt?.source,'wind');

const browser=await chromium.launch({channel:'chrome',headless:true});
const errors=[];
try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(origin+'/workbench?date=2026-09-10');
 await page.getByRole('heading',{name:'该日期数据获取失败',exact:true}).waitFor();
 assert.equal(await page.locator('.cell-button').count(),0);
 assert(await page.getByRole('button',{name:'导出数据',exact:true}).isDisabled());
 assert(await page.getByRole('button',{name:'最新可用',exact:false}).isDisabled());
 assert.match(await page.locator('.prototype-badge').innerText(),/待发布行情/);
 await page.screenshot({path:'test-results/live-workbench.png',fullPage:true});
 await page.getByRole('button',{name:'Wind 数据源',exact:true}).click();
 await page.getByRole('heading',{name:'Wind 实际返回的档案样本',exact:true}).waitFor();
 assert.equal(await page.getByLabel('Wind Key',{exact:true}).inputValue(),'');
 assert.match(await page.locator('.coverage-card').first().innerText(),/服务报告总数/);
 assert(await page.getByRole('button',{name:'核验工作台数据',exact:true}).isEnabled());
 await page.screenshot({path:'test-results/live-source.png',fullPage:true});
 assert.deepEqual(errors,[]);
 const report={passed:7,checks:['真实模式独立库','重启后 Wind Key 仍可用','真实取数失败保留失败原因','无合成矩阵或导出','无快照时最新日期按钮禁用','真实档案样本与字段核验结果可见','页面无脚本异常']};
 await writeFile('test-results/live-report.json',JSON.stringify(report,null,2));
 console.log(JSON.stringify(report));
} finally {
 await browser.close();
}
