import {useEffect,useId,useRef,useState} from 'react';
import {Database,Info,LoaderCircle,Pause,Play,RefreshCw,X} from 'lucide-react';
import {api,time,RuleData,hasPriorityRules} from './data';
import './full-sync.css';

type SyncJob={jobId:string;targetDate:string;universe:'market';status:'queued'|'running'|'paused'|'completed'|'partial'|'failed';phase:string;total:number|null;completed:number;failed:number;pending:number;discovered?:number;catalogCompletedPartitions?:number;catalogTotalPartitions?:number;marketObserved?:number;marketWithDetails?:number;marketEligible?:number;catalogComplete:boolean;marketDataComplete:false;createdAt:string;updatedAt:string;message:string};
type Props={rules?:RuleData|null;targetDate:string;compact?:boolean;onDataChange?:()=>void};
const activeJob=(job:SyncJob|null)=>Boolean(job&&['queued','running'].includes(job.status));
const statusLabel=(status:SyncJob['status'])=>({queued:'等待执行',running:'同步中',paused:'已暂停',completed:'本轮同步完成',partial:'部分完成',failed:'同步失败'}[status]);

export default function FullSync({targetDate,compact=false,onDataChange,rules}:Props){
 const priorityRules=hasPriorityRules(rules);
 const [job,setJob]=useState<SyncJob|null>(null),[loaded,setLoaded]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[expanded,setExpanded]=useState(false),[retry,setRetry]=useState(0);
 const readController=useRef<AbortController|null>(null),actionController=useRef<AbortController|null>(null),busyRef=useRef(false),latestJob=useRef<SyncJob|null>(null),currentDate=useRef(targetDate),changeRef=useRef(onDataChange);
 const wakePoll=useRef<(()=>void)|null>(null);
 currentDate.current=targetDate;changeRef.current=onDataChange;
 const panelId=useId();
 const current=job?.targetDate===targetDate?job:null;
 const active=activeJob(current),canResume=Boolean(current&&['paused','partial','failed'].includes(current.status));

 function accept(value:SyncJob|null){
  const previous=latestJob.current;latestJob.current=value;setJob(value);setLoaded(true);setError('');
  // Poll only the small job record. The parent refreshes datasets on its normal
  // interval; load the final batch once when this job stops making progress.
  if(previous&&value&&previous.status!==value.status&&['paused','completed','partial','failed'].includes(value.status))changeRef.current?.();
 }

 useEffect(()=>{
  let mounted=true,timer:ReturnType<typeof setTimeout>|undefined;
  latestJob.current=null;setJob(null);setLoaded(false);setError('');setBusy(false);busyRef.current=false;
  if(!targetDate)return;
  async function poll(){
   if(!mounted)return;
   if(busyRef.current){timer=setTimeout(poll,3000);return;}
   const controller=new AbortController();readController.current=controller;
   try{const value=await api<SyncJob|null>(`/akshare/sync?target=${encodeURIComponent(targetDate)}`,{signal:controller.signal});if(mounted&&!controller.signal.aborted&&currentDate.current===targetDate)accept(value);}
   catch(reason){if(mounted&&!controller.signal.aborted)setError((reason as Error).message);}
   finally{if(mounted)timer=setTimeout(poll,activeJob(latestJob.current)?3000:15000);}
  }
  wakePoll.current=()=>{clearTimeout(timer);timer=setTimeout(poll,3000);};
  void poll();
  return()=>{mounted=false;wakePoll.current=null;clearTimeout(timer);readController.current?.abort();actionController.current?.abort();};
 },[targetDate,retry]);

 async function action(kind:'start'|'pause'|'resume'){
  if(busyRef.current||!targetDate||(kind!=='start'&&!current))return;
  busyRef.current=true;setBusy(true);setError('');setExpanded(true);readController.current?.abort();
  const controller=new AbortController();actionController.current=controller;
  const requestedDate=targetDate;
  try{
   const path=kind==='start'?'/akshare/sync':`/akshare/sync/${encodeURIComponent(current!.jobId)}/${kind}`;
   const value=await api<SyncJob>(path,{method:'POST',body:JSON.stringify(kind==='start'?{targetDate:requestedDate}:{}),signal:controller.signal});
   if(!controller.signal.aborted&&currentDate.current===requestedDate){accept(value);wakePoll.current?.();}
  }catch(reason){if(!controller.signal.aborted&&currentDate.current===requestedDate)setError((reason as Error).message);}
  finally{if(!controller.signal.aborted&&currentDate.current===requestedDate){busyRef.current=false;setBusy(false);}}
 }

 const buttonLabel=canResume?(current?.status==='paused'?'继续同步':'重试未成功项'):'全量同步地方债';
 const knownTotal=current?.total!=null&&current.catalogComplete;
 const discovered=current?.discovered??current?.total;
 const countText=current?(knownTotal?`档案 ${current.completed.toLocaleString()} / ${current.total!.toLocaleString()}`:discovered!=null?`已发现 ${discovered.toLocaleString()} 只`:active?'收集全市场目录':`${statusLabel(current.status)} · 目录待补齐`):'同步说明';
 return <section className={`full-sync ${compact?'full-sync-compact':'full-sync-panel'}`} aria-label="全市场地方债后台同步">
  <div className="full-sync-actions">
   <button className="button primary" disabled={busy||!targetDate||!loaded||active||current?.status==='completed'} onClick={()=>action(canResume?'resume':'start')}>{busy||active?<LoaderCircle className="spin"/>:canResume?<Play/>:<Database/>}{buttonLabel}</button>
   {compact?<button className="text-button full-sync-toggle" aria-expanded={expanded} aria-controls={panelId} onClick={()=>setExpanded(value=>!value)}>{current?<span className={`dot ${active?'sync-active':''}`}/>:<Info/>}{countText}</button>:null}
  </div>
  {!compact||expanded?<div className="full-sync-progress" id={panelId} onKeyDown={event=>{if(compact&&event.key==='Escape')setExpanded(false)}}>
   <div className="full-sync-title"><strong>全市场地方政府债同步</strong>{compact?<button className="icon-button" onClick={()=>setExpanded(false)} aria-label="收起全量同步进度"><X/></button>:null}</div>
   <p className="full-sync-help">{targetDate||'请选择评估日期'} · AKShare 全市场目录与静态档案</p>
   <p className="full-sync-help full-sync-priority">{priorityRules?'优先补已有行情券的静态详情；完成后匹配已保存的同日收益率与久期来源，按当前优先级选择并重算汇总。':'优先补已有成交券的静态详情；详情完成后自动匹配已保存同日行情，计算个券修正久期并按最近久期档位重算汇总。'}</p>
   {current?<>
    <div className="full-sync-state" role="status"><span className={`badge ${current.status==='completed'?'success':current.status==='failed'?'danger':'neutral'}`}>{statusLabel(current.status)}</span><strong className="full-sync-phase">{current.phase||'等待下一次进度更新'}</strong></div>
    {current.message&&current.message!==current.phase?<p className="full-sync-help">{current.message}</p>:null}
    <dl className="full-sync-metrics">
     <div><dt>全市场目录</dt><dd>{current.catalogComplete?'已收集完整':'仍在补齐，总数待确认'}</dd></div>
     {discovered!=null?<div><dt>已发现债券</dt><dd>{discovered.toLocaleString()} 只</dd></div>:null}
     {current.catalogCompletedPartitions!=null&&current.catalogTotalPartitions!=null&&current.catalogTotalPartitions>0?<div><dt>已核验发行年份</dt><dd>{current.catalogCompletedPartitions.toLocaleString()} / {current.catalogTotalPartitions.toLocaleString()} 年份</dd></div>:null}
     <div><dt>静态详情已完成</dt><dd>{!current.catalogComplete&&current.completed===0?'等待目录收集':`${current.completed.toLocaleString()} 只${knownTotal?` / ${current.total!.toLocaleString()} 只`:''}`}</dd></div>
     <div><dt>待处理 / 未成功</dt><dd>{current.catalogComplete?`${current.pending.toLocaleString()} / ${current.failed.toLocaleString()} 只`:`待处理数待确认 / 未成功 ${current.failed.toLocaleString()} 只`}</dd></div>
     <div className="full-sync-market-start"><dt>已有当日成交覆盖</dt><dd>{current.marketObserved==null?'待更新':`${current.marketObserved.toLocaleString()} 只`}</dd></div>
     <div><dt>其中行情与静态详情齐备</dt><dd>{current.marketWithDetails==null?'待更新':`${current.marketWithDetails.toLocaleString()} 只`}</dd></div>
     <div><dt>可汇总</dt><dd>{current.marketEligible==null?'待更新':`${current.marketEligible.toLocaleString()} 只`}</dd></div>
     <div><dt>当日行情完整度</dt><dd>尚未完整</dd></div>
    </dl>
    <p className="full-sync-help">{priorityRules?'当日成交覆盖按本任务目录匹配的有效成交行情计数；其他可用收益率来源在已有数据页面另计。可汇总还需满足同日收益率、分类、发行规模和久期选择规则。':'当日收益率按本任务目录匹配的有效个券行情计数；可汇总还需满足日期、分类、发行规模和个券修正久期计算规则。'}久期按最近的标准档位分组，11 年归入 10 年档。目录已收齐不代表行情已齐备。</p>
    {knownTotal&&current.total!>0?<progress aria-label="静态详情同步进度" max={current.total!} value={Math.min(current.completed,current.total!)}/>:!current.catalogComplete?<p className="full-sync-help">目录总数尚未确认，完成收集后显示静态详情进度。</p>:null}
    {active?<button className="button secondary" disabled={busy} onClick={()=>action('pause')}><Pause/>暂停同步</button>:null}
    <p className="full-sync-help">最近更新 {time(current.updatedAt)}<br/>任务 {current.jobId}</p>
   </>:<p className="full-sync-help">{loaded?'尚未启动全量同步。将收集全市场地方债目录，再逐券保存静态详情。':'正在读取后台同步任务…'}</p>}
   <p className="full-sync-help">普通更新用于获取新的当日行情；已经保存的同日行情可以复用。{priorityRules?'收益率优先使用同日中债估值，无则使用到期收益率；久期依次采用个券直接值、计算或估算值、同日地方债指数参考。指数久期作为分组代理单独标识，指数和曲线收益率不作个券收益率。':'个券久期由现金流与同日收益率计算，未核实的计息或还本假设标为“估算”；无法计算时保留空值。未偿余额不直接拦截汇总。指数及曲线参考不用于个券久期计算或汇总。'}</p>
   <p className="full-sync-help">任务在后台运行，关闭页面后继续；可暂停并从断点继续。请保持本机服务和电脑运行。</p>
   {error?<div className="inline-error" role="alert">{error}<button className="text-button" disabled={busy} onClick={()=>setRetry(value=>value+1)}><RefreshCw/>重新读取同步状态</button></div>:null}
  </div>:error?<button className="text-button full-sync-error" onClick={()=>setExpanded(true)}>同步状态读取失败</button>:null}
 </section>;
}
