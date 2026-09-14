"""Single source of truth for grouping and Decimal calculations."""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

REGION_ROWS = [
    ('shanghai','上海市',1),('beijing','北京市',1),('guangdong','广东省',1),('shenzhen','深圳市',1),('zhejiang','浙江省',1),('jiangsu','江苏省',1),
    ('fujian','福建省',2),('xiamen','厦门市',2),('ningbo','宁波市',2),('hebei','河北省',2),('shandong','山东省',2),('anhui','安徽省',2),('shanxi','山西省',2),('henan','河南省',2),('hubei','湖北省',2),('sichuan','四川省',2),('hunan','湖南省',2),('jiangxi','江西省',2),('chongqing','重庆市',2),
    ('inner_mongolia','内蒙古自治区',3),('hainan','海南省',3),('liaoning','辽宁省',3),('shaanxi','陕西省',3),('tibet','西藏自治区',3),('dalian','大连市',3),('tianjin','天津市',3),('xinjiang','新疆维吾尔自治区',3),('xpcc','新疆生产建设兵团',3),('ningxia','宁夏回族自治区',3),('qingdao','青岛市',3),('guangxi','广西壮族自治区',3),('gansu','甘肃省',3),('jilin','吉林省',3),('guizhou','贵州省',3),('yunnan','云南省',3),('heilongjiang','黑龙江省',3),('qinghai','青海省',3)]
REGIONS = [dict(id=k,name=n,tier=t,order=i+1) for i,(k,n,t) in enumerate(REGION_ROWS)]
TERMS = [3,5,7,10,15,20,30]
COHORTS = ['before_20250808','on_or_after_20250808']
SCOPES = ['all','general','special']
LEGACY_YIELD_DEFINITION = dict(metric='chinabond_valuation', priceBasis='valuation', label='中债估值收益率', source='中债')
YIELD_DEFINITION = dict(metric='ytm', priceBasis='close', label='收盘价到期收益率', source='Wind')
LEGACY_RULES = dict(version='rules-v1', mappingVersion='regions-v1', cutoff='2025-08-08', terms=TERMS, regions=REGIONS, historyStart='2026-09-01', rounding='ROUND_HALF_UP', weight='发行规模（亿元）', formula='Σ（中债估值收益率 × 发行规模）÷ Σ发行规模', yieldDefinition=LEGACY_YIELD_DEFINITION)
PREVIOUS_RULES = dict(LEGACY_RULES, version='rules-v2-ytm-close', formula='Σ（收盘价到期收益率 × 发行规模）÷ Σ发行规模', yieldDefinition=YIELD_DEFINITION)
RULES = dict(PREVIOUS_RULES, version='rules-v3-mcp-values-no-clauses',
             clausePolicy='not_collected_or_filtered', dataAcceptance='mcp_returned_values', dateBasis='query_date')


def historical_rules(run, version):
    """Use the rules saved with a run; never relabel an old snapshot as current."""
    saved=run.get('rules')
    if saved and saved.get('version')==version:
        return saved
    if version==LEGACY_RULES['version']:
        return LEGACY_RULES
    if version==PREVIOUS_RULES['version']:
        return PREVIOUS_RULES
    return dict(version=version, yieldDefinition=dict(metric='unknown',priceBasis='unknown',label='收益率（历史口径未记录）',source='未知'),formula='历史计算公式未记录')

def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError('Missing numeric field')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Invalid numeric field') from exc
    if not result.is_finite():
        raise ValueError('Nonfinite numeric field')
    return result

def classify(row, evaluation_date):
    """One set of eligibility rules shared by aggregation and lineage."""
    missing=[]
    if not row.get('bondId'):missing.append('稳定债券身份')
    if not row.get('code'):missing.append('证券代码')
    if row.get('regionId') not in {r['id'] for r in REGIONS}:missing.append('发行地区')
    if row.get('bondType') not in ['general','special']:missing.append('一般/专项分类')
    if row.get('yieldMetric')!=YIELD_DEFINITION['metric'] or row.get('yieldPriceBasis')!=YIELD_DEFINITION['priceBasis']:
        missing.append('收盘价到期收益率口径')
    if row.get('yieldDate')!=evaluation_date or row.get('valuationDate')!=evaluation_date or row.get('valueStatus')!='valid':
        missing.append('目标日有效到期收益率')
    try:
        issued=date.fromisoformat(row['issueDate'])
        if issued>date.fromisoformat(evaluation_date):missing.append('发行日期晚于评估日')
    except (ValueError,KeyError,TypeError):
        issued=None;missing.append('发行日期')
    values={}
    for field,label in [('yieldPct','到期收益率'),('issueAmountYi','发行规模'),('remainingYears','剩余期限')]:
        try:values[field]=number(row.get(field))
        except ValueError:missing.append(label)
    if values.get('remainingYears',Decimal(0))<0:missing.append('负剩余期限')
    missing.extend(row.get('_validationErrors',[]))
    if missing:return dict(disposition='excluded',category='missing',reason='缺失或无效：'+'、'.join(dict.fromkeys(missing)))
    if values['issueAmountYi']<=0:return dict(disposition='excluded',category='nonPositiveAmount',reason='发行规模不大于 0')
    term=int(values['remainingYears'].quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    cohort=COHORTS[0 if issued<date(2025,8,8) else 1]
    if term not in TERMS:return dict(disposition='excluded',category='terms',reason=f'剩余期限取整为 {term} 年，不在目标期限内',term=term,cohort=cohort)
    return dict(disposition='included',category='eligible',reason='符合固定口径',term=term,cohort=cohort)


def calculate(records, evaluation_date, observer=None):
    counts = dict(source=len(records), duplicates=0, missing=0, nonPositiveAmount=0, terms=0, eligible=0, deduplicated=0)
    sums = {(c,r['id'],t,s): [0,Decimal(0),Decimal(0)] for c in COHORTS for r in REGIONS for t in TERMS for s in SCOPES}
    seen = {}
    for row in records:
        identity = row.get('bondId')
        # Old source records may still contain clauses. They are outside the
        # current calculation contract and do not affect cross-market equality.
        comparable_row = {k:v for k,v in row.items() if k not in ('code','_descriptive','earlyRepayment','redemption')}
        if identity and identity in seen:
            # Cross-market listings share an explicit stable identity. Conflicting
            # copies must fail the batch instead of silently keeping one value.
            if comparable_row != seen[identity]:
                if observer:observer(row,dict(disposition='conflict',reason='同一债券身份对应冲突数据'))
                raise ValueError('Conflicting duplicate bond identity')
            counts['duplicates'] += 1
            if observer:observer(row,dict(disposition='duplicate',reason='同券跨市场重复记录',duplicateOf=identity))
            continue
        if identity:
            seen[identity] = comparable_row
        counts['deduplicated'] += 1
        decision=classify(row,evaluation_date)
        if observer:observer(row,decision)
        if decision['disposition']!='included':
            counts[decision['category']]+=1
            continue
        counts['eligible'] += 1
        y,amount=(number(row[k]) for k in ['yieldPct','issueAmountYi'])
        term=decision['term'];cohort=decision['cohort']
        for scope in ['all',row['bondType']]:
            a = sums[(cohort,row['regionId'],term,scope)]
            a[0] += 1; a[1] += amount; a[2] += y*amount
    cells = []
    for (cohort,region,term,scope),(count,amount,weighted) in sums.items():
        cells.append(dict(cohort=cohort,regionId=region,termYears=term,bondScope=scope,cellState='value' if count else 'no_samples',yieldPct=str((weighted/amount).quantize(Decimal('.00000001'),rounding=ROUND_HALF_UP)) if count else None,sampleCount=count,issueAmountSumYi=str(amount),weightedYieldSum=str(weighted),yieldMetric=YIELD_DEFINITION['metric'],yieldPriceBasis=YIELD_DEFINITION['priceBasis']))
    return cells, counts

class DemoReader:
    """Deterministic per-bond fixtures, never a live Wind response."""
    def fetch(self, target):
        if target == '2026-09-06':
            return None # Explicit source fixture; not inferred from weekday.
        day = date.fromisoformat(target).toordinal() - date(2026,9,1).toordinal()
        records = []
        for ci,cohort in enumerate(COHORTS):
            for ri,region in enumerate(REGIONS):
                for ti,term in enumerate(TERMS):
                    for si,scope in enumerate(['general','special']):
                        seed = ri*47+ti*29+ci*31+si*19
                        if seed%41==0 or (region['id']=='xpcc' and term==30 and ci==1):
                            continue
                        for bi in range(2+seed%5):
                            identity=f'DEMO-{ci}-{ri:02}-{ti}-{si}-{bi}'
                            y=Decimal('1.51')+Decimal(ri)*Decimal('.0042')+Decimal(ti)*Decimal('.1475')+Decimal(ci)*Decimal('.057')+Decimal(si)*Decimal('.026')+Decimal(bi)*Decimal('.003')+Decimal(day)*Decimal((seed%9)-4)*Decimal('.0021')
                            if region['id']=='qinghai' and term==3 and ci==0 and si==0:
                                y=Decimal(0)
                            records.append(dict(bondId=identity,code=identity+'.IB',regionId=region['id'],bondType=scope,issueDate='2024-05-15' if ci==0 else '2025-08-08',valuationDate=target,yieldDate=target,yieldMetric=YIELD_DEFINITION['metric'],yieldPriceBasis=YIELD_DEFINITION['priceBasis'],valueStatus='valid',yieldPct=str(y),issueAmountYi=str(Decimal(8+seed%30)+Decimal(bi)*Decimal('2.5')),remainingYears=str(Decimal(term)+Decimal(bi%3-1)*Decimal('.12'))))
        if target=='2026-09-02':
            for row in records: row['remainingYears']='11'
        base = records[0]
        for i,change in enumerate([{'yieldPct':None},{'remainingYears':'10.5'},{'issueAmountYi':'0'},{'valuationDate':'2026-08-31'}]):
            records.append(dict(base,**{'bondId':f'EXCLUDED-{i}','code':f'EXCLUDED-{i}.IB',**change}))
        records.append(dict(base,code=base['code'].replace('.IB','.SH')))
        return records
