"""Bounded user-requested market-data test; three data calls, no retries or publication."""
import asyncio
import json
from pathlib import Path

from backend import storage
from backend.credentials import read_wind_key
from backend.lineage import Recorder, export_session, request_detail, requests
from backend.wind_mcp import WindMCP, WindError
from backend.wind_verification import tables_from

OUTPUT = Path('runtime/market-fields-test-20260912.json')
EVIDENCE = Path('runtime/market-fields-test-20260912-evidence.json')
TARGET = '2026-09-10'


class BoundedMCP(WindMCP):
    async def request(self, method, *args, **kwargs):
        history = requests(self.recorder.id)
        if len(history) >= 8:
            raise WindError('本轮协议请求上限已达到，停止')
        if method == 'tools/call' and sum(x['method'] == method for x in history) >= 3:
            raise WindError('本轮3次数据查询上限已达到，停止')
        return await super().request(method, *args, **kwargs)


async def main():
    if OUTPUT.exists():
        previous = json.loads(OUTPUT.read_text(encoding='utf-8'))
        print(json.dumps({'existing': True, 'sessionId': previous.get('sessionId'),
                          'dataCalls': previous.get('dataCalls'), 'status': previous.get('status')}, ensure_ascii=False), flush=True)
        return
    # Load the configured credential in memory through the existing application helper.
    # No credential or authorization header is printed or exported.
    try:
        configured_key = read_wind_key()
    except Exception as exc:
        print('PRECHECK_FAILED ' + type(exc).__name__, flush=True)
        return
    storage.initialize(seed=False)
    rec = Recorder('行情工具实测：中债估值与其他字段（最多3次数据查询）', TARGET,
                   config={'maxDataCalls': 3, 'maxProtocolRequests': 8, 'retries': 0,
                           'codes': ['809336.IB', '809335.IB'], 'controlCode': '809336.IB',
                           'controlDate': '2026-06-17'})
    report = {'sessionId': rec.id, 'targetDate': TARGET, 'startedAt': storage.now(),
              'status': 'running', 'tests': []}

    def save():
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    print('SESSION ' + rec.id, flush=True)
    queries = [
        ('1.两只地方债的中债估价数据',
         '查询809336.IB（26河北23）、809335.IB（26湖南15）在2026-09-10当日的中债估价收益率（%）、'
         '中债估价净价、中债估价全价、中债应计利息、中债修正久期，以及来源实际估值日期。日频。'
         '逐券返回Wind代码、原始指标名称、单位和数据值；保留缺失值，不用收盘价收益率、票面利率或其他来源估值替代。'),
        ('2.相同两券同日其他行情字段',
         '查询809336.IB（26河北23）、809335.IB（26湖南15）在2026-09-10当日（日频）的收盘价、'
         '收盘价收益率、成交量、成交金额、剩余到期期限（年）、债券余额（亿元）、修正久期。'
         '逐券返回Wind代码、原始指标名、单位和对应数据日期，缺失保留空值。'
         '不以最新值或下一行权期限替代指定日到期期限，不将无成交填充为有效行情。'),
        ('3.原Excel非空历史日期的单指标对照',
         '查询809336.IB（26河北23）在2026-06-17的中债估值收益率，对应Wind Excel指标'
         'b_anal_yield_cnbd(代码,日期,1)。仅返回该指标、单位、实际估值日期、证券代码与数据来源，'
         '保留原始字段名。无法获取时明确区分空值、未支持字段和查询错误，'
         '不以收盘价收益率、其他市场代码或其他来源替代。'),
    ]
    try:
        async with BoundedMCP(configured_key, recorder=rec) as mcp:
            rec.artifact('tool-catalog', await mcp.list_tools())
            for stage, question in queries:
                rec.stage = stage
                entry = {'stage': stage, 'tool': 'get_bond_market_data', 'question': question}
                try:
                    result = await mcp.call('get_bond_market_data', {'question': question})
                    safe_result = json.loads(mcp.clean(json.dumps(result, ensure_ascii=False)))
                    entry['tables'] = tables_from(safe_result)
                    if not entry['tables']:
                        entry['content'] = safe_result.get('content', [])
                except WindError as exc:
                    entry['error'] = mcp.clean(str(exc))
                entry['requestId'] = mcp.last_request_id
                detail = request_detail(mcp.last_request_id) if mcp.last_request_id else None
                entry['httpStatus'] = detail['http_status'] if detail else None
                report['tests'].append(entry)
                save()
                print(json.dumps(entry, ensure_ascii=False), flush=True)
                raw = json.dumps(detail['response'], ensure_ascii=False) if detail else ''
                if entry['httpStatus'] in (401, 403, 429) or any(s in raw for s in ('余额不足', '请先充值')):
                    raise WindError('鉴权、额度或限流阻断，停止后续查询')
                if entry.get('error') and entry['httpStatus'] is None:
                    raise WindError('网络或连接中断，停止后续查询，不自动重试')
    except WindError as exc:
        report['stopReason'] = str(exc).replace(configured_key, '[REDACTED]') if configured_key else str(exc)
    except Exception as exc:
        report['stopReason'] = '本地处理异常：' + type(exc).__name__
    finally:
        history = requests(rec.id)
        report.update(status='finished', finishedAt=storage.now(), protocolRequests=len(history),
                      dataCalls=sum(x['method'] == 'tools/call' for x in history))
        save()
        rec.artifact('market-fields-test', report)
        EVIDENCE.write_text(json.dumps(export_session(rec.id), ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('sessionId', 'status', 'protocolRequests', 'dataCalls')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
