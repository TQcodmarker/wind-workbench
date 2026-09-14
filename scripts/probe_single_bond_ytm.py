"""One-bond, four-call completeness probe with durable evidence and no retries."""
import argparse
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
TARGET = '2026-09-11'
OUTPUT = Path('runtime/single-bond-ytm-809336-20260911.json')
EVIDENCE = OUTPUT.with_name(OUTPUT.stem + '-evidence.json')
QUERIES = [
    ('1.指定日收盘到期收益率及行情', 'get_bond_market_data',
     '仅查询809336.IB（26河北23）在2026-09-11当日的收盘价到期收益率（%）、收盘价、'
     '剩余到期期限（年）、债券余额（亿元）、修正久期（年）。'
     '收益率必须是以当日收盘价为基础的到期收益率，明确返回收益率类型、价格口径、来源实际行情日期。'
     '请保留Wind原始字段名称、单位、证券代码和每项数据实际日期，明确久期的价格来源。'
     '不使用中债估值收益率、票面利率、下一行权收益率或其他日期值替代；'
     '不能仅把请求日期写成实际行情日期；无法获取的字段保留空值并说明未支持或缺失。'),
    ('2.债券身份与发行条款', 'get_bond_basicinfo',
     '仅查询809336.IB（26河北23）的Wind代码、证券简称、主证券代码/跨市场代码、'
     '发行主体名称、一般债或专项债分类、首次发行日期、到期日期、原始首次发行规模（亿元）、'
     '币种、票面利率（%）、是否提前偿还、是否含发行人赎回权、特殊条款原文。'
     '可变字段取2026-09-11时点。原始首次发行规模不要用续发行累计总额或债券余额替代。'
     '两项条款标志分别明确返回是/否，未知保留空值，不能把条款原文缺失推断成否。'
     '返回原始指标名、单位和数据来源；无法支持的字段明确说明。'),
    ('3.发行主体与地区', 'get_bond_issuer_info',
     '仅查询809336.IB（26河北23）对应的发行主体完整名称及所属省级行政区，'
     '保留Wind证券代码和原始指标名称，以2026-09-11时点为准，无法获取保留空值。'),
    ('4.原表曲线与久期输入', 'get_bond_market_data',
     '仅为809336.IB（26河北23）核验2026-09-11的两个原Excel输入：'
     '一是中债修正久期b_anal_modidura_cnbd(代码,日期,1)，单位年；'
     '二是以该券当日实际剩余到期期限（年）为期限点的中债曲线4202收益率，'
     '对应b_calc_curve_chinabond("4202",日期,实际剩余期限)。'
     '曲线数据需返回曲线编号、曲线名称、实际曲线日期、期限点、取点/插值说明及收益率（%）。'
     '这是对同一只券的输入核验，不查询其他券；无法支持的指标明确说明、保留空值，'
     '不要用该券到期收益率、其他曲线、整数期限点、剩余期限或其他久期冒充所请求指标。'),
]


class BoundedMCP(WindMCP):
    async def request(self, method, *args, **kwargs):
        history = requests(self.recorder.id)
        if len(history) >= 10:
            raise WindError('达到10次协议请求上限')
        if method == 'tools/call' and sum(x['method'] == method for x in history) >= 4:
            raise WindError('达到4次数据请求上限')
        return await super().request(method, *args, **kwargs)


async def main(resume_preflight=False):
    previous = None
    if OUTPUT.exists():
        previous = json.loads(OUTPUT.read_text(encoding='utf-8'))
        history = requests(previous['sessionId']) if previous.get('sessionId') else []
        paid_calls = sum(x['method'] == 'tools/call' for x in history)
        if not resume_preflight or paid_calls or previous.get('tests'):
            print(json.dumps({'existing': True, 'sessionId': previous.get('sessionId'),
                              'dataCalls': paid_calls, 'status': previous.get('status')}, ensure_ascii=False), flush=True)
            return
    try:
        configured_key = read_wind_key()
    except Exception as exc:
        print('PRECHECK_FAILED ' + type(exc).__name__, flush=True)
        return
    storage.initialize(seed=False)
    rec = Recorder('单券完整性测试：收盘价到期收益率（最多4次数据查询）', TARGET,
                   config={'codes': [CODE], 'maxDataCalls': 4, 'maxProtocolRequests': 10,
                           'retries': 0, 'publishSnapshot': False})
    report = {'sessionId': rec.id, 'code': CODE, 'targetDate': TARGET,
              'startedAt': storage.now(), 'status': 'running', 'tests': []}
    if previous:
        report['previousPreflight'] = previous
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    def save():
        history = requests(rec.id)
        report.update(protocolRequests=len(history),
                      dataCalls=sum(x['method'] == 'tools/call' for x in history))
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    print('SESSION ' + rec.id, flush=True)
    try:
        async with BoundedMCP(configured_key, recorder=rec) as mcp:
            rec.artifact('tool-catalog', await mcp.list_tools())
            for stage, tool, question in QUERIES:
                rec.stage = stage
                entry = {'stage': stage, 'tool': tool, 'question': question}
                print('QUERY ' + stage, flush=True)
                try:
                    result = await mcp.call(tool, {'question': question})
                    safe = json.loads(mcp.clean(json.dumps(result, ensure_ascii=False)))
                    entry['result'] = safe
                    entry['tables'] = tables_from(safe)
                except WindError as exc:
                    entry['error'] = mcp.clean(str(exc))
                entry['requestId'] = mcp.last_request_id
                detail = request_detail(mcp.last_request_id) if mcp.last_request_id else None
                entry['httpStatus'] = detail['http_status'] if detail else None
                report['tests'].append(entry)
                save()
                print(json.dumps({k: v for k, v in entry.items() if k != 'result'}, ensure_ascii=False), flush=True)
                raw = json.dumps(detail.get('response'), ensure_ascii=False) if detail else ''
                if entry['httpStatus'] in (401, 403, 429) or any(s in raw for s in ('余额不足', '请先充值')):
                    raise WindError('鉴权、额度或限流阻断，停止后续查询')
                if entry.get('error') and entry['httpStatus'] is None:
                    raise WindError('网络或连接中断，停止后续查询，不自动重试')
    except WindError as exc:
        report['stopReason'] = str(exc).replace(configured_key, '[REDACTED]') if configured_key else str(exc)
    except Exception as exc:
        report['stopReason'] = '本地处理异常：' + type(exc).__name__
    finally:
        report.update(status='stopped' if report.get('stopReason') else 'finished', finishedAt=storage.now())
        save()
        rec.artifact('single-bond-completeness-probe', report)
        EVIDENCE.write_text(json.dumps(export_session(rec.id), ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: report.get(k) for k in ('sessionId', 'status', 'protocolRequests', 'dataCalls', 'stopReason')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume-preflight', action='store_true', help='Only resume when zero data calls were made.')
    asyncio.run(main(parser.parse_args().resume_preflight))
