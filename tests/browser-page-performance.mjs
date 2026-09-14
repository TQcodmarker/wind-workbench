// Read-only performance loop: never changes source, credentials or sync jobs.
// node tests/browser-page-performance.mjs
// PERF_RECORD_ONLY=1 records a baseline without enforcing the budgets.
import {chromium} from 'playwright';
import {mkdir,writeFile} from 'node:fs/promises';
import assert from 'node:assert/strict';

const base=process.env.BOND_TEST_URL||'http://127.0.0.1:8765';
const target=process.env.BOND_TEST_DATE||'2026-09-11';
const budgets={firstTableMs:Number(process.env.PERF_FIRST_TABLE_MS||3000),apiDecodedBytes:Number(process.env.PERF_API_BYTES||2097152),interactionMs:Number(process.env.PERF_INTERACTION_MS||500)};
const samples=Math.max(1,Math.min(5,Number(process.env.PERF_SAMPLES||1)));
const results=[],failures=[];
const browser=await chromium.launch({channel:'chrome',headless:true});
try{
 for(let sample=0;sample<samples;sample++){
  const context=await browser.newContext({viewport:{width:1440,height:960}});
  const page=await context.newPage();
  page.setDefaultTimeout(5000);
  const errors=[],writes=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/**',route=>{
   if(route.request().method()==='GET')return route.continue();
   writes.push({method:route.request().method(),path:new URL(route.request().url()).pathname});
   return route.abort();
  });
  await page.addInitScript(()=>{
   window.__pagePerf={firstTableMs:null,longTasks:[],jsonReads:[]};
   new PerformanceObserver(list=>{
    for(const entry of list.getEntries())window.__pagePerf.longTasks.push({startMs:entry.startTime,durationMs:entry.duration});
   }).observe({type:'longtask',buffered:true});
   const observe=new MutationObserver(()=>{
    if(window.__pagePerf.firstTableMs===null&&document.querySelector('table[aria-label="已保存个券数据"] tbody tr')){
     requestAnimationFrame(()=>requestAnimationFrame(()=>{if(window.__pagePerf.firstTableMs===null)window.__pagePerf.firstTableMs=performance.now()}));
    }
   });
   observe.observe(document,{childList:true,subtree:true});
   const originalJson=Response.prototype.json;
   Response.prototype.json=async function(...args){
    const start=performance.now();
    try{return await originalJson.apply(this,args)}finally{
     if(this.url.includes('/api/'))window.__pagePerf.jsonReads.push({path:new URL(this.url).pathname,startMs:start,durationMs:performance.now()-start});
    }
   };
  });
  await page.goto(`${base}/workbench?date=${encodeURIComponent(target)}`,{waitUntil:'domcontentloaded',timeout:15000});
  try{await page.waitForFunction(()=>window.__pagePerf.firstTableMs!==null,{},{timeout:15000})}catch{failures.push(`sample ${sample+1}: first table did not appear within 15 seconds`)}
  const initial=await page.evaluate(()=>({
   ...window.__pagePerf,
   navigation:performance.getEntriesByType('navigation').map(e=>({domContentLoadedMs:e.domContentLoadedEventEnd,loadMs:e.loadEventEnd})),
   requests:performance.getEntriesByType('resource').filter(e=>e.name.includes('/api/')).map(e=>({path:new URL(e.name).pathname,query:new URL(e.name).search,durationMs:e.duration,ttfbMs:e.responseStart-e.startTime,downloadMs:e.responseEnd-e.responseStart,encodedBytes:e.encodedBodySize,decodedBytes:e.decodedBodySize,transferBytes:e.transferSize})),
   rows:document.querySelectorAll('table[aria-label="已保存个券数据"] tbody tr').length,
  }));
  const interactions=[];
  if(initial.firstTableMs!==null){
   const measure=async(name,action,ready)=>{
    const start=performance.now();await action();if(ready)await ready();
    await page.locator('.available-data[aria-busy="false"]').waitFor();
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    interactions.push({name,durationMs:performance.now()-start});
   };
   await measure('next page',()=>page.getByRole('button',{name:'下一页个券',exact:true}).click(),()=>page.locator('.saved-pagination').getByText(/第 2 \/ /).waitFor());
   await measure('filter eligible',()=>page.getByLabel('个券数据状态',{exact:true}).selectOption('eligible'),()=>page.waitForFunction(()=>Array.from(document.querySelectorAll('table[aria-label="已保存个券数据"] tbody tr')).every(row=>row.textContent.includes('可纳入汇总'))));
   await measure('search absent code',()=>page.getByRole('searchbox',{name:'搜索债券代码或名称',exact:true}).fill('PERF-NO-SUCH-BOND'),()=>page.getByRole('heading',{name:'当前筛选没有已保存个券',exact:true}).waitFor());
  }
  const totalApiDecodedBytes=initial.requests.reduce((sum,item)=>sum+item.decodedBytes,0);
  const totalApiEncodedBytes=initial.requests.reduce((sum,item)=>sum+item.encodedBytes,0);
  const result={sample:sample+1,...initial,totalApiDecodedBytes,totalApiEncodedBytes,interactions,writes,errors};results.push(result);
  if(initial.firstTableMs>budgets.firstTableMs)failures.push(`sample ${sample+1}: first table ${initial.firstTableMs.toFixed(0)}ms > ${budgets.firstTableMs}ms`);
  if(totalApiDecodedBytes>budgets.apiDecodedBytes)failures.push(`sample ${sample+1}: initial API decoded payload ${totalApiDecodedBytes} bytes > ${budgets.apiDecodedBytes} bytes`);
  if(initial.requests.some(item=>/\/available$/.test(item.path)))failures.push(`sample ${sample+1}: initial rendering requested the full bond dataset`);
  for(const item of interactions)if(item.durationMs>budgets.interactionMs)failures.push(`sample ${sample+1}: ${item.name} ${item.durationMs.toFixed(0)}ms > ${budgets.interactionMs}ms`);
  assert.deepEqual(writes,[],'performance check must not mutate server state');
  assert.deepEqual(errors,[],'browser errors during performance check');
  await context.close();
 }
}finally{await browser.close()}
await mkdir('test-results',{recursive:true});
const report={measuredAt:new Date().toISOString(),target,budgets,passed:failures.length===0,failures,results};
await writeFile(process.env.PERF_REPORT||'test-results/browser-page-performance.json',JSON.stringify(report,null,2));
console.log(JSON.stringify(report,null,2));
if(failures.length&&process.env.PERF_RECORD_ONLY!=='1')process.exitCode=1;
