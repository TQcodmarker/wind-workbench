"""User-requested static clause probe: one bond, one data call, no retries."""
import asyncio
import json
import sys
from pathlib import Path

from backend import storage
from backend.credentials import read_wind_key
from backend.lineage import Recorder, export_session, request_detail, requests
from backend.wind_mcp import WindMCP, WindError
from backend.wind_verification import tables_from

CODE = '809336.IB'
TOOL = 'get_bond_basicinfo'
OUTPUT = Path('runtime/bond-clauses-focused-809336-20260912.json')
EVIDENCE = OUTPUT.with_name(OUTPUT.stem + '-evidence.json')
QUESTION = (
    '查询809336.IB（26河北23）的本金偿还方式、提前还本安排和发行人赎回条款。'
    '分别返回是否提前偿还本金、是否具有发行人赎回权；如有，返回对应条款及日期。'
    '请保留债券代码和原始指标名称，按债券基本档案原始记录返回，未提供的项目保留空值。'
)


class OneCallMCP(WindMCP):
    async def request(self, method, *args, **kwargs):
        history = requests(self.recorder.id)
        if len(history) >= 6:
            raise WindError('已达到本次6次协议请求上限')
        if method == 'tools/call' and any(x['method'] == method for x in history):
            raise WindError('本次仅允许1次数据请求，不重复调用')
        return await super().request(method, *args, **kwargs)


async def main():
    if OUTPUT.exists():
        previous = json.loads(OUTPUT.read_text(encoding='utf-8'))
        print(json.dumps({'existing': True, 'sessionId': previous.get('sessionId'),
                          'dataCalls': previous.get('dataCalls'), 'status': previous.get('status')}, ensure_ascii=False), flush=True)
        return
    try:
        configured_key = read_wind_key()
    except Exception as exc:
        print('PRECHECK_FAILED ' + type(exc).__name__, flush=True)
        return
    storage.initialize(seed=False)
    rec = Recorder('单券条款专项查询：基本信息工具（仅1次数据调用）',
                   config={'codes': [CODE], 'maxDataCalls': 1, 'maxProtocolRequests': 6,
                           'retries': 0, 'publishSnapshot': False, 'scope': 'static_bond_profile'})
    report = {'sessionId': rec.id, 'code': CODE, 'scope': '债券基本档案，未指定历史日期',
              'startedAt': storage.now(), 'status': 'running', 'tool': TOOL, 'question': QUESTION}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    def save():
        history = requests(rec.id)
        report.update(protocolRequests=len(history),
                      dataCalls=sum(x['method'] == 'tools/call' for x in history))
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    print('SESSION ' + rec.id, flush=True)
    try:
        async with OneCallMCP(configured_key, recorder=rec) as mcp:
            rec.artifact('tool-catalog', await mcp.list_tools())
            rec.stage = '提前还本及发行人赎回条款'
            print('QUERY ' + QUESTION, flush=True)
            try:
                result = await mcp.call(TOOL, {'question': QUESTION})
                safe_result = json.loads(mcp.clean(json.dumps(result, ensure_ascii=False)))
                report['result'] = safe_result
                report['tables'] = tables_from(safe_result)
            finally:
                report['requestId'] = mcp.last_request_id
                detail = request_detail(mcp.last_request_id) if mcp.last_request_id else None
                report['httpStatus'] = detail['http_status'] if detail else None
                save()
    except WindError as exc:
        report['stopReason'] = str(exc).replace(configured_key, '[REDACTED]') if configured_key else str(exc)
    except Exception as exc:
        report['stopReason'] = '本地处理异常：' + type(exc).__name__
    finally:
        report.update(status='stopped' if report.get('stopReason') else 'finished', finishedAt=storage.now())
        save()
        rec.artifact('bond-clauses-focused-probe', report)
        EVIDENCE.write_text(json.dumps(export_session(rec.id), ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
