"""User-authorized investigation: ten total MCP protocol requests, no retries."""
import asyncio
import json
import re
from pathlib import Path
from backend import storage
from backend.credentials import read_wind_key
from backend.lineage import Recorder,requests,request_detail
from backend.wind_mcp import WindMCP,WindError
from backend.wind_verification import tables_from

OUTPUT=Path('runtime/ten-attempts-evidence.json')

class BudgetMCP(WindMCP):
    async def request(self,*args,**kwargs):
        if len(requests(self.recorder.id))>=10:
            raise WindError('本轮10次MCP协议请求预算已用完，不再调用')
        return await super().request(*args,**kwargs)

async def main():
    if OUTPUT.exists():
        print('本轮已有记录，不重复调用。'+OUTPUT.read_text(encoding='utf-8'),flush=True);return
    storage.initialize(seed=False)
    rec=Recorder('10次协议请求上限：估值与条款对照实验','2026-09-10',config={
        'maxProtocolRequests':10,'maxDataCalls':7,'retries':0,'sampleCodes':['809336.IB','809335.IB'],
        'controlCode':'038006.SH','controlSource':'https://valuation.chinabond.com.cn/cbweb-mn/val/val_query_list?locale=zh_CN'})
    report={'sessionId':rec.id,'status':'running','tests':[]}
    def save():OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save();print('SESSION '+rec.id,flush=True)
    async def ask(mcp,hypothesis,tool,q):
        rec.stage=hypothesis
        entry={'hypothesis':hypothesis,'tool':tool,'question':q}
        try:
            result=await mcp.call(tool,{'question':q})
            entry['tables']=tables_from(result)
            if not entry['tables']:entry['content']=result.get('content',[])
        except WindError as exc:entry['error']=str(exc)
        entry['requestId']=mcp.last_request_id
        report['tests'].append(entry);save()
        print(json.dumps(entry,ensure_ascii=False),flush=True)
        d=request_detail(entry['requestId']) if entry['requestId'] else None
        if d and (d['http_status'] in (401,403,429) or any(x in json.dumps(d['response'],ensure_ascii=False) for x in ('余额不足','请先充值'))):
            raise WindError('发现鉴权、额度或限流阻断，停止新增请求')
        return entry
    try:
        async with BudgetMCP(read_wind_key(),recorder=rec) as mcp:
            rec.artifact('tool-catalog',await mcp.list_tools())
            identity=await ask(mcp,'1.核对样本身份与跨市场代码','get_bond_basicinfo',
                '查询809336.IB（26河北23）和809335.IB（26湖南15）的Wind代码、证券简称、主证券代码、跨市场代码、银行间代码、上交所代码、深交所代码、起息日期、到期日期、偿还类型。逐券返回来源原始字段，缺失留空，不推断代码。')
            await ask(mcp,'2.公开中债估值对照券与地方债同日比较','get_bond_market_data',
                '查询038006.SH（03三峡债）、809336.IB（26河北23）、809335.IB（26湖南15）在2026-09-10的中债估价收益率、中债估价净价、中债估值日期和修正久期。逐券返回Wind代码、原始值及单位，缺失留空。')
            aliases=[]
            for t in identity.get('tables',[]):
                for row in t['rows']:
                    for v in row:
                        if isinstance(v,str):
                            for code in re.findall(r'\b\d{6,9}\.(?:SH|SZ|IB)\b',v):
                                if code not in ('809336.IB','809335.IB') and code not in aliases:aliases.append(code)
            if aliases:
                await ask(mcp,'3.用来源明确返回的跨市场代码复核','get_bond_market_data',
                    '查询'+ '、'.join(aliases[:4])+'在2026-09-10的中债估价收益率、中债估价净价和实际估值日期。逐券返回Wind代码，保留原始字段及空值。')
            else:
                await ask(mcp,'3.最小单券单指标请求排除宽查询影响','get_bond_market_data',
                    '查询809336.IB在2026-09-10的中债估价收益率（%），返回Wind代码与原始数值。')
            await ask(mcp,'4.历史日期对照，检查是否只缺目标日','get_bond_market_data',
                '查询809336.IB、809335.IB在2026-04-30和2026-06-30两个日期的中债估价收益率（%），逐券逐日返回Wind代码、日期和原始值，缺失留空，不前值填充。')
            await ask(mcp,'5.最新可用估值对照，要求来源实际日期','get_bond_market_data',
                '查询809336.IB、809335.IB最新可用的中债估价收益率及对应实际估值日期，不限定2026-09-10。逐券返回Wind代码、数据日期、原始收益率，缺失留空。')
            await ask(mcp,'6.直接查询提前还本及赎回字段语义','get_bond_basicinfo',
                '查询809336.IB、809335.IB的本金偿还方式、提前还本标志、发行人赎回权标志，以及提前还本和赎回的具体条款说明。返回原始字段名和取值；没有指标或无法查询请明确说明，特殊条款空白不代表否，不根据一般债常识推断。')
            await ask(mcp,'7.其他估值来源对照，检查是否仅中债缺失','get_bond_market_data',
                '查询809336.IB、809335.IB在2026-09-10的中证估值收益率、Wind估值收益率和成交到期收益率。按券分别返回Wind代码、原始指标名、原始值、单位和数据日期，缺失留空；三种来源不能互相替代。')
    except WindError as exc:report['stopReason']=str(exc)
    finally:
        calls=requests(rec.id)
        report.update(status='finished',protocolRequests=len(calls),dataCalls=sum(r['method']=='tools/call' for r in calls))
        save();rec.artifact('ten-attempts-report',report)
        print('COUNTS '+json.dumps({'protocolRequests':report['protocolRequests'],'dataCalls':report['dataCalls']}),flush=True)

if __name__=='__main__':asyncio.run(main())
