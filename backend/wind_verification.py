"""Verify actual Wind replies before enabling any yield publication.

The current natural-language tools may truncate tables or omit requested fields.
None of these replies is treated as a complete market snapshot.
"""
import json
from . import storage as store
from .credentials import read_wind_key
from .wind_mcp import WindMCP, WindError
from .lineage import Recorder


def tables_from(result):
    tables = []
    for item in result.get('content', []):
        if item.get('type') != 'text':
            continue
        try:
            payload = json.loads(item['text'])
        except (ValueError, KeyError):
            continue
        if not isinstance(payload, dict) or payload.get('error'):
            raise WindError('Wind 数据工具返回业务错误，未写入行情')
        data = payload.get('data')
        if not isinstance(data, dict):
            continue
        for table in data.get('data', []):
            columns, rows = table.get('columns'), table.get('rows')
            if not isinstance(columns, list) or not isinstance(rows, list):
                raise WindError('Wind 表格结构异常')
            if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
                raise WindError('Wind 表格行与字段数量不一致')
            if not all(isinstance(c, dict) and isinstance(c.get('name'), str) for c in columns):
                raise WindError('Wind 返回无效字段名')
            tables.append(table)
    return tables


def inspect_universe(tables):
    codes = set()
    reported = []
    columns = set()
    preview = []
    for table in tables:
        names = [c['name'] for c in table['columns']]
        columns.update(names)
        if 'Wind代码' in names:
            index = names.index('Wind代码')
            for row in table['rows']:
                if isinstance(row[index], str) and row[index] not in codes:
                    codes.add(row[index])
                    if len(preview) < 5:
                        preview.append(dict(zip(names, row)))
        elif len(names) == 1 and any(word in names[0] for word in ('总条数', '债券数量', '总数')):
            for row in table['rows']:
                if type(row[0]) is int and row[0] >= 0:
                    reported.append(row[0])
    total = reported[0] if reported and len(set(reported)) == 1 else None
    complete = total is not None and total == len(codes) and total > 0
    return {'received':len(codes), 'reportedTotal':total, 'complete':complete,
            'columns':sorted(columns), 'preview':preview}


def inspect_valuation(tables, target):
    # Keep this internal name for callers; both verification and publication now
    # use the same accepted Wind values and query-date grouping rules.
    from .domain import RULES,YIELD_DEFINITION
    from .wind_mapping import yield_observation
    fields_by_code={}
    columns = set()
    for ti,table in enumerate(tables):
        names = [c['name'] for c in table['columns']]
        columns.update(names)
        if 'Wind代码' not in names:continue
        for ri,row in enumerate(table['rows']):
            code = row[names.index('Wind代码')]
            if not isinstance(code,str):continue
            fields_by_code.setdefault(code,[]).extend(
                dict(name=c['name'],value=value,unit=c.get('unit'),requestId='verification',
                     tableIndex=ti,rowIndex=ri,columnIndex=ci)
                for ci,(c,value) in enumerate(zip(table['columns'],row)))
    observations=[yield_observation(fields,target) for fields in fields_by_code.values()]
    return {'received':len(observations),
            'nonNullYields':sum(row['yieldPct'] is not None for row,_,_ in observations),
            'verifiedDateYields':sum(row['valueStatus']=='valid' for row,_,_ in observations),
            'rejectedYields':sum(bool(errors) for _,_,errors in observations),
            'yieldDefinition':YIELD_DEFINITION,'rulesVersion':RULES['version'],
            'dateBasis':'query_date','dataAcceptance':'mcp_returned_values','columns':sorted(columns)}


def save_report(report):
    with store.connection() as db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('wind_verification',?)", (json.dumps(report,ensure_ascii=False),))


def read_report():
    with store.connection() as db:
        row = db.execute("SELECT value FROM metadata WHERE key='wind_verification'").fetchone()
    return json.loads(row['value']) if row else None


async def verify(target):
    from .domain import RULES,YIELD_DEFINITION
    recorder=Recorder('数据源能力核验',target)
    report = {'targetDate':target, 'checkedAt':store.now(), 'status':'blocked',
              'scope':'上海市地方政府债券（能力核验样本，非全国完整快照）',
              'yieldDefinition':YIELD_DEFINITION,'rulesVersion':RULES['version'],
              'dateBasis':'query_date','dataAcceptance':'mcp_returned_values',
              'clausePolicy':'not_collected_or_filtered','checks':[], 'preview':[]}
    raw = {}
    try:
        async with WindMCP(read_wind_key(),recorder=recorder) as mcp:
            tools = await mcp.list_tools()
            report['toolCount'] = len(tools)
            report['connectionVerified'] = True
            question = f'查询截至{target}上海市政府发行且尚未到期的全部地方政府债券（一般债和专项债），返回完整债券代码名单、总条数、是否截断或分页，以及各券发行地区、债券类型、发行日期、发行总额及单位、起息日、到期日、跨市场代码。需要可核对的原始表格数据，不要抽样或估算；无法提供全量名单请明确说明。'
            raw['universe'] = await mcp.call('get_bond_basicinfo', {'question':question})
            universe = inspect_universe(tables_from(raw['universe']))
            report['preview'] = universe.pop('preview')
            report['universe'] = universe
            total = universe['reportedTotal']
            report['checks'].append({'name':'历史地方债全集与批量获取','status':'通过' if universe['complete'] else '未通过',
                'detail':f'上海返回 {universe["received"]} 个不同代码，服务报告总数 {total if total is not None else "未提供"}；仅用于能力核验。'})
            raw['valuation'] = await mcp.call('get_bond_market_data', {'question':f'查询上海市政府地方政府债券在{target}当天的收盘价到期收益率（%）、剩余期限（年），日频。逐券返回债券代码、原始指标名称和单位，缺失保留空值。'})
            valuation = inspect_valuation(tables_from(raw['valuation']), target)
            report['valuation'] = valuation
            report['checks'].append({'name':'收盘价到期收益率与查询日期归档','status':'待全量核验' if valuation['verifiedDateYields'] else '未通过',
                'detail':f'上海样本返回 {valuation["received"]} 个代码，非空收益率 {valuation["nonNullYields"]} 个，可按查询日期归档的有效收盘价到期收益率 {valuation["verifiedDateYields"]} 个。'})
            report['checks'].append({'name':'37 地区、跨市场身份与历史口径','status':'未验证',
                'detail':'当前为上海能力样本；尚未核实全国完整覆盖、稳定去重身份及各历史日剩余期限。'})
        problems = [c['detail'] for c in report['checks'] if c['status']=='未通过']
        report['message'] = 'Wind 已连接，但完整行情核验未通过。' + (' '.join(problems) if problems else '全国完整数据与字段映射尚未核验。')
    except WindError as exc:
        report['message'] = str(exc)
    # Raw Wind replies are local evidence, separate from published market snapshots.
    report['checkedAt'] = store.now()
    report['traceSessionId']=recorder.id
    recorder.artifact('verification-report',report)
    recorder.artifact('verification-responses',{'targetDate':target,'responses':raw})
    save_report(report)
    return report
