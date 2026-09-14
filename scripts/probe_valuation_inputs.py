"""One batch call to check direct yield and price inputs; never automatically retry."""
import asyncio
import json
from pathlib import Path
from backend import storage
from backend.credentials import read_wind_key
from backend.lineage import Recorder,requests
from backend.wind_mcp import WindMCP,WindError
from backend.wind_verification import tables_from
from scripts.probe_user_columns import CODES

OUTPUT=Path('runtime/valuation-inputs-probe.json')

async def main():
    if OUTPUT.exists():
        print(OUTPUT.read_text(encoding='utf-8'));return
    storage.initialize(seed=False)
    rec=Recorder('估值替代输入核验（仅1次批量查询）','2026-09-10',config={'codes':CODES,'maxDataCalls':1,'retries':0})
    report={'sessionId':rec.id,'status':'running','codes':CODES,'targetDate':'2026-09-10'}
    def save():OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    print('SESSION '+rec.id,flush=True)
    try:
        async with WindMCP(read_wind_key(),recorder=rec) as mcp:
            rec.artifact('tool-catalog',await mcp.list_tools())
            rec.stage='中债估价收益率与估价价格批量核验'
            q='查询以下债券在2026-09-10的中债估价数据，日频，逐券返回Wind代码：'+ '、'.join(CODES)+'。指标：中债估价收益率（%）、中债估价净价（元/百元面值）、中债日间估价全价（元/百元面值）、中债应计利息、来源实际估值日期、中债估价修正久期。保留原始列名、单位和空值，不用票面利率、交易价格或其他机构估值替代，不自行计算或填充。'
            try:
                result=await mcp.call('get_bond_market_data',{'question':q})
                report['tables']=tables_from(result)
                if not report['tables']:report['content']=result.get('content',[])
            except WindError as exc:report['error']=str(exc)
            report['requestId']=mcp.last_request_id
    finally:
        report['status']='finished'
        report['dataCalls']=sum(r['method']=='tools/call' for r in requests(rec.id))
        save();rec.artifact('valuation-inputs-probe',report)
        print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':asyncio.run(main())
