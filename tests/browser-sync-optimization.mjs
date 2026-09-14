// Read-only verification against the running AKShare service and background sync.
import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import {writeFile,mkdir} from 'node:fs/promises';
const base=process.env.BOND_TEST_URL||'http://127.0.0.1:8765';
const target='2026-09-11';
const dataset=await fetch(`${base}/api/datasets/${target}/available`).then(r=>r.json());
const observed=dataset.bonds.filter(b=>b.yieldPct!=null&&b.yieldDate===target&&b.yieldPriceBasis==='latest_trade');
assert.equal(dataset.source,'akshare');
assert.equal(dataset.counts.bonds,18540);
assert.equal(observed.length,469);
assert(dataset.counts.eligible>40,'The prioritized worker must publish at least one newly eligible bond');
assert.equal(dataset.complete,false);
const detail=await fetch(`${base}/api/datasets/${target}/available/${observed[0].code}`).then(r=>r.json());
assert.equal(detail.fieldSources.yieldPct[0].sourceDate,target);
assert.equal(detail.fieldSources.yieldPct[0].sourceFunction,'bond_spot_deal');
assert(detail.tradeObservedAt.startsWith(target));
const browser=await chromium.launch({channel:'chrome',headless:true});
const errors=[],writes=[],checks=['Saved raw trades are replayed with original source and observation time','Prioritized static acquisition increases eligible bonds without changing the total catalog'];
try{
 const page=await browser.newPage({viewport:{width:1440,height:1080}});
 page.on('pageerror',e=>errors.push(e.message));
 page.on('request',r=>{if(r.url().includes('/api/')&&!['GET','HEAD'].includes(r.method()))writes.push(r.method()+' '+r.url())});
 await page.goto(`${base}/workbench?date=${target}`);
 const coverage=page.getByLabel('当前数据快照覆盖情况',{exact:true});
 await coverage.waitFor();
 assert.match(await coverage.innerText(),/18,540/);
 assert.match(await coverage.innerText(),/469/);
 await page.locator('.full-sync-toggle').click();
 await page.locator('.full-sync-metrics').waitFor();
 assert.match(await page.locator('.full-sync-metrics').innerText(),/行情与静态详情齐备/);
 checks.push('Workbench and progress panel separate catalog, details, observed yields and eligibility');
 await mkdir('test-results',{recursive:true});
 await page.screenshot({path:'test-results/sync-optimization-running.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
 checks.push('Mobile viewport has no outer overflow, browser errors or mutation requests');
 const job=await fetch(`${base}/api/akshare/sync?target=${target}`).then(r=>r.json());
 assert.equal(job.status,'running');
 const result={checkedAt:new Date().toISOString(),passed:checks.length,checks,errors,writes,
   datasetAt:dataset.provenance.collectedAt,counts:dataset.counts,observed:observed.length,
   job:{jobId:job.jobId,status:job.status,completed:job.completed,marketObserved:job.marketObserved,marketWithDetails:job.marketWithDetails,marketEligible:job.marketEligible},
   evidenceExample:{code:detail.code,observedAt:detail.tradeObservedAt,source:detail.fieldSources.yieldPct[0]}};
 await writeFile('test-results/browser-sync-optimization.json',JSON.stringify(result,null,2));
 console.log(JSON.stringify(result,null,2));
}finally{await browser.close();}
