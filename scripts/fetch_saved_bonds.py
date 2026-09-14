"""Execute an explicit, bounded enrichment plan and retain every MCP reply."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from backend import storage as store
from backend.available_data import read_available
from backend.credentials import read_wind_key
from backend.lineage import Recorder, requests, request_detail
from backend.wind_mcp import WindMCP, WindError
from backend.wind_verification import tables_from


async def execute(path):
    plan = json.loads(path.read_text(encoding='utf-8'))
    target = plan['targetDate']
    from datetime import date, datetime, timezone, timedelta
    if date.fromisoformat(target) >= datetime.now(timezone(timedelta(hours=8))).date():
        raise ValueError('只能查询已结束日期')
    if len(plan['queries']) > plan['maxDataCalls'] or plan['maxDataCalls'] > 40:
        raise ValueError('查询数量超过本批次明确上限')
    if any(target not in q['question'] or q['tool'] not in ('get_bond_basicinfo', 'get_bond_market_data', 'get_bond_issuer_info') for q in plan['queries']):
        raise ValueError('查询工具或日期不符合计划')
    output = path.with_name(path.stem + '-result.json')
    if output.exists():
        previous = json.loads(output.read_text(encoding='utf-8'))
        print(json.dumps({'existingReport': str(output), 'sessionId': previous.get('sessionId'), 'status': previous.get('status')}, ensure_ascii=False), flush=True)
        return
    key = read_wind_key()
    if not key:
        raise WindError('本机未配置 Wind Key')
    before = read_available(target)
    rec = Recorder('已有债券补齐：'+plan['label'], target, config=plan)
    report = dict(sessionId=rec.id, targetDate=target, status='running', before=before['counts'], startedAt=store.now(), queries=[])

    def save():
        history = requests(rec.id)
        report.update(protocolRequests=len(history), dataCalls=sum(r['method']=='tools/call' for r in history))
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    class BoundedMCP(WindMCP):
        async def request(self, method, *args, **kwargs):
            history = requests(rec.id)
            if len(history) >= plan['maxDataCalls'] + 5:
                raise WindError('已达到本批次协议请求上限')
            if method == 'tools/call' and sum(r['method']==method for r in history) >= plan['maxDataCalls']:
                raise WindError('已达到本批次数据调用上限')
            return await super().request(method, *args, **kwargs)

    save()
    print(json.dumps({'sessionId': rec.id, 'targetDate': target, 'plannedDataCalls': len(plan['queries'])}, ensure_ascii=False), flush=True)
    try:
        async with BoundedMCP(key, recorder=rec) as mcp:
            rec.artifact('tool-catalog', await mcp.list_tools())
            for query in plan['queries']:
                rec.stage = query['label']
                print('QUERY '+query['label'], flush=True)
                entry = {'label': query['label'], 'tool': query['tool']}
                try:
                    result = await mcp.call(query['tool'], {'question': query['question']})
                    tables = tables_from(result)
                    entry.update(tables=[{'columns': t['columns'], 'rows': len(t['rows'])} for t in tables], receivedCodes=sorted({row[[c['name'] for c in t['columns']].index('Wind代码')] for t in tables if 'Wind代码' in [c['name'] for c in t['columns']] for row in t['rows'] if isinstance(row[[c['name'] for c in t['columns']].index('Wind代码')],str)}))
                except WindError as exc:
                    entry['error'] = mcp.clean(str(exc))
                entry['requestId'] = mcp.last_request_id
                detail = request_detail(mcp.last_request_id) if mcp.last_request_id else None
                entry['httpStatus'] = detail['http_status'] if detail else None
                report['queries'].append(entry)
                save()
                print(json.dumps(entry, ensure_ascii=False), flush=True)
                raw = json.dumps(detail.get('response'), ensure_ascii=False) if detail else ''
                if entry['httpStatus'] in (401,403,429) or any(word in raw for word in ('余额不足','请先充值')):
                    raise WindError('鉴权、额度或限流阻断，停止后续查询')
                if entry.get('error') and entry['httpStatus'] is None:
                    raise WindError('网络中断，停止本批次，不自动重试')
                # A broad field-group query with no rows should not be repeated
                # across dozens of codes before inspecting the saved response.
                if not entry.get('receivedCodes'):
                    report['stopReason'] = '本次未返回债券行，停止本批次并检查原始响应'
                    break
    except WindError as exc:
        report['stopReason'] = str(exc).replace(key, '[REDACTED]')
    except Exception as exc:
        report['stopReason'] = '本地处理异常：'+type(exc).__name__
    finally:
        report.update(status='stopped' if report.get('stopReason') else 'finished', finishedAt=store.now(), after=read_available(target)['counts'])
        save()
        rec.artifact('saved-bond-enrichment', report)
        print(json.dumps({k:report.get(k) for k in ('sessionId','status','dataCalls','before','after','stopReason')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('plan', type=Path)
    asyncio.run(execute(parser.parse_args().plan))
