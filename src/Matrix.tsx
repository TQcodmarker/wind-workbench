import {Fragment,useMemo} from 'react';
import {Inbox,SearchX} from 'lucide-react';
import {Bootstrap,Cell,Day,SavedData,View,fmt,dayYieldDefinition,scopes,tierName,hasPriorityRules} from './data';

type Props={day:Day|null;boot:Bootstrap;view:View;saved?:SavedData|null;select:(cell:Cell)=>void;reset:()=>void};

export default function Matrix({day,boot,view,saved,select,reset}:Props){
 const currentDay=day?.evaluationDate===view.date?day:null;
 const snapshot=currentDay?.snapshot;
 const currentSaved=saved?.evaluationDate===view.date?saved:null;
 const regions=(snapshot?.regions||boot.regions).filter(r=>(view.tier==='all'||r.tier===Number(view.tier))&&r.name.includes(view.region));
 const columns=view.scope==='all'?scopes:scopes.filter(s=>s.id===(view.scope==='overall'?'all':view.scope));
 const sourceCells=snapshot?.cells??currentSaved?.cells;
 const durationGrouped=(snapshot?snapshot.groupingBasis??snapshot.rules?.groupingBasis:currentSaved?.groupingBasis)==='modified_duration';
 const groupLabel=durationGrouped?'久期档位':'剩余期限分组';
 const priorityRules=hasPriorityRules(snapshot?.rules??currentSaved?.rules);
 const index=useMemo(()=>new Map(sourceCells?.filter(c=>c.cohort===view.cohort).map(c=>[`${c.regionId}/${c.bondScope}/${c.termYears}`,c])),[sourceCells,view.cohort]);
 const definition=snapshot?dayYieldDefinition(currentDay,boot):currentSaved?.yieldDefinition??dayYieldDefinition(currentDay,boot);
 if(!snapshot&&!currentSaved?.counts.bonds)return <div className="empty-state"><span className="empty-icon"><Inbox/></span><span className="empty-date">{view.date}</span><h2>{currentDay?.dataState==='no_data'?'当日无估值数据':currentDay?.latestAttempt?.status==='failed'?'该日期数据获取失败':['queued','running'].includes(currentDay?.latestAttempt?.status||'')?'正在获取该日期数据':'该日期尚未处理'}</h2><p>{currentDay?.latestAttempt?.message||'该日期暂无已保存数据，可切换日期查看。'}</p></div>;
 if(!regions.length)return <div className="empty-state"><span className="empty-icon"><SearchX/></span><h2>未找到匹配地区</h2><p>请调整地区名称或档位筛选。</p><button className="button secondary" onClick={reset}>清除筛选</button></div>;
 return <table className="valuation-table" style={{minWidth:168+columns.length*boot.terms.length*64}}>
  <caption className="sr-only">{view.date} 地方债{definition.label}，按{groupLabel}分组，发行规模加权，单位百分比{snapshot?'':'，已有数据'}</caption>
  <colgroup><col className="region-col"/>{columns.flatMap(s=>boot.terms.map(t=><col className="value-col" key={s.id+t}/>))}</colgroup>
  <thead><tr className="group-head"><th className="region-header" rowSpan={2} scope="col">发行地区</th>{columns.map(s=><th id={`scope-${s.id}`} className={`scope-header scope-${s.id}`} key={s.id} colSpan={boot.terms.length} scope="colgroup">{s.name}</th>)}</tr><tr className="term-head">{columns.flatMap(s=>boot.terms.map(t=><th key={s.id+t} id={`term-${s.id}-${t}`} scope="col" title={`${groupLabel} ${t} 年`}>{t}Y{durationGrouped?<span className="sr-only"> 久期档</span>:null}</th>))}</tr></thead>
  <tbody>{[1,2,3].map(tier=>{
   const group=regions.filter(r=>r.tier===tier);
   return group.length?<Fragment key={tier}>
    <tr className="tier-row"><th colSpan={1+columns.length*boot.terms.length}><span>{tierName(tier)}地区 <span className="tier-count">{group.length} 个地区</span></span></th></tr>
    {group.map(r=><tr className="region-row" key={r.id}>
     <th id={`region-${r.id}`} scope="row" className="region-name">{r.name}</th>
     {columns.flatMap(s=>boot.terms.map((t,i)=>{
      // Missing positions are display-only blanks, never published or exported as a snapshot.
      const cell:Cell=index.get(`${r.id}/${s.id}/${t}`)??{cohort:view.cohort,regionId:r.id,termYears:t,bondScope:s.id,cellState:'empty',yieldPct:null,sampleCount:0,issueAmountSumYi:'0',weightedYieldSum:'0'};
      return <td key={s.id+t} className={`value-cell ${i===0?'group-start':''} ${cell.yieldPct===null?'cell-empty':''}`} headers={`region-${r.id} scope-${s.id} term-${s.id}-${t}`}>
       <button className="cell-button" title={`${r.name} · ${s.name} · ${groupLabel} ${t}Y · ${cell.sampleCount} 只样本${durationGrouped&&cell.estimatedDurationCount!=null?`，其中估算久期 ${cell.estimatedDurationCount} 只`:``}${priorityRules?`，指数久期参考 ${cell.indexDurationCount??0} 只，中债估值 ${cell.valuationYieldCount??0} 只，到期收益率 ${cell.ytmYieldCount??0} 只`:``}`} aria-label={`${r.name} ${s.name} ${groupLabel} ${t}Y ${fmt(cell.yieldPct)}`} onClick={()=>select(cell)} onKeyDown={e=>{
        if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))return;
        e.preventDefault();
        const buttons=Array.from(e.currentTarget.closest('table')!.querySelectorAll<HTMLButtonElement>('.cell-button'));
        const pos=buttons.indexOf(e.currentTarget);
        const offset=e.key==='ArrowLeft'?-1:e.key==='ArrowRight'?1:e.key==='ArrowUp'?-columns.length*boot.terms.length:columns.length*boot.terms.length;
        buttons[pos+offset]?.focus();
       }}>{fmt(cell.yieldPct)}</button>
      </td>;
     }))}
    </tr>)}
   </Fragment>:null;
  })}</tbody>
 </table>;
}
