"""Map authoritative Wind MCP values while preserving original cell evidence."""
import re
from datetime import date
from decimal import Decimal
from functools import lru_cache
from .domain import REGIONS, number
from .wind_mcp import WindError
from .wind_verification import tables_from

VERSION='wind-fields-v7-closing-yield-alias'
CODE=re.compile(r'^\d{6,9}\.(IB|SH|SZ|BC)$')
ALIASES={
 'code':['Wind代码','债券代码类型'], 'bondId':['主证券代码','跨市场代码'],
 'name':['证券简称'], 'fullName':['证券全称'],
 'issuer':['债务主体名称','发行人名称','发行主体名称'],
 'issueDate':['发行起始日期','发行日期'], 'maturityDate':['到期日期'],
 'issueAmountYi':['发行总额','原始发行规模','首次发行规模'], 'currency':['交易币种'],
 'bondType':['所属概念板块','债券类型','地方债类型'],
 'remainingYears':['剩余期限','剩余到期期限','实际剩余期限','剩余期限_下一行权日'],
 'duration':['修正久期','久期','收盘价修正久期','基于净价的收盘价修正久期'],
 'couponPct':['票面利率','票面利率(当期)','票面利率（当期）','票面利率_发行时'],
 'outstandingBalanceYi':['债券余额','债券存量余额'],
 'closeNetPrice':['收盘价净价','收盘净价'],
}

# Queries request closing YTM. These observed aliases are accepted under that
# request; unrelated coupon, ChinaBond and exercise-yield fields are not aliases.
YIELD_FIELDS={
 '收盘价到期收益率':('ytm','close'), '收盘到期收益率':('ytm','close'),
 '到期收益率(收盘价)':('ytm','close'), '到期收益率（收盘价）':('ytm','close'),
 '到期收益率':('ytm','close'), '收盘价收益率':('ytm','close'),
 '收盘收益率':('ytm','close'),
}
YIELD_METADATA={
 'yieldMetric':['收益率类型','收益率指标'],
 'yieldPriceBasis':['收益率价格口径','收益率价格基准','价格口径'],
 'yieldDate':['收益率数据日期','实际收益率日期','到期收益率日期','收益率日期','收盘行情日期','行情日期','交易日期','数据日期'],
}

PERCENT_UNITS={'%':'1','百分比':'1','百分点':'1','bp':'0.01','bps':'0.01','基点':'0.01',
               '小数':'100','比例':'100','decimal':'100','decimal proportion':'100','1':'100'}
AMOUNT_UNITS={'亿':'1','亿元':'1','亿元人民币':'1','人民币亿元':'1','万元':'0.0001','万':'0.0001',
              '元':'0.00000001','人民币元':'0.00000001'}
NUMERIC_UNITS={
    'yieldPct':PERCENT_UNITS,'couponPct':PERCENT_UNITS,
    'issueAmountYi':AMOUNT_UNITS,'outstandingBalanceYi':AMOUNT_UNITS,
    'remainingYears':{'年':'1','year':'1','years':'1'},
    'duration':{'年':'1','year':'1','years':'1'},
    'closeNetPrice':{'元':'1','元/百元面值':'1','元/100元面值':'1'},
}


def numeric_value(field,cell):
    """Normalize explicit supported units; missing units follow query units."""
    value=number(cell['value'])
    unit=str(cell.get('unit') or '').strip().lower()
    if not unit:return value
    factor=NUMERIC_UNITS[field].get(unit)
    if factor is None:raise ValueError(f'{field} 返回单位暂不支持换算：{unit}')
    return value*Decimal(factor)


def yield_observation(fields,target):
    """Use returned closing-YTM values, grouped by the requested analysis date."""
    row=dict(yieldPct=None,yieldMetric=None,yieldPriceBasis=None,yieldDate=target,
             valuationDate=target,yieldSourceField=None,reportedYieldDates=[],valueStatus='missing')
    sources={key:[] for key in row};errors=[];observations=[]
    for cell in fields:
        name,_=base_name(cell['name'],target)
        if name not in YIELD_FIELDS:continue
        sources['yieldPct'].append(cell)
        scope=tuple(cell.get(key) for key in ('requestId','tableIndex','rowIndex'))
        peers=[c for c in fields if tuple(c.get(key) for key in ('requestId','tableIndex','rowIndex'))==scope]
        metadata={key:[c for c in peers if base_name(c['name'],target)[0] in aliases]
                  for key,aliases in YIELD_METADATA.items()}
        for key,values in metadata.items():
            evidence_key='reportedYieldDates' if key=='yieldDate' else key
            sources[evidence_key].extend(c for c in values if c not in sources[evidence_key])
        bad=[]
        for key,expected,allowed in (
            ('yieldMetric','ytm',{'ytm','到期收益率','yield to maturity'}),
            ('yieldPriceBasis','close',{'close','收盘','收盘价','收盘价格'})):
            values=[str(c['value']).strip().lower() for c in metadata[key] if c['value'] is not None and c['value']!='']
            if values and any(value not in allowed for value in values):bad.append(f'{key} 来源口径冲突或不符合收盘价到期收益率')
            if YIELD_FIELDS[name][0 if key=='yieldMetric' else 1] is not None:sources[key].append(cell)
        dates=[c['value'] for c in metadata['yieldDate'] if c['value'] is not None and c['value']!='']
        row['reportedYieldDates'].extend(value for value in dates if value not in row['reportedYieldDates'])
        numeric=None
        if cell['value'] is not None and cell['value']!='':
            try:numeric=str(numeric_value('yieldPct',cell))
            except ValueError as exc:bad.append(str(exc) if '单位' in str(exc) else 'yieldPct 来源不是有效数值')
        if bad:
            errors.extend(bad)
            continue
        observations.append(dict(yieldPct=numeric,yieldMetric='ytm',yieldPriceBasis='close'))
        sources['yieldSourceField'].append(cell)
    # Ignore empty numeric alternatives, but never choose arbitrarily between
    # conflicting nonempty yields or independently returned observations.
    nonempty=[item for item in observations if item['yieldPct'] is not None]
    chosen=nonempty or observations
    if chosen:
        def comparison(item):
            return (number(item['yieldPct']) if item['yieldPct'] is not None else None,
                    item['yieldMetric'],item['yieldPriceBasis'])
        if any(comparison(item)!=comparison(chosen[0]) for item in chosen[1:]):errors.append('yieldPct 多次返回值冲突')
        else:row.update(chosen[0])
    row['yieldSourceField']='、'.join(dict.fromkeys(c['name'] for c in sources['yieldSourceField'])) or None
    row['valueStatus']='valid' if not errors and row['yieldPct'] is not None else 'missing'
    # query_date is the date policy, not an assertion that Wind returned a date cell.
    sources['valueStatus']=sources['yieldPct']+sources['yieldMetric']+sources['yieldPriceBasis']
    return row,sources,list(dict.fromkeys(errors))


@lru_cache(maxsize=8192)
def base_name(name:str,target:str):
    """Normalize immutable field/date strings, shared across every bond row."""
    d=date.fromisoformat(target)
    markers=[target,f'{d.year}年{d.month}月{d.day}日']
    dated=any(m in name for m in markers)
    for marker in markers:name=name.replace(marker,'')
    name=re.sub(r'\(\s*\)|（\s*）|\[\s*\]|【\s*】','',name).strip('的 :_-')
    # Strip enclosing wrappers, preserving a meaningful suffix such as (收盘价).
    while len(name)>1 and (name[0],name[-1]) in [('(',')'),('（','）'),('[',']'),('【','】')]:
        name=name[1:-1].strip('的 :_-')
    return name,dated


def bond_kinds(value):
    """Read explicit general/special labels, not incidental word substrings."""
    kinds=set()
    for token in re.split(r'[;；]',str(value)):
        token=token.strip()
        for kind,labels in (('general',('一般债','地方政府一般债')),
                            ('special',('专项债','地方政府专项债'))):
            if any(token==label or token.startswith(label+'-') for label in labels):
                kinds.add(kind)
    return kinds


def rows_from(result, request_id, primary=False, target=None):
    output=[]; totals=[]
    for ti,table in enumerate(tables_from(result)):
        names=[base_name(c['name'],target)[0] if target else c['name'] for c in table['columns']]
        code_names=['主证券代码',*ALIASES['code']] if primary else ALIASES['code']
        code_indexes=[index for name in code_names for index,value in enumerate(names) if value==name]
        if not code_indexes:
            if len(names)==1 and any(x in names[0] for x in ['总数','数量','总条数']):
                for values in table['rows']:
                    if type(values[0]) is int and values[0]>=0:totals.append(values[0])
            continue
        for ri,values in enumerate(table['rows']):
            code=next((values[index] for index in code_indexes
                       if isinstance(values[index],str) and CODE.fullmatch(values[index])),None)
            if code is None:
                raise WindError('Wind 返回不可识别的债券代码；原始数据已保存')
            fields=[dict(name=c['name'],value=v,unit=c.get('unit'),requestId=request_id,tableIndex=ti,rowIndex=ri,columnIndex=ci)
                    for ci,(c,v) in enumerate(zip(table['columns'],values))]
            output.append((code,fields))
    if len(set(totals))>1:raise WindError('同一请求的债券总数相互冲突')
    return output,totals[0] if totals else None


class Merge:
    def __init__(self,target):
        self.target=target;self.fields={};self.aliases={}

    def add(self,result,request_id,expected=None,primary=False):
        rows,total=rows_from(result,request_id,primary,self.target)
        resolved=[]
        for code,fields in rows:
            if primary:
                for cell in fields:
                    alias=cell['value']
                    if base_name(cell['name'],self.target)[0] in (*ALIASES['code'],*ALIASES['bondId']) and isinstance(alias,str) and CODE.fullmatch(alias):
                        if alias in self.aliases and self.aliases[alias]!=code:raise WindError('跨市场代码对应多个主证券身份')
                        self.aliases[alias]=code
            if expected is not None and code not in expected:
                explicit=[c['value'] for c in fields if base_name(c['name'],self.target)[0]=='主证券代码' and c['value'] in expected]
                if len(set(explicit))==1:code=explicit[0]
                elif self.aliases.get(code) in expected:code=self.aliases[code]
            resolved.append((code,fields))
        rows=resolved
        unexpected=set(code for code,_ in rows)-set(expected) if expected is not None else set()
        if unexpected:raise WindError('Wind 返回了请求范围外的代码；停止自动合并，原始响应已保存')
        for code,fields in rows:self.fields.setdefault(code,[]).extend(fields)
        return {code for code,_ in rows},total

    def pick(self,code,field,errors,sources,dated=False,optional=False):
        candidates=[]
        for cell in self.fields.get(code,[]):
            name,is_dated=base_name(cell['name'],self.target)
            if name in ALIASES[field] and (not dated or is_dated):
                candidates.append(cell)
        sources[field]=candidates
        if field=='bondId':
            primary=[c for c in candidates if base_name(c['name'],self.target)[0]=='主证券代码' and c['value']]
            if primary:candidates=primary;sources[field]=primary
        # Prefer explicitly dated fields over current/static values where available.
        specific=[c for c in candidates if base_name(c['name'],self.target)[1]]
        if specific:candidates=specific;sources[field]=specific
        nonempty=[c for c in candidates if c['value'] is not None and c['value']!='']
        if not nonempty:return None
        if field=='bondType':
            values=set().union(*(bond_kinds(c['value']) for c in nonempty))
        elif field in NUMERIC_UNITS:
            try:values={numeric_value(field,c) for c in nonempty}
            except ValueError as exc:
                if not optional:errors.append(str(exc) if '单位' in str(exc) else f'{field} 来源不是有效数值')
                return None
        else:values={str(c['value']) for c in nonempty}
        if len(values)>1:
            if not optional:errors.append(f'{field} 多次返回值冲突')
            return None
        if field=='bondType':return next(iter(values),None)
        if field in NUMERIC_UNITS:return str(numeric_value(field,nonempty[0]))
        return nonempty[0]['value']

    def normalize(self,code):
        errors=[];sources={};row={'code':code}
        for field in ALIASES:
            if field=='code':continue
            row[field]=self.pick(code,field,errors,sources,
                optional=field in ('name','fullName','maturityDate','duration','couponPct','outstandingBalanceYi','closeNetPrice','currency'))
        yield_row,yield_sources,yield_errors=yield_observation(self.fields.get(code,[]),self.target)
        row.update(yield_row);sources.update(yield_sources);errors.extend(yield_errors)
        row['bondId']=row['bondId'] if isinstance(row['bondId'],str) and CODE.fullmatch(row['bondId']) else None
        issuer=row.get('issuer')
        matches=[r['id'] for r in REGIONS if issuer in (r['name'],r['name']+'人民政府',r['name']+'财政局',r['name']+'财政厅')]
        row['regionId']=matches[0] if len(matches)==1 else None
        sources['regionId']=sources.get('issuer',[])
        row['_validationErrors']=errors
        # Descriptive fields stay in observations; only financial inputs enter duplicate comparison.
        row['_descriptive']={k:row.pop(k) for k in ('name','fullName','issuer','maturityDate','duration','currency','yieldSourceField','reportedYieldDates','couponPct','outstandingBalanceYi','closeNetPrice')}
        row['_descriptive']['sourceUnits']={field:list(dict.fromkeys(c.get('unit') for c in cells)) for field,cells in sources.items() if cells}
        return row,sources
