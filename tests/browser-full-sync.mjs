// Exercises the real local full-sync controls. Leaves the same task running.
import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
const base=process.env.BOND_TEST_URL||'http://127.0.0.1:8765';
const target='2026-09-11';
const initial=await fetch(`${base}/api/akshare/sync?target=${target}`).then(r=>r.json());
assert(!initial||['paused','queued','running'].includes(initial.status),'Use an absent or active/paused sync job');
await mkdir('test-results',{recursive:true});
const browser=await chromium.launch({channel:'chrome',headless:true});
const errors=[];const checks=[];
try{
 const page=await browser.newPage({viewport:{width:1440,height:960}});
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(`${base}/workbench?date=${target}`);
 const region=page.getByRole('region',{name:'全市场地方债后台同步',exact:true});
 await region.waitFor();
 if(!initial){
  const start=page.waitForResponse(r=>r.url().endsWith('/api/akshare/sync')&&r.request().method()==='POST');
  await region.getByRole('button',{name:'全量同步地方债',exact:true}).click();
  assert.equal((await start).status(),202);
 }else if(initial.status==='paused'){
  await region.getByRole('button',{name:'继续同步',exact:true}).click();
 }else{
  await region.locator('.full-sync-toggle').click();
 }
 await region.getByRole('button',{name:'暂停同步',exact:true}).waitFor();
 let job=await fetch(`${base}/api/akshare/sync?target=${target}`).then(r=>r.json());
 assert.equal(job.source,'akshare');assert.equal(job.universe,'market');
 assert(['queued','running'].includes(job.status));
 const id=job.jobId;
 checks.push('The full-market action persists an AKShare catalog task');
 await page.reload();
 await region.locator('.full-sync-toggle').click();
 await region.getByRole('button',{name:'暂停同步',exact:true}).waitFor();
 assert.equal((await fetch(`${base}/api/akshare/sync?target=${target}`).then(r=>r.json())).jobId,id);
 checks.push('Task identity and progress survive page reload');
 await region.getByRole('button',{name:'暂停同步',exact:true}).click();
 await region.getByRole('button',{name:'继续同步',exact:true}).waitFor();
 job=await fetch(`${base}/api/akshare/sync?target=${target}`).then(r=>r.json());
 assert.equal(job.status,'paused');assert.equal(job.jobId,id);
 checks.push('Pause persists without deleting already acquired data');
 await region.getByRole('button',{name:'继续同步',exact:true}).click();
 await region.getByRole('button',{name:'暂停同步',exact:true}).waitFor();
 job=await fetch(`${base}/api/akshare/sync?target=${target}`).then(r=>r.json());
 assert.equal(job.jobId,id);assert(['queued','running'].includes(job.status));
 assert.equal(job.marketDataComplete,false);
 checks.push('Resume uses the same job and does not claim complete daily quotations');
 assert.match(await region.innerText(),/当日行情完整度/);
 if(!job.catalogComplete)assert.equal(await region.locator('progress').count(),0);
 await page.screenshot({path:'test-results/full-sync-running.png',fullPage:true});
 await page.getByRole('button',{name:'数据源',exact:true}).click();
 await page.getByRole('region',{name:'全市场地方债后台同步',exact:true}).getByText('全市场地方政府债同步',{exact:true}).waitFor();
 checks.push('The same progress controls are accessible on the data-source page');
 assert.deepEqual(errors,[]);
 await writeFile('test-results/browser-full-sync.json',JSON.stringify({checks,errors,job},null,2));
 console.log(JSON.stringify({passed:checks.length,jobId:id,status:job.status,checks},null,2));
}finally{await browser.close();}
