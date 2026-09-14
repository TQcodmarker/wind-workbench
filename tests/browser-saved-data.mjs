import {chromium} from 'playwright';
import {createServer} from 'node:http';
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';

// All API responses are local fixtures. Unexpected requests, including every
// mutation and every external connection, are blocked and fail the test.
const ytm={metric:'ytm',priceBasis:'close',label:'收盘价到期收益率',source:'Wind'};
const terms=[3,5,7,10,15,20,30];
const regions=[{id:'shanghai',name:'上海市',tier:1,order:1},{id:'hebei',name:'河北省',tier:2,order:2}];
const version='rules-v3-mcp-values-no-clauses';
const rules={version,formula:'Σ（收盘价到期收益率 × 发行规模）÷ Σ发行规模',cutoff:'2025-08-08',terms,yieldDefinition:ytm,clausePolicy:'not_collected_or_filtered',dataAcceptance:'mcp_returned_values',dateBasis:'query_date'};
const date='2026-09-11';
const emptyDate='2026-09-10';
const prior='before_20250808';
const later='on_or_after_20250808';
const source=(name,value,unit=null,requestId='fixture-market-request')=>({name,value,unit,requestId,sessionId:'fixture-hebei-session',tableIndex:0,rowIndex:0,columnIndex:0});
const baseBond={bondId:null,name:null,issuer:null,regionId:null,bondType:null,issueDate:null,maturityDate:null,cohort:null,remainingYears:null,termYears:null,issueAmountYi:null,outstandingBalanceYi:null,couponPct:null,yieldPct:null,duration:null,closeNetPrice:null,currency:'CNY',disposition:'incomplete',reason:'汇总必需字段尚缺失',missingFields:[],validationErrors:[],fieldSources:{},sessionIds:[],requestIds:[],codes:[]};
const hebei={...baseBond,code:'809336.IB',bondId:'809336.IB',name:'26河北23',issuer:'河北省人民政府',regionId:'hebei',bondType:'general',issueDate:'2026-04-20',maturityDate:'2036-04-21',cohort:later,remainingYears:'9.6082',termYears:10,issueAmountYi:'88.2',outstandingBalanceYi:'88.2',couponPct:'1.89',yieldPct:'1.7691',duration:'8.7035',closeNetPrice:'101.0634',disposition:'eligible',reason:'已具备样本汇总所需字段',sessionIds:['fixture-hebei-session'],requestIds:['fixture-market-request','fixture-basic-request'],codes:['809336.IB','236633.SH'],fieldSources:{yieldPct:[source('2026年9月11日的收盘价收益率','1.7691','%')],duration:[source('2026年9月11日的基于净价的收盘价修正久期','8.7035')],issueAmountYi:[source('发行总额','88.2','亿元','fixture-basic-request')],outstandingBalanceYi:[source('2026年9月11日的债券余额','88.2','亿')]}};
const zero={...hebei,code:'809337.IB',bondId:'809337.IB',name:'零值样本',bondType:'special',codes:['809337.IB'],issueAmountYi:'10',outstandingBalanceYi:'0',couponPct:'0',yieldPct:'0',duration:'0',closeNetPrice:null,fieldSources:{yieldPct:[source('2026年9月11日的收盘价收益率','0','%')],duration:[source('2026年9月11日的基于净价的收盘价修正久期','0')],outstandingBalanceYi:[source('2026年9月11日的债券余额','0','亿')]}};
const shanghai={...baseBond,code:'2305973.IB',bondId:'2305973.IB',name:'23上海债14',issuer:'上海市人民政府',regionId:'shanghai',issueDate:'2023-08-21',maturityDate:'2053-08-22',cohort:prior,outstandingBalanceYi:'2.3',missingFields:['债券类型','剩余期限','发行规模','收盘价到期收益率'],sessionIds:['fixture-shanghai-session'],requestIds:['fixture-count-request'],codes:['2305973.IB'],fieldSources:{outstandingBalanceYi:[{...source('2026年9月11日的债券余额','2.3','亿','fixture-count-request'),sessionId:'fixture-shanghai-session'}]}};
const unknown={...shanghai,code:'809338.IB',bondId:'809338.IB',name:'发行日期待补样本',issuer:'河北省人民政府',regionId:'hebei',issueDate:null,cohort:null,outstandingBalanceYi:'4.5',missingFields:['发行日期','债券类型','剩余期限','发行规模','收盘价到期收益率'],codes:['809338.IB']};
const sampleCell=(bondScope,yieldPct,sampleCount,issueAmountSumYi,weightedYieldSum)=>({cohort:later,regionId:'hebei',termYears:10,bondScope,cellState:'ready',yieldPct,sampleCount,issueAmountSumYi,weightedYieldSum});
function available(target){
 const populated=target===date;
 return {source:'wind',evaluationDate:target,scope:'saved_sample',complete:false,rulesVersion:version,mappingVersion:'wind-fields-v4-mcp-values',yieldDefinition:ytm,counts:{bonds:populated?4:0,eligible:populated?2:0,incomplete:populated?2:0,excluded:0,conflicted:0,requests:populated?3:0,sessions:populated?2:0},bonds:populated?[hebei,zero,shanghai,unknown]:[],cells:populated?[sampleCell('all','1.58894725',2,'98.2','156.03462'),sampleCell('general','1.7691',1,'88.2','156.03462'),sampleCell('special','0',1,'10','0')]:[],warnings:populated?['仅展示该查询日期已保存的债券样本，覆盖范围未经全市场完整性确认']:[]};
}
function summary(target){
 const full=available(target),facets=[];
 for(const bond of full.bonds){const facet=facets.find(item=>item.cohort===bond.cohort&&item.regionId===bond.regionId&&item.bondType===bond.bondType&&item.disposition===bond.disposition);if(facet)facet.count++;else facets.push({cohort:bond.cohort,regionId:bond.regionId,bondType:bond.bondType,disposition:bond.disposition,count:1})}
 return {...full,bonds:[],facets};
}
function paged(target,params){
 const full=available(target),allowed=new Set(regions.filter(region=>(params.get('tier')==='all'||region.tier===Number(params.get('tier')))&&region.name.includes(params.get('region')||'')).map(region=>region.id));
 const cohort=params.get('cohort'),scope=params.get('scope'),status=params.get('status'),query=(params.get('q')||'').toLowerCase();
 const filtered=full.bonds.filter(bond=>(bond.regionId?allowed.has(bond.regionId):params.get('tier')==='all'&&!params.get('region'))&&(['all','overall'].includes(scope)||bond.bondType===scope)&&(cohort==='all'||(cohort==='unknown'?!bond.cohort:bond.cohort===cohort))&&(status==='all'||bond.disposition===status)&&(!query||`${bond.code} ${bond.name} ${bond.bondId} ${bond.codes.join(' ')}`.toLowerCase().includes(query)));
 const pageSize=Number(params.get('page_size')||25),page=Math.max(1,Math.min(Number(params.get('page')||1),Math.ceil(filtered.length/pageSize)||1));
 return {source:'wind',evaluationDate:target,version:'fixture',total:filtered.length,page,pageSize,bonds:filtered.slice((page-1)*pageSize,page*pageSize).map(bond=>({...bond,fieldSources:{}}))};
}
const dist=path.resolve('dist');
const server=createServer(async(req,res)=>{
 try{
  const url=new URL(req.url,'http://localhost');
  const target=path.resolve(dist,'.'+decodeURIComponent(url.pathname));
  if(!target.startsWith(dist+path.sep)&&target!==dist){res.writeHead(403);res.end();return}
  const asset=url.pathname.startsWith('/assets/');
  const file=asset?target:path.join(dist,'index.html');
  const body=await readFile(file);
  res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});res.end(body);
 }catch{res.writeHead(404);res.end()}
});

// Fixture records and assertions are kept in this single script so the test can
// run against a production build without a database or configured Wind account.
const failedRun={runId:'fixture-failed-run',targetDate:date,triggerType:'manual_retry',status:'failed',outcome:null,phase:'获取未完成',createdAt:date+'T09:00:00+08:00',startedAt:date+'T09:00:00+08:00',finishedAt:date+'T09:01:00+08:00',message:'完整取数未完成，已保存收到的响应',counts:null,rulesVersion:version,rules,yieldDefinition:ytm};
let legacyMode=false;
const legacy={metric:'chinabond_valuation',priceBasis:'valuation',label:'中债估值收益率',source:'中债'};
const legacyRules={...rules,version:'rules-v1',formula:'Σ（中债估值收益率 × 发行规模）÷ Σ发行规模',yieldDefinition:legacy,clausePolicy:undefined,dataAcceptance:undefined,dateBasis:undefined};
function dataset(target){
 const snapshot=legacyMode&&target===date?{source:'wind',publishedRunId:'fixture-legacy-snapshot',publishedAt:date+'T08:00:00+08:00',rulesVersion:'rules-v1',mappingVersion:'regions-v1',yieldDefinition:legacy,rules:legacyRules,regions,cells:[prior,later].flatMap(cohort=>regions.flatMap(region=>['all','general','special'].flatMap(bondScope=>terms.map(termYears=>({cohort,regionId:region.id,termYears,bondScope,cellState:'ready',yieldPct:'1.5',sampleCount:1,issueAmountSumYi:'10',weightedYieldSum:'15'})))))}:null;
 return {source:'wind',evaluationDate:target,yieldDefinition:ytm,dataState:snapshot?'ready':target===date?'failed':'pending',latestAttempt:target===date?failedRun:null,snapshot};
}

await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin=`http://127.0.0.1:${server.address().port}`;
let browser;
const checks=[],errors=[],requests=[];
let delayEmpty=false;
try{
 browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900},acceptDownloads:true});
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const request=route.request();const url=new URL(request.url());
  if(url.origin!==origin){errors.push('Blocked external request: '+url.origin+url.pathname);await route.abort();return}
  if(!url.pathname.startsWith('/api/')){await route.continue();return}
  requests.push({method:request.method(),path:url.pathname});
  if(request.method()!=='GET'){errors.push('Blocked API mutation: '+request.method()+' '+url.pathname);await route.fulfill({status:405,json:{detail:'Fixture tests prohibit data acquisition'}});return}
  let body;
  if(url.pathname==='/api/bootstrap')body={mode:'wind',latestDate:null,latestSavedDate:date,availableDates:[date],historyStart:'2026-09-01',maxDate:date,regions,terms,yieldDefinition:ytm};
  else if(url.pathname==='/api/rules')body=rules;
  else if(url.pathname==='/api/runs')body=[failedRun];
  else if(url.pathname==='/api/calendar')body=Array.from({length:11},(_,i)=>({date:'2026-09-'+String(i+1).padStart(2,'0'),dataState:i===10?'failed':'pending',status:i===10?'failed':null,availableBondCount:i===10?3:0}));
  else if(/^\/api\/datasets\/[^/]+\/available\/summary$/.test(url.pathname)){
   const target=url.pathname.split('/')[3];
   if(delayEmpty&&target===emptyDate)await new Promise(resolve=>setTimeout(resolve,1200));
   body=summary(target);
  }
  else if(/^\/api\/datasets\/[^/]+\/available\/page$/.test(url.pathname))body=paged(url.pathname.split('/')[3],url.searchParams);
  else if(/^\/api\/datasets\/[^/]+\/available\/[^/]+$/.test(url.pathname))body=available(url.pathname.split('/')[3]).bonds.find(bond=>bond.code===decodeURIComponent(url.pathname.split('/').at(-1)));
  else if(/^\/api\/datasets\/[^/]+\/available$/.test(url.pathname)){
   const target=url.pathname.split('/').at(-2);
   if(delayEmpty&&target===emptyDate)await new Promise(resolve=>setTimeout(resolve,1200));
   body=available(target);
  }
  else if(/^\/api\/datasets\/[^/]+$/.test(url.pathname))body=dataset(url.pathname.split('/').at(-1));
  else{errors.push('Unexpected API request: '+url.pathname);await route.fulfill({status:404,json:{detail:'Unconfigured test endpoint'}});return}
  await route.fulfill({json:body});
 });
 const table=()=>page.getByRole('table',{name:'已保存个券数据',exact:true});
 const rows=()=>table().locator('tbody tr');
 const settleSaved=async()=>{if(await page.locator('.available-data').count())await page.locator('.available-data[aria-busy="false"]').waitFor()};
 const switchView=name=>page.getByRole('tablist',{name:'数据展示范围',exact:true}).getByRole('tab',{name,exact:true}).click();
 const switchCohort=which=>page.getByRole('tablist',{name:'发行日期分组',exact:true}).getByRole('tab',{name:which===later?/2025-08-08 当天及以后发行/:/2025-08-08 前发行/}).click();
 const exportText=async name=>{
  const event=page.waitForEvent('download');await page.getByRole('button',{name,exact:true}).click();
  const download=await event;return {name:download.suggestedFilename(),text:await readFile(await download.path(),'utf8')};
 };
 await page.goto(`${origin}/workbench?date=${date}&cohort=${prior}&tier=all&scope=all&region=`);
 await table().waitFor();
 await table().getByText('2305973.IB',{exact:true}).waitFor();
 assert.equal(requests.filter(request=>request.path===`/api/datasets/${date}/available`).length,0,'Initial rendering must not request the full dataset');
 assert.equal(await rows().count(),1);
 assert.equal(await page.getByRole('tab',{name:'已有个券',exact:true}).getAttribute('aria-selected'),'true');
 assert.match(await rows().first().innerText(),/23上海债14/);
 assert.match(await rows().first().innerText(),/2\.30/);
 assert.match(await rows().first().innerText(),/—/);
 assert.match(await page.locator('#workbench-page').innerText(),/已有|已保存/);
 assert.match(await page.locator('#workbench-page').innerText(),/样本|完整性/);
 checks.push('全国任务失败且无快照时，默认显示当前日期旧发行组已有的上海静态数据');

 await page.getByLabel('个券数据状态',{exact:true}).selectOption('eligible');
 await settleSaved();
 assert.equal(await page.getByText('2305973.IB',{exact:true}).count(),0);
 await page.getByLabel('个券数据状态',{exact:true}).selectOption('incomplete');
 await settleSaved();
 await table().getByText('2305973.IB',{exact:true}).waitFor();
 await page.getByLabel('个券数据状态',{exact:true}).selectOption('all');
 await settleSaved();
 await page.getByRole('button',{name:'查看待补发行日期的个券',exact:true}).click();
 await table().getByText('809338.IB',{exact:true}).waitFor();
 assert.equal(await rows().count(),1);
 assert.match(await rows().first().innerText(),/4\.50/);
 await page.getByLabel('个券发行组',{exact:true}).selectOption('all');
 await settleSaved();
 assert.equal(await rows().count(),4);
 await page.getByLabel('个券发行组',{exact:true}).selectOption('current');
 await settleSaved();
 await table().getByText('2305973.IB',{exact:true}).waitFor();
 checks.push('状态筛选生效；缺少发行日期的债券仍可查看已有字段，并可选择全部发行组');

 await page.getByLabel('搜索地区',{exact:true}).fill('河北');
 await settleSaved();
 assert.equal(await page.getByText('2305973.IB',{exact:true}).count(),0);
 assert.equal(await page.getByRole('tablist',{name:'发行日期分组',exact:true}).getByRole('tab',{name:/2025-08-08 前发行/}).getAttribute('aria-selected'),'true');
 await switchCohort(later);
 await table().getByText('809336.IB',{exact:true}).waitFor();
 await page.getByLabel('搜索地区',{exact:true}).fill('');
 await settleSaved();
 await page.getByLabel('搜索债券代码或名称',{exact:true}).fill('809336');
 await settleSaved();
 await table().getByText('809336.IB',{exact:true}).waitFor();
 assert.equal(await rows().count(),1);
 const hebeiText=await rows().first().innerText();
 for(const value of ['26河北23','1.7691','8.7035','88.20','101.0634','1.8900'])assert(hebeiText.includes(value),`Existing bond is missing ${value}: ${hebeiText}`);
 assert.match(hebeiText,/2026-04-20/);
 assert.match(hebeiText,/2036-04-21/);
 checks.push('共享地区与发行日期筛选保留用户选择，个券搜索可看到收益率、久期、余额、票息、净价及日期');

 await page.getByRole('button',{name:'查看 26河北23 数据来源',exact:true}).click();
 const detail=page.getByRole('dialog',{name:'26河北23 · 数据来源',exact:true});
 await detail.waitFor();
 await detail.getByText('2026年9月11日的收盘价收益率',{exact:true}).waitFor();
 assert.match(await detail.innerText(),/2026年9月11日的收盘价收益率/);
 assert.match(await detail.innerText(),/1\.7691/);
 assert.match(await detail.innerText(),/%/);
 const rawLinks=await detail.getByRole('link',{name:'原始响应',exact:true}).evaluateAll(links=>links.map(a=>a.getAttribute('href')));
 assert(rawLinks.some(href=>href?.includes('fixture-market-request')),'Raw evidence links must retain the contributing request identifier');
 await page.keyboard.press('Escape');
 checks.push('字段来源保留原始指标名称、值、单位和可追溯到请求的原始响应链接');

 const filteredCsv=await exportText('导出已有个券');
 assert.match(filteredCsv.name,/2026-09-11/);
 assert.match(filteredCsv.text,/809336\.IB/);
 assert.match(filteredCsv.text,/1\.7691/);
 assert.match(filteredCsv.text,/8\.7035/);
 assert.doesNotMatch(filteredCsv.text,/809337\.IB|2305973\.IB/);
 assert.match(filteredCsv.text,/样本|已有个券|已保存/);
 checks.push('个券 CSV 仅导出当前筛选命中的数据，并保留日期和样本范围');

 await page.getByLabel('搜索债券代码或名称',{exact:true}).fill('零值');
 await settleSaved();
 await table().getByText('809337.IB',{exact:true}).waitFor();
 const zeroText=await rows().first().innerText();
 assert((zeroText.match(/0\.0000/g)||[]).length>=3,`Zero coupon, yield and duration must remain numeric: ${zeroText}`);
 assert.match(zeroText,/0\.00/);
 assert.match(zeroText,/—/);
 checks.push('有效零收益率、久期、票息和余额正常显示，空净价仍显示破折号');

 await page.getByLabel('搜索债券代码或名称',{exact:true}).fill('');
 await settleSaved();
 await page.getByLabel('显示口径',{exact:true}).selectOption('general');
 await settleSaved();
 await table().getByText('809336.IB',{exact:true}).waitFor();
 assert.equal(await rows().count(),1);
 await page.getByLabel('地区档位',{exact:true}).selectOption('1');
 await settleSaved();
 assert.equal(await page.getByText('809336.IB',{exact:true}).count(),0);
 await page.getByLabel('地区档位',{exact:true}).selectOption('all');
 await settleSaved();
 await table().getByText('809336.IB',{exact:true}).waitFor();
 checks.push('个券表遵守债券类型和地区档位筛选');

 await switchView('已有样本汇总');
 await page.getByRole('button',{name:'导出样本汇总',exact:true}).waitFor();
 assert.match(await page.locator('#workbench-page').innerText(),/1\.7691/);
 assert.match(await page.locator('#workbench-page').innerText(),/样本/);
 const summaryCsv=await exportText('导出样本汇总');
 assert.match(summaryCsv.text,/1\.7691/);
 assert.match(summaryCsv.text,/88\.2/);
 assert.match(summaryCsv.text,/样本/);
 checks.push('已有合格样本能够按当前范围汇总与导出，明确标注样本口径');

 await switchView('全国完整结果');
 const matrix=()=>page.locator('.valuation-table');
 const matrixRows=()=>matrix().locator('.region-row');
 const cell=name=>matrix().getByRole('button',{name,exact:true});
 await cell('河北省 一般债 10Y 1.7691').waitFor();
 assert.equal(await matrixRows().count(),regions.length);
 assert.equal(await matrix().locator('.cell-button').count(),regions.length*terms.length);
 assert.equal(await matrixRows().filter({hasText:'上海市'}).locator('.cell-button').filter({hasText:'—'}).count(),terms.length);
 assert.equal(await matrix().locator('.cell-button').filter({hasText:'—'}).count(),regions.length*terms.length-1);
 assert.match(await page.locator('.matrix-caption').innerText(),/已有数据/);
 assert.equal(await page.locator('.snapshot-status .status-meta').count(),0,'Saved cells must not acquire a fabricated publication timestamp');
 assert.equal(dataset(date).snapshot,null);
 checks.push('无完整快照时，全国结果按全地区与期限展示已有数据；缺失格保留为空且不伪造发布时间');

 await cell('河北省 一般债 10Y 1.7691').click();
 const matrixDetail=page.getByRole('dialog');
 await matrixDetail.waitFor();
 assert.match(await matrixDetail.locator('.detail-result').innerText(),/1\.7691/);
 assert.match(await matrixDetail.locator('.detail-metrics').innerText(),/88\.20/);
 assert.match(await matrixDetail.innerText(),/已有|样本/);
 assert.doesNotMatch(await matrixDetail.innerText(),/生成于|fixture-failed-run|undefined|合成数据/);
 await matrixDetail.getByRole('button',{name:'查看已有个券',exact:true}).click();
 assert.equal(await page.getByRole('tab',{name:'已有个券',exact:true}).getAttribute('aria-selected'),'true');
 await table().getByText('809336.IB',{exact:true}).waitFor();
 assert.equal(await rows().count(),1);
 checks.push('已有汇总详情显示真实收益率、规模与样本范围，可直接返回对应个券视图');

 await switchView('全国完整结果');
 await page.getByLabel('搜索地区',{exact:true}).fill('河北');
 await settleSaved();
 assert.equal(await matrixRows().count(),1);
 const matrixCsv=await exportText('导出数据');
 assert.match(matrixCsv.name,/2026-09-11/);
 assert.match(matrixCsv.text,/1\.7691/);
 assert.match(matrixCsv.text,/88\.2/);
 assert.match(matrixCsv.text,/已有样本|已有数据/);
 assert.match(matrixCsv.text,/非全国完整|覆盖未完整|覆盖未确认|覆盖范围未/);
 assert.doesNotMatch(matrixCsv.text,/上海市|专项债|fixture-failed-run|publishedRunId|结果版本/);
 assert.equal(matrixCsv.text.trim().split(/\r?\n/).length,2,'Sample matrix export contains only the matching saved cell, not synthetic empty cells');
 checks.push('全国结果导出遵守地区、发行组与口径筛选，只输出已有汇总并明确覆盖未完整');

 await page.getByLabel('搜索地区',{exact:true}).fill('');
 await settleSaved();
 await page.getByLabel('地区档位',{exact:true}).selectOption('1');
 await settleSaved();
 assert.equal(await matrixRows().count(),1);
 assert.match(await matrixRows().first().innerText(),/上海市/);
 assert.equal(await matrix().locator('.cell-button').filter({hasText:'—'}).count(),terms.length);
 await page.getByLabel('地区档位',{exact:true}).selectOption('all');
 await settleSaved();
 await page.getByLabel('显示口径',{exact:true}).selectOption('special');
 await settleSaved();
 await cell('河北省 专项债 10Y 0.0000').waitFor();
 assert.equal(await matrix().locator('.cell-button').filter({hasText:'—'}).count(),regions.length*terms.length-1);
 await cell('河北省 专项债 10Y 0.0000').click();
 await matrixDetail.waitFor();
 assert.match(await matrixDetail.locator('.detail-result').innerText(),/0\.0000/);
 await page.keyboard.press('Escape');
 const zeroCsv=await exportText('导出数据');
 assert.match(zeroCsv.text,/"0"/);
 assert.match(zeroCsv.text,/专项债/);
 assert.doesNotMatch(zeroCsv.text,/1\.7691|一般债/);
 await page.getByLabel('显示口径',{exact:true}).selectOption('all');
 await settleSaved();
 assert.equal(await matrix().locator('.cell-button').count(),regions.length*terms.length*3);
 await cell('河北省 整体 10Y 1.5889').waitFor();
 await switchCohort(prior);
 assert.equal(await matrix().locator('.cell-button').filter({hasText:'—'}).count(),regions.length*terms.length*3);
 assert.equal(await matrix().getByText('1.7691',{exact:true}).count(),0);
 await switchCohort(later);
 await page.getByLabel('显示口径',{exact:true}).selectOption('general');
 await settleSaved();
 checks.push('矩阵遵守地区档位、债券口径与发行组筛选；有效零值正常显示和导出，空格不补零');

 await switchView('已有个券');
 await table().getByText('809336.IB',{exact:true}).waitFor();
 await page.getByRole('button',{name:'刷新结果',exact:true}).click();
 await table().getByText('809336.IB',{exact:true}).waitFor();
 await settleSaved();
 assert(requests.filter(request=>request.path===`/api/datasets/${date}/available/summary`).length>=2);
 checks.push('刷新结果只重新读取本地记录');

 delayEmpty=true;
 await switchView('全国完整结果');
 await cell('河北省 一般债 10Y 1.7691').waitFor();
 await page.getByRole('button',{name:'选择评估日期',exact:true}).click();
 const requestStarted=page.waitForRequest(request=>request.url()===`${origin}/api/datasets/${emptyDate}/available/summary`);
 await page.getByRole('button',{name:new RegExp(`^${emptyDate} `)}).click();
 await requestStarted;
 assert.equal(await page.getByText('809336.IB',{exact:true}).count(),0,'Old-date bonds must disappear before the new response arrives');
 assert.equal(await page.locator('.cell-button').count(),0,'Old-date saved matrix must disappear before the new response arrives');
 assert(await page.getByRole('button',{name:'导出数据',exact:true}).isDisabled());
 await page.waitForResponse(response=>response.url()===`${origin}/api/datasets/${emptyDate}/available/summary`);
 assert.equal(await page.getByText('809336.IB',{exact:true}).count(),0);
 assert.equal(await page.getByText('2305973.IB',{exact:true}).count(),0);
 assert.equal(await page.locator('.cell-button').count(),0);
 await switchView('已有个券');
 assert.equal(await page.getByText('809336.IB',{exact:true}).count(),0);
 checks.push('切日期立即隐藏已有矩阵并禁用导出；无数据日的矩阵和个券均不回填前日数据');

 legacyMode=true;
 await page.goto(`${origin}/workbench?date=${date}&cohort=${later}&tier=all&scope=general&region=`);
 await page.locator('.cell-button').first().waitFor();
 assert.equal(await page.locator('.matrix-caption strong').textContent(),legacy.label);
 assert.equal(await cell('河北省 一般债 10Y 1.5000').count(),1,'Published snapshot must take precedence over the same-date saved cell');
 assert.equal(await matrix().getByText('1.7691',{exact:true}).count(),0);
 assert.doesNotMatch(await page.locator('.matrix-caption').innerText(),/已有数据/);
 const legacyCsv=await exportText('导出数据');
 assert.match(legacyCsv.text,/fixture-legacy-snapshot/);
 assert.match(legacyCsv.text,/中债估值收益率/);
 assert.doesNotMatch(legacyCsv.text,/1\.7691/);
 await page.getByRole('button',{name:'计算口径',exact:true}).click();
 assert.match(await page.locator('.snapshot-label').textContent(),/rules-v1/);
 await page.keyboard.press('Escape');
 await switchView('已有个券');
 await table().getByText('809336.IB',{exact:true}).waitFor();
 assert.match(await page.locator('.saved-bonds-table .yield-column').textContent(),/收盘价到期收益率/);
 await page.getByRole('button',{name:'计算口径',exact:true}).click();
 assert.match(await page.locator('.snapshot-label').textContent(),/rules-v3-mcp-values-no-clauses/);
 assert.match(await page.locator('.rule-list').textContent(),/按查询日期归档/);
 assert.match(await page.locator('.rule-list .formula-box').textContent(),/收盘价到期收益率/);
 await page.keyboard.press('Escape');
 await switchView('全国完整结果');
 await page.locator('.cell-button').first().waitFor();
 assert.equal(await page.locator('.matrix-caption strong').textContent(),legacy.label);
 checks.push('同日旧中债完整快照与新YTM已有数据并存时，各视图标题和计算规则保持各自口径');

 assert(requests.every(request=>request.method==='GET'));
 assert.equal(requests.filter(request=>request.path.endsWith('/acquisition')).length,0);
 checks.push('全流程没有 Wind 取数、API 写入或外部网络请求');
 assert.deepEqual(errors,[]);
 await mkdir('test-results',{recursive:true});
 await writeFile('test-results/browser-saved-data-report.json',JSON.stringify({passed:checks.length,checks,errors,requests},null,2));
 console.log(JSON.stringify({passed:checks.length,checks},null,2));
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve))}
