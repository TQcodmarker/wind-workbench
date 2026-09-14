import {chromium} from 'playwright';
import {createServer} from 'node:http';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';

const date='2026-09-11',otherDate='2026-09-10';
const yieldDefinition={metric:'ytm',priceBasis:'close',label:'收盘价到期收益率',source:'Wind'};
const terms=[3,5,7,10,15,20,30],regions=[{id:'hebei',name:'河北省',tier:2,order:1}];
const rules={version:'rules-v3-mcp-values-no-clauses',formula:'Σ（收盘价到期收益率 × 发行规模）÷ Σ发行规模',cutoff:'2025-08-08',terms,yieldDefinition};
const bond={code:'809336.IB',bondId:'809336.IB',name:'26河北23',issuer:'河北省人民政府',regionId:'hebei',bondType:'general',issueDate:'2026-04-20',maturityDate:'2036-04-21',cohort:'on_or_after_20250808',remainingYears:'9.6082',termYears:10,issueAmountYi:'88.2',outstandingBalanceYi:'88.2',couponPct:'1.89',yieldPct:'1.7691',duration:'8.7035',closeNetPrice:'101.0634',currency:'CNY',disposition:'eligible',reason:'已具备样本汇总所需字段',missingFields:[],validationErrors:[],fieldSources:{},sessionIds:['saved-session'],requestIds:['saved-request'],codes:['809336.IB']};
const saved=target=>({evaluationDate:target,scope:'saved_sample',complete:false,rulesVersion:rules.version,mappingVersion:'wind-fields-v6',yieldDefinition,counts:{bonds:target===date?1:0,eligible:target===date?1:0,incomplete:0,excluded:0,conflicted:0,requests:target===date?1:0,sessions:target===date?1:0},bonds:target===date?[bond]:[],cells:[],warnings:[]});
// An acquisition fixture remains available to make an accidental request visible;
// the workbench must neither request it nor render its contents.
const running={scope:'excel_universe',targetDate:date,status:'running',requestedCodes:14023,dataCalls:25,maxDataCalls:1000,progress:{terminal:200,fullyPopulated:190,partial:10},updatedAt:'2026-09-12T15:00:00+08:00'};
const dist=path.resolve('dist');
const server=createServer(async(req,res)=>{
 try{const pathname=new URL(req.url,'http://localhost').pathname,file=pathname.startsWith('/assets/')?path.resolve(dist,'.'+pathname):path.join(dist,'index.html');if(!file.startsWith(dist+path.sep)){res.writeHead(403);res.end();return;}res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});res.end(await readFile(file));}catch{res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin=`http://127.0.0.1:${server.address().port}`;
let browser;
const errors=[],checks=[],requests=[];
try{
 browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900}});
 // Compress the former ten-second polling interval so a reintroduced poll
 // fails quickly, while leaving all other application timers unchanged.
 await page.addInitScript(()=>{const original=window.setTimeout.bind(window);window.setTimeout=(callback,delay,...args)=>original(callback,delay===10000?50:delay,...args);});
 page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin!==origin){errors.push('External request: '+url.origin);await route.abort();return;}
  if(!url.pathname.startsWith('/api/')){await route.continue();return;}
  requests.push({method:request.method(),path:url.pathname});
  if(request.method()!=='GET'){errors.push('Forbidden mutation: '+url.pathname);await route.abort();return;}
  let body;
  if(url.pathname==='/api/bootstrap')body={mode:'wind',latestDate:null,latestSavedDate:date,availableDates:[date],historyStart:'2026-09-01',maxDate:date,regions,terms,yieldDefinition};
  else if(url.pathname==='/api/rules')body=rules;
  else if(url.pathname==='/api/runs')body=[];
  else if(/^\/api\/datasets\/[^/]+\/acquisition$/.test(url.pathname))body=running;
  else if(/^\/api\/datasets\/[^/]+\/available$/.test(url.pathname))body=saved(url.pathname.split('/').at(-2));
  else if(/^\/api\/datasets\/[^/]+$/.test(url.pathname))body={evaluationDate:url.pathname.split('/').at(-1),source:'wind',dataState:'pending',latestAttempt:null,yieldDefinition,snapshot:null};
  else{errors.push('Unexpected endpoint: '+url.pathname);await route.fulfill({status:404,json:{detail:'Unexpected endpoint'}});return;}
  await route.fulfill({json:body});
 });
 const workbench=()=>page.locator('#workbench-page');
 const switchView=name=>page.getByRole('tablist',{name:'数据展示范围',exact:true}).getByRole('tab',{name,exact:true}).click();
 const assertNoAcquisition=async()=>{
  await page.waitForTimeout(180);
  assert.equal(await page.getByRole('region',{name:'Excel 清单取数进度',exact:true}).count(),0);
  assert.equal(await page.getByRole('progressbar',{name:'已处理债券数',exact:true}).count(),0);
  assert.equal(await page.getByRole('button',{name:'刷新已返回数据',exact:true}).count(),0);
  assert.doesNotMatch(await workbench().innerText(),/Excel|excel_universe|清单取数|券已处理|数据调用上限/i);
  assert.equal(requests.filter(request=>request.path.endsWith('/acquisition')).length,0,'Workbench views must never request acquisition progress');
 };
 const changeDate=async target=>{
  const response=page.waitForResponse(response=>response.url()===`${origin}/api/datasets/${target}/available`);
  await page.evaluate(value=>{history.pushState(null,'',`/workbench?date=${value}&cohort=on_or_after_20250808&tier=all&scope=all&region=`);dispatchEvent(new PopStateEvent('popstate'));},target);
  await response;
 };

 await page.goto(`${origin}/workbench?date=${date}&cohort=on_or_after_20250808&tier=all&scope=all&region=`);
 await page.getByRole('table',{name:'已保存个券数据',exact:true}).waitFor();
 await assertNoAcquisition();
 checks.push('已有个券正常加载，页面没有 Excel 清单提示，也不请求取数进度');

 for(const name of ['已有样本汇总','全国完整结果','已有个券']){
  await switchView(name);
  await assertNoAcquisition();
 }
 checks.push('切换个券、样本汇总与全国结果视图后，均不会启动进度轮询');

 const before=requests.filter(item=>item.path.endsWith('/available')).length;
 const refreshed=page.waitForResponse(response=>response.url()===`${origin}/api/datasets/${date}/available`);
 await page.getByRole('button',{name:'刷新结果',exact:true}).click();
 await refreshed;
 await page.getByRole('table',{name:'已保存个券数据',exact:true}).getByText('809336.IB',{exact:true}).waitFor();
 assert(requests.filter(item=>item.path.endsWith('/available')).length>before);
 await assertNoAcquisition();
 checks.push('刷新结果仍读取已有数据，且不会读取 Excel 任务或产生写入');

 await changeDate(otherDate);
 assert.equal(await page.getByText('809336.IB',{exact:true}).count(),0);
 for(const name of ['全国完整结果','已有样本汇总','已有个券']){
  await switchView(name);
  await assertNoAcquisition();
 }
 await changeDate(date);
 await page.getByRole('table',{name:'已保存个券数据',exact:true}).getByText('809336.IB',{exact:true}).waitFor();
 await assertNoAcquisition();
 checks.push('切换到空日期再返回时不串数据，日期切换也不会创建进度轮询');

 await page.reload();
 await page.getByRole('table',{name:'已保存个券数据',exact:true}).getByText('809336.IB',{exact:true}).waitFor();
 await assertNoAcquisition();
 assert(requests.every(request=>request.method==='GET'));
 assert.deepEqual(errors,[]);
 checks.push('刷新页面后保持无 Excel 提示、无 acquisition 请求、无外部请求或 API 写入');
 await mkdir('test-results',{recursive:true});
 await writeFile('test-results/browser-acquisition-progress-report.json',JSON.stringify({passed:checks.length,checks,errors,requests},null,2));
 console.log(JSON.stringify({passed:checks.length,checks},null,2));
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve));}

