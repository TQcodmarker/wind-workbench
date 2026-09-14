"""Read-only capability checks against the discovered Wind tools; never print credentials."""
import asyncio
import json
from backend import storage as store
from backend.credentials import read_wind_key
from backend.wind_mcp import WindMCP, WindError


QUERIES = [
    ('universe', 'get_bond_basicinfo', '查询截至2026-09-10上海市政府发行且尚未到期的全部地方政府债券（一般债和专项债），返回完整债券代码名单、总条数、是否截断或分页，以及各券发行地区、债券类型、发行日期、发行规模及单位、起息日、到期日、提前偿还与赎回条款、跨市场代码。需要可核对的原始表格数据，不要抽样或估算；无法提供全量名单请明确说明。'),
    ('valuation', 'get_bond_market_data', '查询上海市政府地方政府债券在2026-09-10当天的中债估值收益率（百分比），日频，并返回债券代码、实际估值日期、剩余期限及单位。需要原始数据，不能用最新交易日或前一日替代；如需要指定债券代码且不支持按地区查询，请明确说明。'),
]


async def check(label, name, question):
    try:
        async with WindMCP(read_wind_key()) as mcp:
            await mcp.list_tools()
            result = await mcp.call(name, {'question': question})
        data = {'tool':name,'question':question,'result':result}
    except WindError as exc:
        data = {'tool':name,'error':str(exc)}
    path = store.DB.parent / f'wind-probe-{label}.json'
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    tables = []
    if 'result' in data:
        for item in data['result'].get('content',[]):
            if item.get('type') == 'text':
                try:
                    payload = json.loads(item['text'])
                    for table in (payload.get('data') or {}).get('data',[]):
                        tables.append({'columns':table.get('columns'), 'rowCount':len(table.get('rows',[])), 'samples':table.get('rows',[])[:3]})
                except (ValueError,AttributeError):
                    tables.append({'text':item['text'][:2000]})
    print(json.dumps({'label':label,'error':data.get('error'),'tables':tables},ensure_ascii=False))


async def main():
    await asyncio.gather(*(check(*query) for query in QUERIES))


if __name__ == '__main__':
    asyncio.run(main())
