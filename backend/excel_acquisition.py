"""Bounded, resumable acquisition for an explicit Excel bond universe.

Raw replies are committed by Recorder before a checkpoint advances. This is a
separate acquisition scope: it never queues the nationwide worker or publishes a
national snapshot. Successful replies can be projected by available_data at once.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import storage as store
from .available_data import _response_rows, read_available
from .credentials import read_wind_key
from .lineage import Recorder, dump
from .wind_mapping import ALIASES, CODE, Merge, base_name
from .wind_mcp import WindError, WindMCP
from .wind_verification import tables_from


FIELDS = {
    'basic': ('bondId', 'name', 'issuer', 'bondType', 'issueDate', 'maturityDate',
              'issueAmountYi', 'couponPct', 'currency'),
    'market': ('yieldPct', 'remainingYears', 'duration', 'outstandingBalanceYi', 'closeNetPrice'),
}
NUMERIC = {'issueAmountYi', 'couponPct', *FIELDS['market']}
TOOLS = {'basic': 'get_bond_basicinfo', 'market': 'get_bond_market_data'}
TERMINAL = {'cached', 'complete', 'partial', 'unavailable', 'uncertain'}
QUOTA_WORDS = ('余额不足', '请先充值', '额度不足', '额度耗尽', '配额', '调用次数已达',
               'quota', 'rate limit', 'too many requests', 'insufficient balance')


def question(group, codes, target):
    joined = '、'.join(codes)
    if group == 'market':
        return (f'查询{joined}在{target}的收盘价收益率（%）、实际剩余期限（年）、'
                '基于净价的收盘价修正久期（年）、债券余额（亿元）、收盘价净价（元），'
                '逐券返回Wind代码和原始指标值。')
    return (f'查询{joined}在{target}的发行总额（原始首次发行规模，亿元）、所属概念板块、'
            '票面利率（%）、主证券代码、跨市场代码、债务主体名称、发行起始日期、'
            '到期日期、交易币种。逐券返回Wind代码、证券简称、原始指标名、单位和值，缺失留空。')


def signature(tool, text):
    return hashlib.sha256(dump([tool, text]).encode('utf-8')).hexdigest()


def populated(value, field):
    if value is None or isinstance(value, str) and not value.strip():
        return False
    if field in NUMERIC:
        try:
            return not isinstance(value, bool) and Decimal(str(value)).is_finite()
        except (InvalidOperation, ValueError):
            return False
    return True


def group_complete(bond, group):
    errors = bond.get('validationErrors', bond.get('_validationErrors', []))
    return all(populated(bond.get(field), field) for field in FIELDS[group]) and not any(
        any(field in error for field in FIELDS[group]) for error in errors)


def cached_group(bond, group):
    """An explicitly returned null already answers a field query for this date."""
    sources = bond.get('fieldSources', {})
    attempted = all(sources.get(field) for field in FIELDS[group])
    status = 'cached' if group_complete(bond, group) else 'partial' if attempted else 'pending'
    return dict(status=status, requestIds=list(dict.fromkeys(
        cell['requestId'] for field in FIELDS[group] for cell in sources.get(field, []) if cell.get('requestId'))),
        cached=status != 'pending',
        missingFields=[field for field in FIELDS[group] if not populated(bond.get(field), field)])


def cache_index(data):
    index = {}
    for bond in data['bonds']:
        for code in {bond['code'], bond.get('bondId'), *bond.get('codes', [])}:
            if code:
                index[code] = bond
    return index


@contextmanager
def checkpoint_lock(path):
    """A second launcher must fail before issuing requests for this checkpoint."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix+'.lock').open('a+b') as stream:
        stream.seek(0)
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('该 checkpoint 已有执行进程，禁止并行重复取数') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def response_index(detail, target):
    """Match explicit returned primary/listing aliases; keep outside rows in raw evidence."""
    response = detail['response']
    raw = dict(id=detail['id'], response=dump(response), target_date=target)
    grouped = {}
    for code, cells in _response_rows(raw):
        aliases = {code}
        aliases.update(cell['value'] for cell in cells
                       if base_name(cell['name'], target)[0] in (*ALIASES['code'], *ALIASES['bondId'])
                       and isinstance(cell['value'], str) and CODE.fullmatch(cell['value']))
        # One response may contain several tables for the same listing.
        entry = grouped.setdefault(code, {'cells': [], 'aliases': set()})
        entry['cells'].extend(cells)
        entry['aliases'].update(aliases)
    index = {}
    for code, entry in grouped.items():
        merge = Merge(target)
        merge.fields[code] = entry['cells']
        normalized, _ = merge.normalize(code)
        normalized.update(normalized.pop('_descriptive'))
        for alias in entry['aliases']:
            index[alias] = normalized
    return index


class Acquisition:
    def __init__(self, universe_path, target, batch_size, max_data_calls, resume=None,
                 available_reader=read_available, emit=print, retry_rejected=False):
        if date.fromisoformat(target) >= datetime.now(timezone(timedelta(hours=8))).date():
            raise ValueError('只能查询已结束日期')
        if not 1 <= batch_size <= 100 or not 0 <= max_data_calls <= 10000:
            raise ValueError('batch-size 必须在 1–100，max-data-calls 必须在 0–10000')
        if retry_rejected and not resume:
            raise ValueError('--retry-rejected 只能用于显式恢复已有 checkpoint')
        self.retry_rejected = retry_rejected
        self.universe_path = Path(universe_path).resolve()
        universe = json.loads(self.universe_path.read_text(encoding='utf-8-sig'))
        raw_codes = universe.get('codes', [])
        if not isinstance(raw_codes, list) or not raw_codes or any(
                not isinstance(code, str) or not CODE.fullmatch(code) for code in raw_codes):
            raise ValueError('债券名单必须是非空的有效 Wind 代码列表')
        codes = list(dict.fromkeys(raw_codes))
        digest = hashlib.sha256(dump(codes).encode('utf-8')).hexdigest()
        self.target, self.emit, self.available_reader = target, emit, available_reader
        self.path = Path(resume).resolve() if resume else self.universe_path.with_name(
            self.universe_path.stem+'-acquisition-'+target+'.json')
        self.initial_checkpoint_hash = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
        self.cache = cache_index(available_reader(target))
        if resume:
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            if (self.state.get('version') != 1 or self.state.get('scope') != 'excel_universe'
                    or self.state['targetDate'] != target or self.state['universeHash'] != digest
                    or self.state.get('sourceHash') != universe.get('sourceHash')):
                raise ValueError('恢复文件的日期、范围或 Excel 文件指纹不匹配')
            if max_data_calls < self.state['dataCalls']:
                raise ValueError('新的累计调用上限小于已经发生的调用数')
            self.state['maxDataCalls'] = max_data_calls
        else:
            if self.path.exists():
                raise ValueError('已有 checkpoint；请使用 --resume，避免重复付费查询')
            manifest = {code: {group: cached_group(self.cache.get(code, {}), group)
                               for group in FIELDS} for code in codes}
            queue = []
            # Interleave basic/market batches so useful individual bonds become
            # available early, even when the explicit call budget pauses the run.
            for start in range(0, len(codes), batch_size):
                for group in FIELDS:
                    needed = [code for code in codes[start:start+batch_size]
                              if manifest[code][group]['status'] == 'pending']
                    if needed:
                        queue.append(dict(group=group, codes=needed, depth=0, singleFallback=False))
            self.state = dict(version=1, scope='excel_universe', targetDate=target,
                              sourcePath=universe.get('sourcePath'), sourceHash=universe.get('sourceHash'),
                              sheet=universe.get('sheet'), universeHash=digest,
                              universePath=str(self.universe_path), requestedCodes=len(codes),
                              rawCodeCount=len(raw_codes), codes=codes, batchSize=batch_size,
                              maxDataCalls=max_data_calls, dataCalls=0, protocolRequests=0,
                              status='prepared', createdAt=store.now(), sessionId=None,
                              queue=queue, pending=None, manifest=manifest, attempts=[],
                              unexpectedCodes=[], publishedSnapshot=False)
        self.refresh_cached()

    def refresh_cached(self):
        for code, groups in self.state['manifest'].items():
            for group, entry in groups.items():
                cached = cached_group(self.cache.get(code, {}), group)
                if entry['status'] == 'pending' and cached['status'] != 'pending':
                    entry.update(cached)

    def summary(self):
        manifest = self.state['manifest']
        return dict(requested=len(manifest),
                    fullyPopulated=sum(all(group['status'] in ('cached', 'complete') for group in groups.values())
                                       for groups in manifest.values()),
                    attempted=sum(any(group['requestIds'] for group in groups.values()) for groups in manifest.values()),
                    terminal=sum(all(group['status'] in TERMINAL for group in groups.values()) for groups in manifest.values()),
                    partial=sum(any(group['status'] in ('partial', 'unavailable') for group in groups.values())
                                for groups in manifest.values()),
                    uncertain=sum(any(group['status'] == 'uncertain' for group in groups.values())
                                  for groups in manifest.values()))

    def save(self):
        self.state.update(updatedAt=store.now(), progress=self.summary())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix+'.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(self.state, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def refresh_coverage(self):
        final = cache_index(self.available_reader(self.target))
        scoped = {code: final[code] for code in self.state['codes'] if code in final}
        for code, bond in scoped.items():
            for group, entry in self.state['manifest'][code].items():
                if entry['status'] in ('cached', 'complete', 'partial'):
                    if group_complete(bond, group):
                        entry['status'] = 'cached' if entry.get('cached') else 'complete'
                    else:
                        entry['status'] = 'partial'
                    entry['missingFields'] = [field for field in FIELDS[group]
                                              if not populated(bond.get(field), field)]
        self.state['coverage'] = dict(requestedCodes=self.state['requestedCodes'],
            presentCodes=len(scoped), fullyPopulatedCodes=sum(all(group_complete(bond, group) for group in FIELDS)
                for bond in scoped.values()), completeUniverse=len(scoped) == self.state['requestedCodes'],
            nationalMarketComplete=False)

    def history(self):
        if not self.state.get('sessionId'):
            return []
        with store.connection() as db:
            return [dict(row) for row in db.execute(
                'SELECT id,method FROM source_requests '
                'WHERE session_id=? ORDER BY rowid', (self.state['sessionId'],))]

    def sync_counts(self, history):
        self.state['protocolRequests'] = len(history)
        self.state['dataCalls'] = sum(row['method'] == 'tools/call' for row in history)

    def apply_reply(self, task, detail):
        rid, group = detail['id'], task['group']
        index = response_index(detail, self.target)
        matched = [code for code in task['codes'] if code in index]
        missing = [code for code in task['codes'] if code not in index]
        outside = set(index)-set(self.state['codes'])
        self.state['unexpectedCodes'] = sorted(set(self.state['unexpectedCodes']) | outside)
        for code in matched:
            merged = dict(self.cache.get(code, {}))
            merged.update({field: value for field, value in index[code].items()
                           if value is not None and value != ''})
            self.cache[code] = merged
            entry = self.state['manifest'][code][group]
            entry.update(status='complete' if group_complete(merged, group) else 'partial',
                         missingFields=[field for field in FIELDS[group] if not populated(merged.get(field), field)])
        for code in task['codes']:
            entry = self.state['manifest'][code][group]
            if rid not in entry['requestIds']:
                entry['requestIds'].append(rid)
        if missing:
            if len(task['codes']) == 1 and task['singleFallback']:
                self.state['manifest'][missing[0]][group]['status'] = 'unavailable'
            else:
                midpoint = max(1, math.ceil(len(missing)/2))
                fallback = [dict(group=group, codes=missing[start:start+midpoint], depth=task['depth']+1,
                                 singleFallback=len(missing[start:start+midpoint]) == 1)
                            for start in range(0, len(missing), midpoint)]
                self.state['queue'][0:0] = fallback
        self.state['attempts'].append(dict(signature=task['signature'], requestId=rid, group=group,
                                           requested=len(task['codes']), returned=len(matched), missing=len(missing)))
        self.state['pending'] = None

    def stop_reply(self, task, detail, reason):
        for code in task['codes']:
            entry = self.state['manifest'][code][task['group']]
            entry['status'] = 'uncertain'
            if detail['id'] not in entry['requestIds']:
                entry['requestIds'].append(detail['id'])
        self.state.update(status='stopped', stopReason=reason)

    def allow_rejected_retry(self):
        """Explicitly reopen only a proved, empty insufficient-balance refusal.

        Previously committed raw requests remain unchanged and still count
        toward the cumulative budget. Transport uncertainty is never eligible.
        """
        task = self.state.get('pending')
        if not task or not self.state.get('sessionId'):
            return
        excluded = {entry['requestId'] for entry in self.state.get('attempts', [])}
        excluded.update(entry['requestId'] for entry in self.state.get('rejectedRetries', []))
        with store.connection() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT id,status,http_status,response FROM source_requests WHERE session_id=? "
                "AND method='tools/call' AND tool=? AND json_extract(request,'$.params.arguments.question')=? ORDER BY rowid",
                (self.state['sessionId'], TOOLS[task['group']], task['question'])) if row['id'] not in excluded]
        if not rows:
            return
        row = rows[-1]
        response = json.loads(row['response']) if row['response'] else None
        result = response.get('result', {}) if isinstance(response, dict) else {}
        if row['status'] == 'succeeded' and not result.get('isError'):
            return  # Let normal reconciliation replay an already-paid success.
        text = dump(result).lower()
        eligible = (row['http_status'] == 200 and row['status'] in ('failed', 'succeeded')
                    and result.get('isError') is True
                    and any(word in text for word in ('余额不足', 'insufficient balance')))
        if eligible:
            try:
                eligible = not any(table['rows'] for table in tables_from(result))
            except (ValueError, TypeError, KeyError, AttributeError, WindError):
                eligible = False
        if not eligible:
            raise ValueError('只能重试明确余额不足且没有数据行的业务拒绝；网络不确定、其他错误或已有数据行均禁止重发')
        self.state.setdefault('rejectedRetries', []).append(dict(requestId=row['id'],
            signature=task['signature'], group=task['group'], codes=task['codes'],
            authorizedAt=store.now(), reason='explicit_retry_rejected_insufficient_balance'))
        for code in task['codes']:
            entry = self.state['manifest'][code][task['group']]
            cached = cached_group(self.cache.get(code, {}), task['group'])
            entry.update(status=cached['status'], cached=cached['cached'], missingFields=cached['missingFields'])
            entry['requestIds'] = list(dict.fromkeys([*entry['requestIds'], row['id'], *cached['requestIds']]))
        self.state.update(status='prepared')
        self.state.pop('stopReason', None)
        self.save()

    def reconcile(self):
        """Replay already-committed replies; never reissue an uncertain paid request."""
        history = self.history()
        self.sync_counts(history)
        pending = self.state.get('pending')
        if not pending:
            return True
        # Match the exact persisted paid question in SQL. Historical response
        # bodies can be large; fetch only this pending reply, never the whole
        # session on every batch.
        with store.connection() as db:
            matching = [dict(row) for row in db.execute(
                "SELECT id,status,http_status,error FROM source_requests WHERE session_id=? "
                "AND method='tools/call' AND tool=? "
                "AND json_extract(request,'$.params.arguments.question')=? ORDER BY rowid",
                (self.state['sessionId'], TOOLS[pending['group']], pending['question']))]
        # A previous successful empty singleton may have the same query text as
        # its one fallback. Only requests not accounted for in attempts are new.
        applied = {attempt['requestId'] for attempt in self.state['attempts']}
        applied.update(entry['requestId'] for entry in self.state.get('rejectedRetries', []))
        matching = [row for row in matching if row['id'] not in applied]
        if not matching:
            return True  # Checkpoint persisted before Recorder.begin: safe to issue.
        row = matching[-1]
        with store.connection() as db:
            raw_response = db.execute('SELECT response FROM source_requests WHERE id=?', (row['id'],)).fetchone()[0]
        row['response'] = json.loads(raw_response) if raw_response else None
        raw = dump(row['response']).lower()
        result = row['response'].get('result', {}) if isinstance(row['response'], dict) else {}
        if (row['status'] != 'succeeded' or row['http_status'] in (401, 403, 429)
                or not row['response'] or result.get('isError')
                or any(word in raw for word in QUOTA_WORDS)):
            self.stop_reply(pending, row, '请求失败或结果不确定，已暂停；不会自动重发可能已计费的请求')
            self.save()
            return False
        try:
            self.apply_reply(pending, row)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, WindError):
            self.stop_reply(pending, row, '已收到响应但解析失败，原文已保存；暂停并避免重复付费')
            self.save()
            return False
        self.save()
        return True

    def next_task(self):
        if self.state.get('pending'):
            pending = self.state['pending']
            kept = [code for code in pending['codes']
                    if self.state['manifest'][code][pending['group']]['status'] == 'pending']
            if kept:
                if kept != pending['codes']:
                    pending['codes'] = kept
                    pending['question'] = question(pending['group'], kept, self.target)
                    pending['signature'] = signature(TOOLS[pending['group']], pending['question'])
                return pending
            self.state['pending'] = None
        while self.state['queue']:
            task = self.state['queue'].pop(0)
            task['codes'] = [code for code in task['codes']
                             if self.state['manifest'][code][task['group']]['status'] == 'pending']
            if not task['codes']:
                continue
            task['question'] = question(task['group'], task['codes'], self.target)
            task['signature'] = signature(TOOLS[task['group']], task['question'])
            self.state['pending'] = task
            return task
        return None

    async def run(self, client_factory=WindMCP, key=None):
        with checkpoint_lock(self.path):
            current_hash = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
            if current_hash != self.initial_checkpoint_hash:
                raise ValueError('checkpoint 已由另一个进程更新，请重新使用 --resume 加载')
            return await self._run(client_factory, key)

    async def _run(self, client_factory, key):
        store.initialize(seed=False)
        if self.retry_rejected:
            self.allow_rejected_retry()
        if not self.reconcile():
            self.refresh_coverage()
            self.save()
            return self.state
        task = self.next_task()
        if not task:
            self.state.update(status='finished', finishedAt=store.now())
            self.refresh_coverage()
            self.save()
            return self.state
        if self.state['dataCalls'] >= self.state['maxDataCalls']:
            self.state.update(status='budget_paused', stopReason='已达到累计数据调用上限')
            self.refresh_coverage()
            self.save()
            return self.state
        key = key or read_wind_key()
        if not key:
            raise WindError('本机未配置 Wind Key')
        if self.state['sessionId']:
            recorder = Recorder.__new__(Recorder)
            recorder.id, recorder.stage = self.state['sessionId'], '恢复连接'
        else:
            recorder = Recorder('Excel 全部债券取数', self.target, config={
                'scope': 'excel_universe', 'universeHash': self.state['universeHash'],
                'universePath': str(self.universe_path), 'checkpointPath': str(self.path),
                'requestedCodes': self.state['requestedCodes'], 'sourceHash': self.state['sourceHash'],
                'maxDataCalls': self.state['maxDataCalls'], 'publishedSnapshot': False})
            self.state['sessionId'] = recorder.id
        self.state.update(status='running', startedAt=self.state.get('startedAt') or store.now())
        self.state.pop('stopReason', None)
        self.save()
        try:
            async with client_factory(key, recorder=recorder) as mcp:
                catalog = await mcp.list_tools()
                recorder.artifact('tool-catalog', catalog)
                needed = {TOOLS[task['group']] for task in [self.state['pending'], *self.state['queue']] if task}
                if not needed <= {tool.get('name') for tool in catalog}:
                    raise WindError('当前服务缺少该计划需要的债券工具')
                while (task := self.next_task()) is not None:
                    self.sync_counts(self.history())
                    if self.state['dataCalls'] >= self.state['maxDataCalls']:
                        self.state.update(status='budget_paused', stopReason='已达到累计数据调用上限')
                        break
                    # Persist the exact paid question before any HTTP operation.
                    self.save()
                    recorder.stage = f"Excel {task['group']}：{len(task['codes'])} 只"
                    self.emit(dump(dict(event='request', dataCall=self.state['dataCalls']+1,
                                        maxDataCalls=self.state['maxDataCalls'], group=task['group'],
                                        requested=len(task['codes']), progress=self.summary())), flush=True)
                    try:
                        await mcp.call(TOOLS[task['group']], {'question': task['question']})
                    except Exception as exc:
                        # Recorder owns HTTP evidence; do not print exception text
                        # from clients, which could carry a key or remote content.
                        self.reconcile()
                        self.state.update(status='stopped', stopReason='Wind 请求中断，已保存证据与进度；未自动重试',
                                          errorType=type(exc).__name__)
                        break
                    if not self.reconcile():
                        break
                    self.emit(dump(dict(event='progress', dataCalls=self.state['dataCalls'],
                                        progress=self.summary())), flush=True)
                else:
                    self.state.update(status='finished', finishedAt=store.now())
        except Exception as exc:
            self.state.update(status='stopped', stopReason='连接或本地处理异常，进度已保存；未自动重试',
                              errorType=type(exc).__name__)
        finally:
            self.sync_counts(self.history())
            self.save()
            # Refresh once after the bounded run; never rescan every historical
            # response for each batch in a large Excel universe.
            self.refresh_coverage()
            self.save()
            recorder.artifact('excel-universe-acquisition', {key: value for key, value in self.state.items()
                if key not in ('manifest', 'queue', 'codes', 'attempts')})
            self.emit(dump({key: self.state.get(key) for key in
                ('status', 'targetDate', 'sessionId', 'dataCalls', 'progress', 'coverage', 'stopReason')}), flush=True)
        return self.state


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--universe', type=Path, required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--batch-size', type=int, required=True)
    parser.add_argument('--max-data-calls', type=int, required=True)
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--retry-rejected', action='store_true',
                        help='充值确认后，显式重试没有数据行的余额不足业务拒绝；不重试网络不确定请求')
    args = parser.parse_args()
    acquisition = Acquisition(args.universe, args.target, args.batch_size, args.max_data_calls, args.resume,
                              retry_rejected=args.retry_rejected)
    state = asyncio.run(acquisition.run())
    return 1 if state['status'] == 'stopped' else 0


if __name__ == '__main__':
    raise SystemExit(main())
