import {chromium} from 'playwright';
import {createServer} from 'node:http';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';

// Local-only fixtures exercise the lightweight list / single-bond evidence split.
const target='2026-09-11',otherDate='2026-09-10';
const definition={metric:'ytm',priceBasis:'close',label:'收盘价到期收益率',source:'Wind'};
const version='rules-v3-mcp-values-no-clauses';
const regions=[{id:'hebei',name:'河北省',tier:2,order:1}],terms=[3,5,7,10,15,20,30];
const rules={version,formula:'Σ（收盘价到期收益率 × 发行规模）÷ Σ发行规模',cutoff:'2025-08-08',terms,yieldDefinition:definition,clausePolicy:'not_collected_or_filtered',dataAcceptance:'mcp_returned_values',dateBasis:'query_date'};
const bond={code:'809336.IB',bondId:'809336.IB',name:'26河北23',issuer:'河北省人民政府',regionId:'hebei',bondType:'general',issueDate:'2026-04-20',maturityDate:'2036-04-21',cohort:'on_or_after_20250808',remainingYears:'9.6082',termYears:10,issueAmountYi:'88.2',outstandingBalanceYi:'88.2',couponPct:'1.89',yieldPct:'1.7691',duration:'8.7035',closeNetPrice:'101.0634',currency:'CNY',disposition:'eligible',reason:'已具备样本汇总所需字段',missingFields:[],validationErrors:[],fieldSources:{},sessionIds:['saved-session'],requestIds:['saved-request'],codes:['809336.IB','236633.SH']};
const fullBond={...bond,fieldSources:{yieldPct:[{name:'2026年9月11日的收盘价收益率',value:1.7691,unit:'%',requestId:'saved-request',sessionId:'saved-session',tableIndex:0,rowIndex:0,columnIndex:1}]}};
const saved=date=>({source:'wind',evaluationDate:date,scope:'saved_sample',complete:false,rulesVersion:version,mappingVersion:'wind-fields-v4-mcp-values',yieldDefinition:definition,counts:{bonds:date===target?1:0,eligible:date===target?1:0,incomplete:0,excluded:0,conflicted:0,requests:date===target?1:0,sessions:date===target?1:0},bonds:date===target?[bond]:[],cells:[],warnings:[]});
const dist=path.resolve('dist');
const server=createServer(async(req,res)=>{
 try{
  const url=new URL(req.url,'http://localhost'),file=url.pathname.startsWith('/assets/')?path.resolve(dist,'.'+url.pathname):path.join(dist,'index.html');
  if(!file.startsWith(dist+path.sep)){res.writeHead(403);res.end();return;}
  res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});res.end(await readFile(file));
 }catch{res.writeHead(404);res.end();}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin=`http://127.0.0.1:${server.address().port}`;
let browser,detailCount=0,detailDelay=150,failNext=false;
const errors=[],checks=[],requests=[];
try{
 browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900}});
 page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url());
  if(url.origin!==origin){errors.push('External request: '+url.origin);await route.abort();return;}
  if(!url.pathname.startsWith('/api/')){await route.continue();return;}
  requests.push({method:request.method(),path:url.pathname});
  if(request.method()!=='GET'){errors.push('Forbidden mutation: '+url.pathname);await route.abort();return;}
  let body;
  if(url.pathname==='/api/bootstrap')body={mode:'wind',latestDate:null,latestSavedDate:target,availableDates:[target],historyStart:'2026-09-01',maxDate:target,regions,terms,yieldDefinition:definition};
  else if(url.pathname==='/api/rules')body=rules;
  else if(url.pathname==='/api/runs')body=[];
  else if(url.pathname===`/api/datasets/${target}/available/${bond.code}`){
   detailCount++;
   const failed=failNext;failNext=false;
   await new Promise(resolve=>setTimeout(resolve,detailDelay));
   try{await route.fulfill({status:failed?503:200,json:failed?{detail:'本地来源暂时读取失败'}:fullBond});}catch{}
   return;
  }
  else if(/^\/api\/datasets\/[^/]+\/acquisition$/.test(url.pathname))body=null;
  else if(/^\/api\/datasets\/[^/]+\/available\/summary$/.test(url.pathname)){const full=saved(url.pathname.split('/')[3]);body={...full,bonds:[],facets:full.bonds.map(bond=>({cohort:bond.cohort,regionId:bond.regionId,bondType:bond.bondType,disposition:bond.disposition,count:1}))};}
  else if(/^\/api\/datasets\/[^/]+\/available\/page$/.test(url.pathname)){const full=saved(url.pathname.split('/')[3]);body={source:'wind',evaluationDate:full.evaluationDate,version:'fixture',total:full.bonds.length,page:1,pageSize:25,bonds:full.bonds};}
  else if(/^\/api\/datasets\/[^/]+\/available$/.test(url.pathname))body=saved(url.pathname.split('/').at(-2));
  else if(/^\/api\/datasets\/[^/]+$/.test(url.pathname))body={evaluationDate:url.pathname.split('/').at(-1),source:'wind',dataState:'pending',latestAttempt:null,yieldDefinition:definition,snapshot:null};
  else{errors.push('Unexpected endpoint: '+url.pathname);await route.fulfill({status:404,json:{detail:'Unexpected endpoint'}});return;}
  await route.fulfill({json:body});
 });
 await page.goto(`${origin}/workbench?date=${target}&cohort=on_or_after_20250808&tier=all&scope=all&region=`);
 const openDetail=()=>page.getByRole('button',{name:'查看 26河北23 数据来源',exact:true}).click();
 const dialog=()=>page.getByRole('dialog',{name:'26河北23 · 数据来源',exact:true});
 await page.getByRole('table',{name:'已保存个券数据',exact:true}).waitFor();
 assert.equal(detailCount,0,'List rendering must not fetch every bond evidence');
 assert.match(await page.getByRole('table',{name:'已保存个券数据',exact:true}).innerText(),/1\.7691/);
 await openDetail();
 await dialog().getByRole('status').waitFor();
 await dialog().getByText('2026年9月11日的收盘价收益率',{exact:true}).waitFor();
 assert.equal(detailCount,1);
 assert.match(await dialog().getByRole('link',{name:'原始响应',exact:true}).getAttribute('href'),/saved-request/);
 checks.push('列表不请求来源；点击一只债券才读取该券完整原始字段');
 await page.keyboard.press('Escape');

 failNext=true;
 await openDetail();
 await dialog().getByRole('alert').waitFor();
 assert.match(await dialog().getByRole('alert').innerText(),/本地来源暂时读取失败/);
 await dialog().getByRole('button',{name:'重新读取个券来源',exact:true}).click();
 await dialog().getByText('2026年9月11日的收盘价收益率',{exact:true}).waitFor();
 assert.equal(detailCount,3);
 checks.push('单券来源加载失败可重试，个券列表和已有数值不受影响');
 await page.keyboard.press('Escape');

 detailDelay=1000;
 await openDetail();
 await dialog().getByRole('status').waitFor();
 await page.evaluate(date=>{
  history.pushState(null,'',`/workbench?date=${date}&cohort=on_or_after_20250808&tier=all&scope=all&region=`);
  dispatchEvent(new PopStateEvent('popstate'));
 },otherDate);
 await page.waitForResponse(response=>response.url()===`${origin}/api/datasets/${otherDate}/available/summary`);
 await page.waitForTimeout(1100);
 assert.equal(await dialog().count(),0);
 assert.equal(await page.getByText('809336.IB',{exact:true}).count(),0);
 assert.equal(await page.getByText('2026年9月11日的收盘价收益率',{exact:true}).count(),0);
 checks.push('读取来源途中切换日期会取消请求，旧日期来源不会回写新页面');
 assert(requests.every(request=>request.method==='GET'));
 assert.deepEqual(errors,[]);
 await mkdir('test-results',{recursive:true});
 await writeFile('test-results/browser-lazy-evidence-report.json',JSON.stringify({passed:checks.length,checks,errors,requests},null,2));
 console.log(JSON.stringify({passed:checks.length,checks},null,2));
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve));}
