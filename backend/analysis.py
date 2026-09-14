"""Read-only, deterministic demo assistant; no model fabricates financial numbers."""
import re
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode
from . import storage as store
from .domain import REGIONS, COHORTS, SCOPES, TERMS, YIELD_DEFINITION


def comparable(left, right):
    if any(s['yieldDefinition'].get('metric')=='unknown' or s['yieldDefinition'].get('priceBasis')=='unknown' for s in (left,right)):
        return False
    return (left['rulesVersion'],left['mappingVersion'],left['yieldDefinition']) == (right['rulesVersion'],right['mappingVersion'],right['yieldDefinition'])

def diff(target,base,factor=1):
    if target is None or base is None: return None
    return str((Decimal(str(target))-Decimal(str(base)))*factor)

def select(snapshot, context):
    if not snapshot: return []
    regions={r['id'] for r in snapshot['regions'] if (str(context.get('tier','all'))=='all' or str(r['tier'])==str(context['tier'])) and (not context.get('region') or context['region'] in r['name'])}
    if context.get('regionIds'): regions &= set(context['regionIds'])
    scope='all' if context.get('scope')=='overall' else context.get('scope','all')
    return [c for c in snapshot['cells'] if c['cohort']==context.get('cohort',COHORTS[0]) and c['regionId'] in regions and (context.get('scope','all')=='all' or c['bondScope']==scope) and (not context.get('term') or c['termYears']==int(context['term']))]

def status_text(day):
    if day['snapshot']:
        if day['latestAttempt'] and day['latestAttempt']['outcome']=='no_data':
            return '本次检查未返回新收益率，使用保留的成功快照'
        return '本次更新失败，使用保留的成功快照' if day['latestAttempt'] and day['latestAttempt']['status']=='failed' else '已发布'
    if day['dataState']=='no_data': return '当日无收益率数据'
    if day['latestAttempt'] and day['latestAttempt']['status']=='failed': return '该日期数据获取失败'
    return '该日期尚未处理'

def link(target,context,region=None):
    return '/workbench?'+urlencode(dict(date=target,cohort=context.get('cohort',COHORTS[0]),tier=context.get('tier','all'),scope=context.get('scope','all'),region=region or context.get('region','')))

def query(target,context,base=None,reader=None):
    read=reader or store.read_day
    day=read(target)
    before=read(base) if base else None
    result=dict(mode='compare' if base else 'query',targetDate=target,baseDate=base,context=context,status=status_text(day),baseStatus=status_text(before) if before else None,publishedRunId=day['snapshot']['publishedRunId'] if day['snapshot'] else None,baseRunId=before['snapshot']['publishedRunId'] if before and before['snapshot'] else None,publishedAt=day['snapshot']['publishedAt'] if day['snapshot'] else None,rows=[],link=link(target,context),engine='deterministic')
    result['yieldDefinition']=day['snapshot']['yieldDefinition'] if day['snapshot'] else day.get('yieldDefinition',YIELD_DEFINITION)
    result['baseYieldDefinition']=before['snapshot']['yieldDefinition'] if before and before['snapshot'] else None
    result['comparisonBlocked']=False
    if day.get('source') == 'akshare':
        result.update(source='akshare',scope='saved_sample',complete=False)
    if not day['snapshot']:
        result['message']=f'{target}：{result["status"]}。未使用其他日期替代。'
        return result
    result['groupingBasis']=day['snapshot'].get('groupingBasis', 'remaining_term')
    result['rulesVersion']=day['snapshot']['rulesVersion']
    cells=select(day['snapshot'],context)
    if before and before['snapshot'] and not comparable(day['snapshot'],before['snapshot']):
        result['comparisonBlocked']=True
        result['message']='两日收益率口径、计算规则或地区映射版本不同，无法直接进行同口径比较。'
        return result
    old={(c['cohort'],c['regionId'],c['termYears'],c['bondScope']):c for c in select(before['snapshot'],context)} if before else {}
    names={r['id']:r['name'] for r in day['snapshot']['regions']}
    for cell in cells:
        prev=old.get((cell['cohort'],cell['regionId'],cell['termYears'],cell['bondScope']))
        result['rows'].append(dict(**cell,regionName=names[cell['regionId']],baseYieldPct=prev['yieldPct'] if prev else None,baseSampleCount=prev['sampleCount'] if prev else None,baseAmountYi=prev['issueAmountSumYi'] if prev else None,changeBP=diff(cell['yieldPct'],prev['yieldPct'],100) if prev else None,sampleChange=cell['sampleCount']-prev['sampleCount'] if prev else None,amountChangeYi=diff(cell['issueAmountSumYi'],prev['issueAmountSumYi']) if prev else None,link=link(target,context,names[cell['regionId']])))
    empty=sum(c['yieldPct'] is None for c in cells)
    result['message']=f'找到 {len(cells)} 个汇总位置，其中 {empty} 个无合格样本。'
    if before:
        result['message']+=f' 按目标日 {target} 减基准日 {base} 计算。'
        if not before['snapshot']: result['message']+=f'基准日{status_text(before)}，变化不可用。'
    if result['status']!='已发布': result['message']+=' '+result['status']+'。'
    if day.get('source') == 'akshare':
        result['message']+=' 仅为 AKShare 已验证成交样本，指数参考值未纳入；样本覆盖不同会影响日间变化。'
    return result

def daily_summary(target,context,reader=None,dates=None):
    read=reader or store.read_day
    candidates=[d for d in (dates if dates is not None else store.ready_dates()) if d<target]
    snapshot=read(target)['snapshot']
    base=context.get('baseDate')
    if not base and snapshot:
        base=next((d for d in candidates if read(d)['snapshot'] and comparable(snapshot,read(d)['snapshot'])),None)
    result=query(target,context,base,reader=reader)
    if result.get('comparisonBlocked'):
        result.update(mode='summary',highlights=[])
        return result
    if not context:
        # Automatic daily brief covers both issue cohorts, with separate rankings.
        other=query(target,{'cohort':COHORTS[1]},base,reader=reader)
        result['rows']+=other['rows']
        result['context']={'cohort':'both','scope':'all','tier':'all'}
        if result['publishedRunId']:
            total=len(result['rows']);empty=sum(r['yieldPct'] is None for r in result['rows'])
            result['message']=(f'AKShare 成交样本包含两个发行组中的 {total} 个汇总位置；覆盖未完整，指数替代值未纳入。' if result.get('source')=='akshare' else f'覆盖两个发行日期组、37 个地区、全部期限与三个口径，共 {total} 个汇总位置，其中 {empty} 个无合格样本。')
            if base: result['message']+=f' 目标日 {target}，基准日 {base}。'
            if result.get('baseStatus') and result['baseStatus']!='已发布': result['message']+=f' 基准日：{result["baseStatus"]}。'
            if result['status']!='已发布': result['message']+=f' {result["status"]}。'
    result['mode']='summary'
    sections=[]
    if not result['publishedRunId']: return result
    rows=result['rows']
    if not any(c['sampleCount'] for c in rows):
        result['message']='已完成计算，当前范围无符合口径的债券。'
    elif not base:
        result['message']+=' 没有更早的同口径可用基准日，无法进行历史比较。'
    # Separate each maturity and scope; never mix incomparable rankings.
    for cohort in COHORTS:
        for term in TERMS:
            for scope in SCOPES:
                group=[c for c in rows if c['cohort']==cohort and c['termYears']==term and c['bondScope']==scope and c['changeBP'] is not None]
                if group:
                    largest=max(group,key=lambda c:abs(Decimal(c['changeBP'])))
                    sections.append(dict(cohort=cohort,termYears=term,bondScope=scope,regionName=largest['regionName'],changeBP=largest['changeBP'],sampleChange=largest['sampleChange'],amountChangeYi=largest['amountChangeYi'],link=largest['link']))
    result['highlights']=sections
    result['note']='以上为汇总数据的事实对比。样本与规模变化不能证明因果关系；未进行逐券归因。'
    return result

def respond(body,reader=None,dates=None):
    context=dict(body.get('context') or {})
    text=body.get('prompt','').strip()
    target=context.pop('date','2026-09-10')
    action=body.get('action','query')
    base=body.get('baseDate')
    # Treat issue cutoff as a cohort selector, not as an evaluation date.
    cleaned=re.sub(r'2025(?:-|年)0?8(?:-|月)0?8日?','分界日',text)
    if re.search(r'(分界日|8月8日).{0,5}(后|以后|及以后)|非免税',cleaned): context['cohort']=COHORTS[1]
    elif re.search(r'(分界日|8月8日).{0,3}前|(?<!非)免税',cleaned): context['cohort']=COHORTS[0]
    parsed_dates=[]
    for m in re.finditer(r'(?:(\d{4})[-年])?(\d{1,2})[-月](\d{1,2})日?',cleaned):
        parsed_dates.append(f'{m[1] or target[:4]}-{int(m[2]):02}-{int(m[3]):02}')
    if parsed_dates:
        target=parsed_dates[0]
        if len(parsed_dates)>1: base=parsed_dates[1]
    if re.search(r'今天|昨天|前天|上周|上月|最近几天',cleaned):
        return dict(mode='clarify',message='请填写明确的评估日期，例如 2026-09-10；系统不会把相对日期替换为最近可用日期。')
    aliases={'inner_mongolia':'内蒙古','heilongjiang':'黑龙江','xpcc':'兵团'}
    found=[r['id'] for r in REGIONS if r['name'] in text or aliases.get(r['id'],r['name'][:2]) in text]
    if '兵团' in text:
        found=[i for i in found if i!='xinjiang']+['xpcc']
    if found: context.update(regionIds=list(dict.fromkeys(found)),region='',tier='all')
    match=re.search(r'第?([一二三123])档',text)
    if match:
        context['tier']=str({'一':1,'二':2,'三':3}.get(match[1],match[1]))
        if not found: context.update(region='',regionIds=[])
    if '全部地区' in text or '所有地区' in text: context.update(tier='all',region='',regionIds=[])
    if '专项' in text: context['scope']='special'
    elif '一般' in text: context['scope']='general'
    elif '整体' in text: context['scope']='overall'
    elif '全部口径' in text: context['scope']='all'
    # UI uses overall to distinguish one overall group from all three scopes.
    if context.get('scope')=='overall': context['scope']='overall'
    match=re.search(r'(?<!\d)(\d{1,2})\s*(?:[Yy]|年(?:期|估值|期限|债|\b))',cleaned)
    if match:
        if int(match[1]) not in TERMS:
            return dict(mode='clarify',message='仅支持 3、5、7、10、15、20、30 年期限。')
        context['term']=int(match[1])
    if '全部期限' in text or '所有期限' in text: context['term']=None
    if any(w in text for w in ['比较','对比','变化','涨跌']): action='compare'
    if any(w in text for w in ['摘要','简报']): action='summary'
    if action=='compare' and not base:
        return dict(mode='clarify',message='请选择基准评估日，再进行同口径比较；不会自动替换你指定的日期。')
    from datetime import date
    for value in [target,base]:
        if value:
            try: date.fromisoformat(value)
            except ValueError: return dict(mode='clarify',message='评估日期格式不正确，请使用 YYYY-MM-DD。')
    if action=='summary':
        if base: context['baseDate']=base
        result=daily_summary(target,context,reader=reader,dates=dates)
    else: result=query(target,context,base if action=='compare' else None,reader=reader)
    if any(w in text for w in ['最大','排序','排名']) and action=='compare':
        groups={(r['termYears'],r['bondScope']) for r in result['rows']}
        if len(groups)>1: return dict(mode='clarify',message='排名需限定一个期限和一种债券口径，请先选择，例如 10Y 专项债。')
        result['rows'].sort(key=lambda r: (r['changeBP'] is None,-abs(Decimal(r['changeBP'] or 0))))
    return result
