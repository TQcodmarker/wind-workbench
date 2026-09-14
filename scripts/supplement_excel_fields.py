"""Acquire missing single indicators after the main Excel runner has attempted them.

Each Excel-code/field pair is sent at most once by this checkpoint. Nulls and
missing response rows are terminal outcomes. The independent journal can run
alongside the main acquisition, without publishing snapshots or queueing jobs.
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from backend import storage as store
from backend.available_data import read_available
from backend.credentials import read_wind_key
from backend.excel_acquisition import cache_index, checkpoint_lock, populated, response_index, signature, QUOTA_WORDS
from backend.lineage import Recorder, dump
from backend.wind_mcp import WindError, WindMCP


FIELD_LABELS = {'yieldPct': '收盘价到期收益率（%）', 'duration': '基于净价的收盘价修正久期（年）'}
MAIN_TERMINAL = {'cached', 'complete', 'partial', 'unavailable'}
MAIN_ACTIVE = {'prepared', 'running'}
TOOL = 'get_bond_market_data'


def question(field, codes, target):
    return (f"查询{'、'.join(codes)}在{target}的{FIELD_LABELS[field]}，"
            '逐券返回Wind代码、原始指标名称、单位和值；无数据留空。')


class Supplement:
    def __init__(self, main_checkpoint, target, fields, batch_size, max_data_calls, resume=None,
                 poll_seconds=10, available_reader=read_available, sleeper=asyncio.sleep, emit=print):
        if date.fromisoformat(target) >= datetime.now(timezone(timedelta(hours=8))).date():
            raise ValueError('只能补取已结束日期')
        if (not fields or len(fields) != len(set(fields)) or not set(fields) <= set(FIELD_LABELS)
                or not 1 <= batch_size <= 100 or not 0 <= max_data_calls <= 10000
                or not 0 < poll_seconds <= 10):
            raise ValueError('字段、批量大小、累计调用上限或轮询间隔无效')
        self.main_path = Path(main_checkpoint).resolve()
        self.target, self.available_reader, self.sleeper, self.emit = target, available_reader, sleeper, emit
        self.poll_seconds = poll_seconds
        main = json.loads(self.main_path.read_text(encoding='utf-8-sig'))
        if main.get('scope') != 'excel_universe' or main.get('targetDate') != target:
            raise ValueError('主任务范围或日期不匹配')
        self.path = Path(resume).resolve() if resume else self.main_path.with_name(
            self.main_path.stem+'-supplement-'+'-'.join(fields)+'.json')
        self.initial_hash = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
        if resume:
            self.state = json.loads(self.path.read_text(encoding='utf-8-sig'))
            if (self.state.get('scope') != 'excel_field_supplement' or self.state.get('targetDate') != target
                    or self.state.get('universeHash') != main.get('universeHash')
                    or self.state.get('fields') != fields
                    or self.state.get('sourceHash') != main.get('sourceHash')
                    or Path(self.state.get('mainCheckpoint', '')).resolve() != self.main_path):
                raise ValueError('补字段恢复文件的主名单、日期或字段不匹配')
            if max_data_calls < self.state['dataCalls']:
                raise ValueError('累计调用上限小于已执行次数')
            self.state['maxDataCalls'] = max_data_calls
        else:
            if self.path.exists():
                raise ValueError('已有补字段 checkpoint；请使用 --resume 避免重复付费')
            self.state = dict(version=1, scope='excel_field_supplement', targetDate=target,
                fields=fields, mainCheckpoint=str(self.main_path), universeHash=main.get('universeHash'),
                sourceHash=main.get('sourceHash'), sourcePath=main.get('sourcePath'),
                batchSize=batch_size, maxDataCalls=max_data_calls, dataCalls=0, protocolRequests=0,
                status='prepared', createdAt=store.now(), sessionId=None, pending=None,
                manifest={}, attempts=[], skippedMaturedCodes=[], publishedSnapshot=False)

    def read_main(self):
        main = json.loads(self.main_path.read_text(encoding='utf-8-sig'))
        if (main.get('scope') != 'excel_universe' or main.get('targetDate') != self.target
                or main.get('universeHash') != self.state['universeHash']
                or main.get('sourceHash') != self.state['sourceHash']):
            raise ValueError('执行过程中主名单或日期发生变化，已停止补取')
        return main

    def progress(self):
        entries = [entry for group in self.state['manifest'].values() for entry in group.values()]
        return dict(attemptedPairs=sum(entry.get('requestId') is not None for entry in entries),
                    receivedValues=sum(entry['status'] == 'complete' for entry in entries),
                    nullValues=sum(entry['status'] == 'null' for entry in entries),
                    absentRows=sum(entry['status'] == 'unavailable' for entry in entries),
                    uncertainPairs=sum(entry['status'] == 'uncertain' for entry in entries),
                    reservedPairs=sum(entry['status'] == 'reserved' for entry in entries))

    def save(self):
        self.state.update(updatedAt=store.now(), progress=self.progress())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix+'.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(self.state, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def counts(self):
        if not self.state['sessionId']:
            return
        with store.connection() as db:
            row = db.execute("SELECT COUNT(*) AS total,SUM(CASE WHEN method='tools/call' THEN 1 ELSE 0 END) AS data "
                             'FROM source_requests WHERE session_id=?', (self.state['sessionId'],)).fetchone()
        self.state.update(dataCalls=row['data'] or 0, protocolRequests=row['total'])

    def stop_pending(self, detail, reason):
        task = self.state['pending']
        for code in task['codes']:
            self.state['manifest'][code][task['field']].update(status='uncertain', requestId=detail['id'])
        self.state.update(status='stopped', stopReason=reason)

    def reconcile(self):
        self.counts()
        task = self.state.get('pending')
        if not task or not self.state['sessionId']:
            return True
        with store.connection() as db:
            rows = db.execute("SELECT id,status,http_status,response FROM source_requests WHERE session_id=? "
                "AND method='tools/call' AND tool=? AND json_extract(request,'$.params.arguments.question')=? ORDER BY rowid",
                (self.state['sessionId'], TOOL, task['question'])).fetchall()
        if not rows:
            return True  # Journal committed before Recorder.begin: safe to send.
        detail = dict(rows[-1])
        detail['response'] = json.loads(detail['response']) if detail['response'] else None
        result = detail['response'].get('result', {}) if isinstance(detail['response'], dict) else {}
        if (detail['status'] != 'succeeded' or detail['http_status'] in (401, 403, 429)
                or not detail['response'] or result.get('isError')
                or any(word in dump(detail['response']).lower() for word in QUOTA_WORDS)):
            self.stop_pending(detail, '请求失败或结果不确定，已保存证据；不会自动重发可能已计费的请求')
            self.save()
            return False
        try:
            returned = response_index(detail, self.target)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, WindError):
            self.stop_pending(detail, '已收到响应但解析失败；原文已保存，不重复付费')
            self.save()
            return False
        for code in task['codes']:
            row = returned.get(code)
            value = (row or {}).get(task['field'])
            self.state['manifest'][code][task['field']].update(
                status='unavailable' if row is None else 'complete' if populated(value, task['field']) else 'null',
                requestId=detail['id'], returnedValue=value, finishedAt=store.now())
        self.state['attempts'].append(dict(signature=task['signature'], field=task['field'],
            requestId=detail['id'], codes=task['codes'], requested=len(task['codes']),
            returnedRows=sum(code in returned for code in task['codes'])))
        self.state['pending'] = None
        self.save()
        return True

    def candidates(self, main):
        """Restrict requests to rows whose main market group has already ended."""
        choices = {field: [] for field in self.state['fields']}
        # Scope by the main manifest first, then read the persisted values. Never
        # preempt a main query which is queued or in flight for this code.
        matured = set(self.state.get('skippedMaturedCodes', []))
        terminal = [code for code in main['codes']
                    if main['manifest'].get(code, {}).get('market', {}).get('status') in MAIN_TERMINAL
                    and code not in matured
                    and any(field not in self.state['manifest'].get(code, {}) for field in self.state['fields'])]
        if not terminal:
            return choices
        available = cache_index(self.available_reader(self.target))
        for code in terminal:
            bond = available.get(code, {})
            try:
                if date.fromisoformat(bond.get('maturityDate', '')) <= date.fromisoformat(self.target):
                    matured.add(code)
                    continue
            except (TypeError, ValueError):
                pass
            for field in self.state['fields']:
                if field not in self.state['manifest'].get(code, {}) and not populated(bond.get(field), field):
                    choices[field].append(code)
        self.state['skippedMaturedCodes'] = sorted(matured)
        return choices

    def reserve(self, field, codes):
        text = question(field, codes, self.target)
        for code in codes:
            if field in self.state['manifest'].get(code, {}):
                raise ValueError('同一代码和指标已经提交，禁止重复付费')
            self.state['manifest'].setdefault(code, {})[field] = dict(status='reserved', requestId=None)
        self.state['pending'] = dict(field=field, codes=codes, question=text, signature=signature(TOOL, text))
        self.save()

    def report_plan(self):
        """Allow the existing terminal-summary CLI to count this explicit session."""
        if not self.state['sessionId']:
            return
        plan_path = self.path.with_name(self.path.stem+'-plan.json')
        result_path = plan_path.with_name(plan_path.stem+'-result.json')
        plan = dict(targetDate=self.target, scope='excel_field_supplement',
                    checkpointPath=str(self.path), fields=self.state['fields'])
        result = {key: self.state.get(key) for key in ('targetDate', 'status', 'sessionId', 'dataCalls',
                  'createdAt', 'startedAt', 'finishedAt', 'progress', 'stopReason')}
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        self.state.update(summaryPlanPath=str(plan_path), summaryResultPath=str(result_path))

    async def run(self, client_factory=WindMCP, key=None):
        with checkpoint_lock(self.path):
            current = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
            if current != self.initial_hash:
                raise ValueError('补字段 checkpoint 已被更新，请重新加载 --resume')
            return await self._run(client_factory, key)

    async def _run(self, client_factory, key):
        store.initialize(seed=False)
        if not self.reconcile():
            self.report_plan()
            self.save()
            return self.state
        if self.state['dataCalls'] >= self.state['maxDataCalls']:
            self.state.update(status='budget_paused', stopReason='已达到补字段累计数据调用上限')
            self.report_plan()
            self.save()
            return self.state
        key = key or read_wind_key()
        if not key:
            raise WindError('本机未配置 Wind Key')
        if self.state['sessionId']:
            recorder = Recorder.__new__(Recorder)
            recorder.id, recorder.stage = self.state['sessionId'], '恢复单指标补取'
        else:
            recorder = Recorder('Excel 单指标补取', self.target, config=dict(
                scope='excel_field_supplement', mainCheckpoint=str(self.main_path), checkpointPath=str(self.path),
                universeHash=self.state['universeHash'], fields=self.state['fields'],
                maxDataCalls=self.state['maxDataCalls'], publishedSnapshot=False))
            self.state['sessionId'] = recorder.id
        self.state.update(status='running', startedAt=self.state.get('startedAt') or store.now())
        self.state.pop('stopReason', None)
        self.save()
        try:
            async with client_factory(key, recorder=recorder) as mcp:
                tools = await mcp.list_tools()
                recorder.artifact('tool-catalog', tools)
                if TOOL not in {tool.get('name') for tool in tools}:
                    raise WindError('当前 Wind 服务未提供债券行情工具')
                while True:
                    self.counts()
                    if self.state['dataCalls'] >= self.state['maxDataCalls']:
                        self.state.update(status='budget_paused', stopReason='已达到补字段累计数据调用上限')
                        break
                    task = self.state.get('pending')
                    if not task:
                        main = self.read_main()
                        choices = self.candidates(main)
                        self.state['mainStatus'] = main['status']
                        self.state['candidateCounts'] = {field: len(codes) for field, codes in choices.items()}
                        active = main['status'] in MAIN_ACTIVE
                        selected = next((field for field in self.state['fields'] if choices[field]
                            and (not active or len(choices[field]) >= self.state['batchSize'])), None)
                        if selected:
                            self.reserve(selected, choices[selected][:self.state['batchSize']])
                            task = self.state['pending']
                        elif not active:
                            self.state.update(status='finished', finishedAt=store.now())
                            break
                        else:
                            self.save()
                            await self.sleeper(self.poll_seconds)
                            continue
                    self.save()
                    recorder.stage = f"单指标 {task['field']}：{len(task['codes'])} 只"
                    self.emit(dump(dict(event='request', field=task['field'], requested=len(task['codes']),
                                        dataCall=self.state['dataCalls']+1, progress=self.progress())), flush=True)
                    try:
                        await mcp.call(TOOL, {'question': task['question']})
                    except Exception as exc:
                        self.reconcile()
                        self.state.update(status='stopped', stopReason='单指标请求中断，证据和进度已保存；未自动重试',
                                          errorType=type(exc).__name__)
                        break
                    if not self.reconcile():
                        break
                    self.emit(dump(dict(event='progress', dataCalls=self.state['dataCalls'], progress=self.progress())), flush=True)
        except Exception as exc:
            self.state.update(status='stopped', stopReason='连接或本地处理异常，已暂停补字段；未自动重试',
                              errorType=type(exc).__name__)
        finally:
            self.counts()
            self.state['finishedAt'] = store.now()
            self.report_plan()
            self.save()
            recorder.artifact('excel-field-supplement', {key: value for key, value in self.state.items()
                if key not in ('manifest', 'attempts', 'pending')})
            self.emit(dump({key: self.state.get(key) for key in ('status', 'dataCalls', 'progress',
                'sessionId', 'stopReason', 'summaryPlanPath')}), flush=True)
        return self.state


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--main-checkpoint', type=Path, required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--fields', choices=list(FIELD_LABELS), nargs='+', default=['yieldPct'])
    parser.add_argument('--batch-size', type=int, default=100)
    parser.add_argument('--max-data-calls', type=int, required=True)
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    runner = Supplement(args.main_checkpoint, args.target, args.fields, args.batch_size,
                        args.max_data_calls, args.resume)
    state = asyncio.run(runner.run())
    return 1 if state['status'] == 'stopped' else 0


if __name__ == '__main__':
    raise SystemExit(main())
