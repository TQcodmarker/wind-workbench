"""Read-only checks for the user's bond table; all responses are persisted."""
import asyncio
import json
from backend import storage
from backend.credentials import read_wind_key
from backend.lineage import Recorder
from backend.wind_mcp import WindMCP, WindError
from backend.wind_verification import tables_from

CODES=['809336.IB','809335.IB','809322.IB','809321.IB','809320.IB','809319.IB',
       '809312.IB','809311.IB','809310.IB','809308.IB','809306.IB','809304.IB']

async def main():
    storage.initialize(seed=False)
    rec=Recorder('用户表格字段可用性核验','2026-09-10',config={'codes':CODES,'sampleCodes':CODES[:2]})
    print('SESSION '+rec.id,flush=True)
    queries=[
      ('档案与代码核对','get_bond_basicinfo','逐券查询以下债券的证券简称、债券类型、票面利率（%）、起息日期、到期日期和主证券代码：'+ '、'.join(CODES)),
      ('发行地区','get_bond_issuer_info','查询809336.IB、809335.IB的发行主体名称和所属省份，返回Wind代码。'),
      ('余额','get_bond_basicinfo','查询809336.IB、809335.IB在2026-09-10的债券余额（亿元），返回Wind代码，不用发行规模替代余额。'),
      ('历史期限与久期','get_bond_market_data','查询809336.IB、809335.IB在2026-09-10的剩余到期期限（年）及中债修正久期（年），按日，返回Wind代码和数据日期。'),
      ('中债估值','get_bond_market_data','查询809336.IB、809335.IB从2026-09-09至2026-09-10的中债估值收益率（%），频率为日，返回Wind代码及实际估值日期。'),
      ('地方债曲线','get_bond_market_data','查询809336.IB、809335.IB在2026-09-10对应剩余期限的地方政府债收益率曲线值（%）。请返回曲线名称、曲线代码、期限、日期和原始值；无法取得请明确缺失。'),
      ('免税收益与曲线偏离','get_bond_market_data','查询809336.IB、809335.IB在2026-09-10的免税收益率、非免税曲线偏离和免税曲线偏离。仅返回可查询的原始指标，保留指标全称、单位与数据日期，不自行假设税率或计算公式；不支持请明确说明。'),
    ]
    async with WindMCP(read_wind_key(),recorder=rec) as mcp:
        rec.artifact('tool-catalog',await mcp.list_tools())
        for stage,tool,q in queries:
            rec.stage=stage
            try:
                result=await mcp.call(tool,{'question':q})
                tables=tables_from(result)
                preview=[{'columns':t['columns'],'count':len(t['rows']),'rows':t['rows'][:12]} for t in tables]
                print(json.dumps({'stage':stage,'requestId':mcp.last_request_id,'tables':preview,
                    'text':[] if tables else result.get('content',[])},ensure_ascii=False),flush=True)
            except WindError as exc:
                print(json.dumps({'stage':stage,'error':str(exc)},ensure_ascii=False),flush=True)
    rec.artifact('probe-description',{'targetDate':'2026-09-10','codes':CODES,'purpose':'用户表格字段可用性核验；不发布行情'})

if __name__=='__main__':asyncio.run(main())
