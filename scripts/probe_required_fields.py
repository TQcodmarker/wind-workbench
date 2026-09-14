"""Bounded, resumable probe: at most three paid data calls; no automatic retries."""
import asyncio
import json
from pathlib import Path
from backend import storage
from backend.credentials import read_wind_key
from backend.lineage import Recorder, requests, request_detail
from backend.wind_mcp import WindMCP, WindError
from backend.wind_verification import tables_from
from scripts.probe_user_columns import CODES

OUTPUT=Path('runtime/required-fields-probe.json')

async def main():
    if OUTPUT.exists():
        print('已存在本轮核验记录；为避免重复收费，不再次调用。'+OUTPUT.read_text(encoding='utf-8'),flush=True)
        return
    storage.initialize(seed=False)
    target='2026-09-10'
    rec=Recorder('必需字段批量核验（最多3次数据调用）',target,config={
        'codes':CODES,'maxDataCalls':3,'automaticRetries':0,'scope':'当前发行规模加权分析；不查询额外税务/曲线指标'})
    report={'sessionId':rec.id,'date':target,'codes':CODES,'maxDataCalls':3,'status':'running','results':[]}
    def save():OUTPUT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    codes='、'.join(CODES)
    shared='逐券返回Wind代码及以下原始字段，标明单位；缺失或不支持保留空值，不推算、不用近似字段替代。债券：'+codes+'。'
    queries=[
        ('档案、发行规模与条款','get_bond_basicinfo',shared+
         '查询证券简称、主证券代码（用于跨市场去重）、发行日期、一般债/专项债分类、原始发行规模（亿元，含义为首次发行规模，不是余额）、币种、是否含提前偿还条款、是否含发行人赎回条款。条款须有明确的有/无/未知；不能将特殊条款空白解释为无。'),
        ('发行主体','get_bond_issuer_info',shared+'查询发行主体完整名称和所属省份/直辖市。'),
        ('历史估值、期限与久期','get_bond_market_data',shared+
         '查询2026-09-10当日（日频）的中债估值收益率（%）、来源实际估值日期、该日剩余到期期限（年）、中债修正久期（年）。剩余期限不用下一行权期限代替，实际估值日期不使用请求日期填充。'),
    ]
    print('SESSION '+rec.id,flush=True)
    try:
        async with WindMCP(read_wind_key(),recorder=rec) as mcp:
            rec.artifact('tool-catalog',await mcp.list_tools())
            for stage,tool,q in queries:
                rec.stage=stage
                entry={'stage':stage,'tool':tool}
                try:
                    result=await mcp.call(tool,{'question':q})
                    entry['tables']=tables_from(result)
                    if not entry['tables']:entry['content']=result.get('content',[])
                except WindError as exc:entry['error']=str(exc)
                entry['requestId']=mcp.last_request_id
                report['results'].append(entry);save()
                print(json.dumps(entry,ensure_ascii=False),flush=True)
                raw=request_detail(mcp.last_request_id) if mcp.last_request_id else None
                if raw and any(s in json.dumps(raw['response'],ensure_ascii=False) for s in ('余额不足','请先充值')):
                    report['stoppedBecause']='来源额度不足';break
    finally:
        report['dataCalls']=sum(r['method']=='tools/call' for r in requests(rec.id))
        report['status']='finished'
        save();rec.artifact('field-probe-report',report)
        print('DATA_CALLS '+str(report['dataCalls']),flush=True)

if __name__=='__main__':asyncio.run(main())
