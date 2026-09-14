"""Resumable public local-government-bond catalog and static-detail synchronizer.

The market universe comes from the documented AKShare issuance-year catalog,
never from traded bonds. Completing this job does not imply complete quotations.
"""
import argparse
from collections import deque
from concurrent.futures import FIRST_COMPLETED, wait
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
import os
import re
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from . import storage as store
from . import akshare_provider as provider

MAX_ATTEMPTS = 3
DETAIL_WORKERS = 3
PUBLISH_EVERY = 250
PUBLISH_SECONDS = 60
CATALOG_COLUMNS = ['债券简称', '债券代码', '发行人/受托机构', '债券类型', '发行日期', '查询代码']
# One owning job only: version changes invalidate this optimization, and SQLite
# remains authoritative across normal collectors, restarts and source switches.
_PUBLICATION_CACHE = None


class SyncPaused(Exception):
    """A requested pause or replacement generation stopped this worker."""


def initialize():
    provider.initialize(seed=False)
    with store.connection() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS akshare_sync_jobs (
                job_id TEXT PRIMARY KEY, target_date TEXT NOT NULL, universe TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS akshare_sync_partitions (
                job_id TEXT NOT NULL, year TEXT NOT NULL, status TEXT NOT NULL,
                total INTEGER, attempts INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
                error TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(job_id,year));
            CREATE TABLE IF NOT EXISTS akshare_sync_items (
                job_id TEXT NOT NULL, code TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
                error TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(job_id,code));
            CREATE INDEX IF NOT EXISTS akshare_sync_items_status ON akshare_sync_items(job_id,status);
        ''')


def _get_job(job_id):
    rows = provider._read_rows('SELECT payload FROM akshare_sync_jobs WHERE job_id=?', (job_id,))
    if not rows:
        raise KeyError('全市场同步任务不存在')
    return json.loads(rows[0]['payload'])


def _write_job(db, job):
    db.execute('UPDATE akshare_sync_jobs SET status=?,payload=? WHERE job_id=?',
               (job['status'], provider._dump(job), job['jobId']))


def _assert_owner(db, job_id, generation, states=('running',)):
    row = db.execute('SELECT payload FROM akshare_sync_jobs WHERE job_id=?', (job_id,)).fetchone()
    if not row:
        raise KeyError('全市场同步任务不存在')
    job = json.loads(row['payload'])
    if (generation is not None and job.get('generation') != generation) or (states is not None and job['status'] not in states):
        raise SyncPaused()
    return job


def _update_job(job_id, *, expected_generation=None, expected_states=None, include_progress=True, **changes):
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        job = _assert_owner(db, job_id, expected_generation, expected_states)
        job.update(changes, updatedAt=store.now())
        _write_job(db, job)
    return _progress(job_id) if include_progress else job


def _update_owned(job, **changes):
    return _update_job(job['jobId'], expected_generation=job['generation'],
                       expected_states=('running',), include_progress=False, **changes)


def _progress(job_id):
    job = _get_job(job_id)
    counts = {row['status']: row['n'] for row in provider._read_rows(
        'SELECT status,COUNT(*) n FROM akshare_sync_items WHERE job_id=? GROUP BY status', (job_id,))}
    partitions = {row['status']: row['n'] for row in provider._read_rows(
        'SELECT status,COUNT(*) n FROM akshare_sync_partitions WHERE job_id=? GROUP BY status', (job_id,))}
    discovered = sum(counts.values())
    job.update(total=discovered if job.get('catalogComplete') else None,
               discovered=discovered, completed=counts.get('completed', 0),
               failed=counts.get('failed', 0)+counts.get('unmapped', 0), unmapped=counts.get('unmapped', 0),
               pending=counts.get('pending', 0)+counts.get('running', 0),
               catalogCompletedPartitions=partitions.get('completed', 0),
               catalogTotalPartitions=sum(partitions.values()),
               catalogFailedPartitions=partitions.get('failed', 0), marketDataComplete=False)
    return job


def status(target=None):
    if target is not None:
        date.fromisoformat(target)
    rows = provider._read_rows('SELECT job_id FROM akshare_sync_jobs'+
                              (' WHERE target_date=?' if target else '')+' ORDER BY rowid DESC LIMIT 1',
                              (target,) if target else ())
    return _progress(rows[0]['job_id']) if rows else None


def get(job_id):
    return _progress(job_id)


def pending_jobs():
    return [row['job_id'] for row in provider._read_rows(
        "SELECT job_id FROM akshare_sync_jobs WHERE status IN ('queued','running') ORDER BY rowid")]


def resume(job_id):
    job = _get_job(job_id)
    return start(job['targetDate'], job['universe'])


def start(target, universe='market'):
    target = date.fromisoformat(target).isoformat()
    if universe != 'market':
        raise ValueError('仅支持全市场地方政府债（market）')
    initialize()
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        active = db.execute("SELECT payload FROM akshare_sync_jobs WHERE status IN ('queued','running') ORDER BY rowid LIMIT 1").fetchone()
        if active:
            active_job = json.loads(active['payload'])
            if active_job['targetDate'] != target:
                raise ValueError('已有其他日期的全市场同步正在运行，请先暂停该任务。')
        row = db.execute('SELECT payload FROM akshare_sync_jobs WHERE target_date=? AND universe=? ORDER BY rowid DESC LIMIT 1',
                         (target, universe)).fetchone()
        if row:
            job = json.loads(row['payload'])
            if job['status'] in ('paused', 'partial', 'failed'):
                job.update(status='queued', phase='等待继续同步', generation=job.get('generation', 1)+1,
                           updatedAt=store.now(), message='已保存进度；继续未完成条目，并重试失败条目。')
                for table in ('akshare_sync_items', 'akshare_sync_partitions'):
                    db.execute(f"UPDATE {table} SET status='pending',attempts=0,error=NULL WHERE job_id=? AND status IN ('failed','running')", (job['jobId'],))
                _write_job(db, job)
        else:
            job = dict(jobId=str(uuid.uuid4()), targetDate=target, universe=universe, source='akshare',
                       status='queued', phase='等待同步全市场名单', total=None, completed=0, failed=0,
                       pending=0, catalogComplete=False, marketDataComplete=False,
                       enumComplete=False,
                       createdAt=store.now(), updatedAt=store.now(), generation=1,
                       message='按发行年份获取中国货币网地方政府债名单，并持续补齐基本信息。')
            db.execute('INSERT INTO akshare_sync_jobs VALUES (?,?,?,?,?)',
                       (job['jobId'], target, universe, job['status'], provider._dump(job)))
    return _progress(job['jobId'])


def pause(job_id):
    job = _get_job(job_id)
    if job['status'] in ('queued', 'running'):
        try:
            return _update_job(job_id, expected_generation=job['generation'], expected_states=('queued', 'running'),
                               status='paused', phase='已暂停',
                               message='暂停请求已保存；当前网络请求结束后停止，可从已保存进度继续。')
        except SyncPaused:
            return _progress(job_id)
    return _progress(job_id)


def _check(job_id, generation):
    current = _get_job(job_id)
    if current['status'] == 'paused' or current.get('generation') != generation:
        raise SyncPaused()


def _backoff(job, attempt):
    if attempt < MAX_ATTEMPTS:
        _check(job['jobId'], job['generation'])
        time.sleep(min(2**(attempt-1), 4))
        _check(job['jobId'], job['generation'])


def _brief(entry):
    return {key: entry.get(key) for key in ('requestId', 'runId', 'function', 'arguments',
                                           'retrievedAt', 'executionMode', 'dictionaryUrl')}


def _raise_if_denied(recorder):
    statuses = {response.get('status') for entry in recorder.queries
                for response in entry.get('responses', [])}
    denied = statuses & {401, 403, 429}
    if denied:
        raise RuntimeError(f'公开数据源返回 HTTP {min(denied)}，已停止本批请求并保留断点；稍后可继续同步')


def _body_data(entry):
    return [response['body']['data'] for response in entry.get('responses', [])
            if 'BondMarketInfoList2' in response.get('url', '')
            and isinstance(response.get('body'), dict)
            and isinstance(response['body'].get('data'), dict)]


def validate_catalog(rows, entry, year):
    """Require upstream totals and exact identity agreement with the SDK output."""
    responses = _body_data(entry)
    if not responses:
        raise ValueError('名单响应缺少原始分页证据')
    totals, page_totals = set(), set()
    raw_by_id = {}
    for response in responses:
        total, pages = response.get('total'), response.get('pageTotal')
        if not isinstance(total, int) or isinstance(total, bool) or total < 0 or not isinstance(pages, int) or pages < 0:
            raise ValueError('名单分页总数无效')
        totals.add(total)
        page_totals.add(pages)
        if not isinstance(response.get('resultList'), list):
            raise ValueError('名单响应缺少 resultList')
        for row in response['resultList']:
            registry_id = row.get('bondDefinedCode')
            if not isinstance(registry_id, str) or not registry_id.strip() or row.get('bondType') != '地方政府债':
                raise ValueError('名单存在身份或债券类型异常')
            issued = provider._date(row.get('issueStartDate'))
            if issued is None or issued[:4] != year:
                raise ValueError('名单发行年份与分区不符')
            identity = (row.get('bondName'), provider._code(row.get('bondCode')), issued, row.get('entyFullName'))
            if registry_id in raw_by_id and raw_by_id[registry_id] != identity:
                raise ValueError('名单同一官方登记ID的身份信息冲突')
            raw_by_id[registry_id] = identity
    if len(totals) != 1 or len(page_totals) != 1:
        raise ValueError('获取过程中名单总数发生变化，请重试分区')
    total, page_count = next(iter(totals)), next(iter(page_totals))
    if total == 0:
        if rows or raw_by_id:
            raise ValueError('空名单总数与实际记录矛盾')
        return []
    if page_count < 1 or len(responses) < page_count or len(raw_by_id) != total:
        raise ValueError('名单分页未完整返回')
    pages = []
    for response in entry.get('responses', []):
        if 'BondMarketInfoList2' not in response.get('url', ''):
            continue
        request_body = response.get('requestBody')
        if isinstance(request_body, str):
            values = parse_qs(request_body)
            if values.get('pageNo'):
                pages.append(int(values['pageNo'][0]))
    if pages and not set(range(1, page_count+1)) <= set(pages):
        raise ValueError('名单请求缺少分页页码')
    sdk_by_id = {}
    for row in rows:
        registry_id = row.get('查询代码')
        if not isinstance(registry_id, str) or not registry_id.strip() or row.get('债券类型') != '地方政府债' or registry_id in sdk_by_id:
            raise ValueError('SDK 名单存在重复或无效官方登记ID')
        identity = (row.get('债券简称'), provider._code(row.get('债券代码')), provider._date(row.get('发行日期')), row.get('发行人/受托机构'))
        if identity != raw_by_id.get(registry_id) or not identity[0]:
            raise ValueError('SDK 名单身份与原始响应不一致')
        sdk_by_id[registry_id] = row
    if set(sdk_by_id) != set(raw_by_id):
        raise ValueError('SDK 名单数量与原始分页总数不一致')
    return list(sdk_by_id.values())


def _catalog_identity(row):
    return provider._code(row.get('债券代码')) or 'CFETS:'+row['查询代码']


def _cached_catalog(job_id, year):
    for row in provider._read_rows('SELECT payload FROM akshare_queries WHERE run_id=? AND function_name=? ORDER BY rowid DESC',
                                   (job_id, 'bond_info_cm')):
        entry = json.loads(row['payload'])
        if entry.get('status') != 'succeeded' or entry.get('arguments') != {'bond_type': '地方政府债', 'issue_year': year}:
            continue
        try:
            records = entry['records']
            validate_catalog(records, entry, year)
        except (ValueError, KeyError, TypeError):
            continue
        return records, entry
    return None


def _valid_detail(code, detail):
    if provider._code(detail.get('bondCode')) != code or detail.get('bondType') != '地方政府债':
        return False
    for field in ('bondName', 'bondFullName', 'entyFullName'):
        if detail.get(field) in (None, '', '---'):
            return False
    return (provider._date(detail.get('issueDate')) is not None
            and provider._date(detail.get('mrtyDate')) is not None
            and provider._numeric(detail.get('issueAmnt')) is not None
            and provider.number(detail['issueAmnt']) > 0
            and provider._numeric(detail.get('parCouponRate')) is not None)


def _recorder(job, timeout_seconds, phase):
    def on_response(response):
        _check(job['jobId'], job['generation'])
        data = response.get('body', {}).get('data', {}) if isinstance(response.get('body'), dict) else {}
        page = None
        if isinstance(response.get('requestBody'), str):
            page = parse_qs(response['requestBody']).get('pageNo', [None])[0]
        _update_owned(job, phase=phase+(f" · 第 {page}/{data.get('pageTotal', '?')} 页" if page else ''))
        time.sleep(.15)
        _check(job['jobId'], job['generation'])
    return provider.QueryRecorder(dict(runId=job['jobId'], source='akshare', targetDate=job['targetDate']),
                                  timeout_seconds=timeout_seconds, on_response=on_response)


def _set_partition(job_id, year, state, attempts=0, total=None, payload=None, error=None, generation=None):
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if generation is not None:
            _assert_owner(db, job_id, generation)
        db.execute('INSERT INTO akshare_sync_partitions VALUES (?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(job_id,year) DO UPDATE SET status=excluded.status,total=excluded.total,'
                   'attempts=excluded.attempts,payload=excluded.payload,error=excluded.error,updated_at=excluded.updated_at',
                   (job_id, year, state, total, attempts, provider._dump(payload or {}), error, store.now()))


def _catalog(job):
    job_id, target = job['jobId'], job['targetDate']
    partitions = provider._read_rows('SELECT * FROM akshare_sync_partitions WHERE job_id=? ORDER BY year DESC', (job_id,))
    current = _get_job(job_id)
    if not current.get('enumComplete') or set(current.get('enumYears', [])) != {row['year'] for row in partitions}:
        _check(job_id, job['generation'])
        recorder = _recorder(job, 60, '获取官方发行年份枚举')
        rows, entry = recorder.query('bond_info_cm_query', {'symbol': '发行年份'}, ['name', 'code'])
        years = sorted({str(row['name']) for row in rows if re.fullmatch(r'\d{4}', str(row.get('name', '')))
                        and str(row['name']) <= target[:4]}, reverse=True)
        if not years:
            raise ValueError('发行年份枚举未返回有效年份')
        # The enum marker and every partition are one durable commit. A worker
        # killed between individual years must never mistake a prefix for all years.
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = _assert_owner(db, job_id, job['generation'])
            for year in years:
                db.execute('INSERT OR IGNORE INTO akshare_sync_partitions VALUES (?,?,?,?,?,?,?,?)',
                           (job_id, year, 'pending', None, 0, provider._dump({'enumEvidence': _brief(entry)}), None, store.now()))
            current.update(enumComplete=True, enumYears=years, enumEvidence=_brief(entry), updatedAt=store.now())
            _write_job(db, current)
        partitions = provider._read_rows('SELECT * FROM akshare_sync_partitions WHERE job_id=? ORDER BY year DESC', (job_id,))
    last_publish = time.monotonic()
    for partition in partitions:
        if partition['status'] == 'completed':
            continue
        year = partition['year']
        saved_catalog = _cached_catalog(job_id, year)
        for attempt in range(partition['attempts']+1, MAX_ATTEMPTS+1):
            _check(job_id, job['generation'])
            _set_partition(job_id, year, 'running', attempt, generation=job['generation'])
            _update_owned(job, currentYear=year, phase=f'同步 {year} 年地方政府债名单')
            recorder = _recorder(job, 600, f'同步 {year} 年地方政府债名单')
            try:
                try:
                    if saved_catalog is not None:
                        rows, entry = saved_catalog
                        _update_owned(job, phase=f'恢复已保存的 {year} 年完整名单', message='复用本任务已成功返回的官方名单，保留原查询证据和采集时间。')
                    else:
                        rows, entry = recorder.query('bond_info_cm', {'bond_type': '地方政府债', 'issue_year': year}, CATALOG_COLUMNS)
                except SyncPaused:
                    raise
                except Exception:
                    # The SDK selects columns after a zero-row response and may
                    # raise KeyError. Only an explicit upstream zero is accepted.
                    entry = recorder.queries[-1] if recorder.queries else {}
                    if not _body_data(entry) or any(data.get('total') != 0 for data in _body_data(entry)):
                        raise
                    rows = []
                records = validate_catalog(rows, entry, year)
                eligible = [row for row in records if provider._date(row['发行日期']) <= target]
                with store.connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    _assert_owner(db, job_id, job['generation'])
                    for row in eligible:
                        code = _catalog_identity(row)
                        existing = db.execute('SELECT payload FROM akshare_sync_items WHERE job_id=? AND code=?', (job_id, code)).fetchone()
                        payload = dict(catalog=row, evidence=_brief(entry), year=year)
                        if existing and json.loads(existing['payload'])['catalog'] != row:
                            raise ValueError('不同发行年份分区返回重复身份')
                        cached = db.execute('SELECT payload FROM akshare_details WHERE code=?', (code,)).fetchone()
                        state = ('unmapped' if code.startswith('CFETS:') else
                                 'completed' if cached and _valid_detail(code, json.loads(cached['payload'])) else 'pending')
                        db.execute('INSERT OR IGNORE INTO akshare_sync_items VALUES (?,?,?,?,?,?,?)',
                                   (job_id, code, state, 0, provider._dump(payload),
                                    '官方名单尚未公布可用交易代码；保留登记ID，未取得个券详情' if state == 'unmapped' else None, store.now()))
                    _partition_payload = dict(evidence=_brief(entry), upstreamTotal=len(records),
                                              futureIssueExcluded=len(records)-len(eligible), eligibleCount=len(eligible))
                    db.execute("UPDATE akshare_sync_partitions SET status='completed',total=?,payload=?,error=NULL,updated_at=? WHERE job_id=? AND year=?",
                               (len(eligible), provider._dump(_partition_payload), store.now(), job_id, year))
                break
            except SyncPaused:
                raise
            except Exception as exc:
                _set_partition(job_id, year, 'failed', attempt, error=f'{type(exc).__name__}: {str(exc)[:300]}', generation=job['generation'])
                _raise_if_denied(recorder)
                _update_owned(job, message=f'{year} 年名单失败（{attempt}/{MAX_ATTEMPTS}）：{str(exc)[:150]}')
                _backoff(job, attempt)
        if time.monotonic()-last_publish >= PUBLISH_SECONDS:
            materialize(job_id)
            last_publish = time.monotonic()
    remaining = provider._read_rows("SELECT COUNT(*) n FROM akshare_sync_partitions WHERE job_id=? AND status!='completed'", (job_id,))[0]['n']
    _update_owned(job, catalogComplete=remaining == 0, currentYear=None)
    materialize(job_id)


def _source_evidence(source):
    return dict(requestId=source.get('requestId'), runId=source.get('sessionId'),
                function=source.get('sourceFunction'), retrievedAt=source.get('retrievedAt'),
                executionMode=source.get('executionMode', 'documented_sdk'), evidence=source.get('evidence', []),
                reusedSameDate=True, sourceDate=source.get('sourceDate'))


def _unmapped_bond(item, payload, target, job_id):
    row, evidence = payload['catalog'], payload['evidence']
    issued = provider._date(row['发行日期'])
    registry_id = row['查询代码']
    sources = {
        'code': [provider._field_source('中国货币网官方登记ID（不是交易代码）', registry_id, '登记ID', evidence)],
        'name': [provider._field_source('债券简称', row['债券简称'], None, evidence)],
        'issuer': [provider._field_source('发行人/受托机构', row['发行人/受托机构'], None, evidence)],
        'issueDate': [provider._field_source('发行日期', issued, '日期', evidence)],
    }
    return dict(code=item['code'], bondId=item['code'], codes=[item['code']],
                name=row['债券简称'], issuer=row['发行人/受托机构'],
                registryId=registry_id, tradingCode=None, codeKind='official_registry_id',
                identityBasis='official_registry_id', source='akshare',
                regionId=provider._region(row['发行人/受托机构']), bondType=None,
                issueDate=issued, maturityDate=None, remainingYears=None, termYears=None,
                issueAmountYi=None, outstandingBalanceYi=None, couponPct=None, yieldPct=None,
                yieldDate=None, yieldMetric='ytm', yieldPriceBasis='latest_trade',
                duration=None, closeNetPrice=None, tradeNetPrice=None, currency=None,
                cohort=provider.COHORTS[0 if issued < '2025-08-08' else 1] if issued else None,
                disposition='incomplete', reason='未公布交易代码；仅保留中国货币网官方登记ID，个券基本信息未取得',
                missingFields=['交易代码', '个券基本信息'], validationErrors=[],
                fieldSources=sources, sourceUnits={}, requestIds=[evidence['requestId']],
                sessionIds=[evidence.get('runId')], substitutions=[],
                referenceFields=dict(indexYieldPct=None, indexDurationYears=None, curveYieldPct=None,
                                     adjustedYieldReferencePct=None, spreadReferenceBp=None,
                                     adjustedSpreadReferenceBp=None, durationSizeReference=None,
                                     issueSizeReferenceYi=None, yieldReferenceKind=None),
                earlyRepayment=None, redemption=None, staticSyncStatus='unmapped',
                catalogSource=evidence, syncJobId=job_id)


def materialize(job_id):
    """Publish batches of the known catalog, preserving dated market observations."""
    with provider.dataset_lock():
        return _materialize(job_id)


def _materialize(job_id):
    global _PUBLICATION_CACHE
    from . import akshare_read_model as index

    job = _progress(job_id)
    target = job['targetDate']
    items = provider._read_rows('SELECT i.code,i.status,i.payload,d.payload AS detail_payload,d.evidence '
                                'FROM akshare_sync_items i LEFT JOIN akshare_details d ON d.code=i.code '
                                'WHERE i.job_id=? ORDER BY i.code', (job_id,))
    if not items:
        return
    revision = provider._read_rows('SELECT version FROM akshare_dataset_index WHERE target_date=?', (target,))
    version = revision[0]['version'] if revision else None
    cache_key = (str(store.DB.resolve()), job_id, target)
    cache = _PUBLICATION_CACHE
    reusable = cache is not None and cache['key'] == cache_key and cache['version'] == version
    previous = cache['dataset'] if reusable else provider.read_available(target, True)
    trades, trade_sources, trade_conflicts = provider.cached_trade_observations(target)
    saved_metrics = provider.load_saved_metrics(target)
    benchmarks = previous.get('benchmarks', {})
    benchmark_evidence = {}
    for key in ('indexYieldPct', 'indexDurationYears', 'curveYieldPct'):
        source = next((sources[0] for bond in previous['bonds'] if (sources := bond.get('fieldSources', {}).get(key))), None)
        if source:
            benchmark_evidence[key] = _source_evidence(source)
    environment = provider._dump((benchmarks, benchmark_evidence, provider.RULES))
    old_inputs = cache['inputs'] if reusable and cache['environment'] == environment else {}
    # Compare raw inputs before parsing details or re-normalizing fields. New
    # same-day observations/conflicts participate, even without a detail change.
    market_inputs = {code: provider._dump((trades.get(code), trade_sources.get(code), code in trade_conflicts))
                     for code in trades.keys() | trade_conflicts}
    bonds = {bond['code']: bond for bond in previous['bonds']}
    changed, inputs = [], {}
    market_observed, market_with_details = 0, 0
    for item in items:
        code = item['code']
        digest = sha256()
        for value in (item['status'], item['payload'], item['detail_payload'], item['evidence'], market_inputs.get(code),
                      provider._dump(saved_metrics.get(code, []))):
            digest.update((value or '').encode('utf-8'))
            digest.update(b'\0')
        fingerprint = digest.hexdigest()
        old_input = old_inputs.get(code)
        if old_input and old_input[0] == fingerprint and code in bonds:
            inputs[code] = old_input
            market_observed += old_input[1]
            market_with_details += old_input[2]
            continue
        payload = json.loads(item['payload'])
        catalog = payload['catalog']
        if code.startswith('CFETS:'):
            bonds[code] = _unmapped_bond(item, payload, target, job_id)
            changed.append(bonds[code])
            inputs[code] = (fingerprint, 0, 0)
            continue
        detail = json.loads(item['detail_payload']) if item['detail_payload'] else None
        detail_valid = detail is not None and _valid_detail(item['code'], detail)
        if not detail_valid:
            detail = dict(bondCode=catalog['债券代码'], bondName=catalog['债券简称'], bondType='地方政府债',
                          entyFullName=catalog['发行人/受托机构'], issueDate=catalog['发行日期'])
        evidence = _brief(json.loads(item['evidence'])) if detail_valid else payload['evidence']
        old = bonds.get(item['code'])
        observation, trade_evidence = provider.select_trade_observation(
            old, trades.get(item['code']), trade_sources.get(item['code']), target,
            conflicted=item['code'] in trade_conflicts)
        if observation is not None:
            market_observed += 1
            market_with_details += int(detail_valid)
        inputs[code] = (fingerprint, int(observation is not None), int(observation is not None and detail_valid))
        bond = provider.normalize_bond(detail, target, observation, benchmarks, evidence, trade_evidence,
                                       provider._benchmark_sources_for_bond(old, benchmark_evidence), saved_metrics.get(code, []))
        bond.update(staticSyncStatus=item['status'], catalogSource=payload['evidence'], syncJobId=job_id)
        if not detail_valid:
            bond['reason'] = '基本信息同步失败，仍保留名单身份' if item['status'] == 'failed' else '已取得地方政府债名单，基本信息等待同步'
        bonds[item['code']] = bond
        changed.append(bond)
    provenance = dict(previous.get('provenance') or {})
    provenance.update(source='akshare', complete=False, origin='market_sync', collectedAt=store.now(),
                       syncJobId=job_id, universe='market', catalogComplete=job['catalogComplete'],
                       marketDataComplete=False, staticCompleted=job['completed'], catalogCount=job['discovered'],
                       dictionaryUrl=provider.DOC, scope='中国货币网按官方发行年份枚举查询的地方政府债；发行日期不晚于查询日')
    warnings = [warning for warning in previous.get('warnings', []) if not warning.startswith('全市场名单同步：')]
    warnings.append(f"全市场名单同步：已发现 {job['discovered']} 只，基本信息完成 {job['completed']} 只，失败 {job['failed']} 只；行情覆盖仍为部分。")
    dataset = provider._dataset(target, list(bonds.values()), benchmarks, provenance, warnings)
    published_job = _update_job(job_id, expected_generation=job['generation'], expected_states=(job['status'],),
                                marketObserved=market_observed, marketWithDetails=market_with_details,
                                marketEligible=dataset['counts']['eligible'],
                                groupingBasis=provider.RULES['groupingBasis'], rulesVersion=provider.RULES['version'])
    dataset['fullSync'] = published_job
    new_version = index.save_delta(dataset, changed, version,
        validate=lambda db: _assert_owner(db, job_id, job['generation'], (job['status'],)))
    # Do not publish cache state before the transaction succeeds. Failed/stale
    # batches must be retried against the last durable dataset and input set.
    _PUBLICATION_CACHE = dict(key=cache_key, version=new_version, dataset=dataset,
                              environment=environment, inputs=inputs)
    return dict(changedBonds=len(changed), totalBonds=len(bonds))


def _pending_detail_batch(job):
    trades, _, conflicts = provider.cached_trade_observations(job['targetDate'])
    items = provider._read_rows("SELECT * FROM akshare_sync_items WHERE job_id=? "
                                "AND status IN ('pending','running','failed') AND attempts<?", (job['jobId'], MAX_ATTEMPTS))
    def priority(item):
        trade = trades.get(item['code']) if item['code'] not in conflicts else None
        if trade is None:
            return 2, item['code']
        match = re.fullmatch(r'(\d+(?:\.\d+)?)Y', str(trade.get('termToMaturity', '')))
        rounded = int(Decimal(match[1]).quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if match else None
        # This term is a queue hint only. Eligibility is recalculated from the
        # documented maturity date after the actual detail is available.
        return (0 if rounded in provider.TERMS else 1), item['code']
    return sorted(items, key=priority)[:PUBLISH_EVERY]


def _sync_detail_item(job, item):
    job_id = job['jobId']
    code, payload = item['code'], json.loads(item['payload'])
    _check(job_id, job['generation'])
    # Another task may have populated this detail since catalog discovery.
    cached = provider._read_rows('SELECT payload FROM akshare_details WHERE code=?', (code,))
    if cached and _valid_detail(code, json.loads(cached[0]['payload'])):
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            _assert_owner(db, job_id, job['generation'])
            db.execute("UPDATE akshare_sync_items SET status='completed',error=NULL,updated_at=? WHERE job_id=? AND code=?", (store.now(), job_id, code))
        return
    for attempt in range(item['attempts']+1, MAX_ATTEMPTS+1):
        _check(job_id, job['generation'])
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            _assert_owner(db, job_id, job['generation'])
            db.execute("UPDATE akshare_sync_items SET status='running',attempts=?,updated_at=? WHERE job_id=? AND code=?", (attempt, store.now(), job_id, code))
        _update_owned(job, phase=f'同步基本信息 · {payload["catalog"]["债券简称"]} ({code})')
        recorder = _recorder(job, 60, f'同步基本信息 · {code}')
        try:
            rows, entry = recorder.query('bond_info_detail_cm', {'symbol': payload['catalog']['债券简称']},
                                          ['name', 'value'], compatibility=True,
                                          resolved_lookup=[payload['catalog']])
            error = _accept_detail_result(job, item, {'rows': rows, 'evidence': _brief(entry)})
            if error:
                raise ValueError(error)
            return
        except SyncPaused:
            raise
        except Exception as exc:
            with store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                _assert_owner(db, job_id, job['generation'])
                db.execute("UPDATE akshare_sync_items SET status='failed',error=?,updated_at=? WHERE job_id=? AND code=?",
                           (f'{type(exc).__name__}: {str(exc)[:300]}', store.now(), job_id, code))
            _raise_if_denied(recorder)
            _update_owned(job, message=f'{code} 基本信息失败（{attempt}/{MAX_ATTEMPTS}），已保存错误和进度。')
            _backoff(job, attempt)


def _details_serial(job):
    while True:
        _check(job['jobId'], job['generation'])
        batch = _pending_detail_batch(job)
        if not batch:
            return
        checkpoint_started = time.monotonic()
        for item in batch:
            _sync_detail_item(job, item)
            if time.monotonic()-checkpoint_started >= PUBLISH_SECONDS:
                break
        materialize(job['jobId'])
        # Refresh both observations and the queue at every publication, so
        # newly saved market observations gain priority without a worker restart.


def _prepare_detail(job, item):
    """Claim one attempt, or accept a concurrently populated valid cache."""
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        _assert_owner(db, job['jobId'], job['generation'])
        current = db.execute('SELECT status,attempts FROM akshare_sync_items WHERE job_id=? AND code=?',
                             (job['jobId'], item['code'])).fetchone()
        if current['status'] == 'completed' or current['attempts'] >= MAX_ATTEMPTS:
            return None
        cached = db.execute('SELECT payload FROM akshare_details WHERE code=?', (item['code'],)).fetchone()
        if cached and _valid_detail(item['code'], json.loads(cached['payload'])):
            db.execute("UPDATE akshare_sync_items SET status='completed',error=NULL,updated_at=? WHERE job_id=? AND code=?",
                       (store.now(), job['jobId'], item['code']))
            return None
        attempt = current['attempts'] + 1
        db.execute("UPDATE akshare_sync_items SET status='running',attempts=?,updated_at=? WHERE job_id=? AND code=?",
                   (attempt, store.now(), job['jobId'], item['code']))
    return attempt


def _accept_detail_result(job, item, result):
    """Commit validated detail and progress together under current ownership."""
    detail = None
    error = result.get('error')
    if not error:
        try:
            detail = {row['name']: row['value'] for row in result['rows']}
            catalog = json.loads(item['payload'])['catalog']
            if not _valid_detail(item['code'], detail) or detail['bondName'] != catalog['债券简称']:
                raise ValueError('详情身份或必需基本信息缺失，不能记为同步完成')
            if result['evidence'].get('runId') != job['jobId']:
                raise ValueError('详情查询证据不属于当前同步任务')
        except (KeyError, TypeError, ValueError) as exc:
            error = f'{type(exc).__name__}: {str(exc)[:300]}'
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        _assert_owner(db, job['jobId'], job['generation'])
        if not error:
            evidence = result['evidence']
            db.execute('INSERT OR REPLACE INTO akshare_details VALUES (?,?,?,?)',
                       (item['code'], evidence.get('retrievedAt') or store.now(),
                        provider._dump(detail), provider._dump(evidence)))
        db.execute('UPDATE akshare_sync_items SET status=?,error=?,updated_at=? WHERE job_id=? AND code=?',
                   ('failed' if error else 'completed', error, store.now(), job['jobId'], item['code']))
    return error


def _details_parallel(job):
    from .akshare_query_pool import DetailQueryPool

    with DetailQueryPool(DETAIL_WORKERS) as pool:
        while True:
            _check(job['jobId'], job['generation'])
            batch = _pending_detail_batch(job)
            if not batch:
                return
            queue, active = deque(batch), {}
            checkpoint_started = time.monotonic()
            while queue or active:
                _check(job['jobId'], job['generation'])
                if pool.denied.value:
                    raise RuntimeError(f'公开数据源返回 HTTP {pool.denied.value}，已停止本批请求并保留断点；稍后可继续同步')
                publishing = time.monotonic()-checkpoint_started >= PUBLISH_SECONDS
                while queue and len(active) < DETAIL_WORKERS and not publishing:
                    item = queue.popleft()
                    attempt = _prepare_detail(job, item)
                    if attempt is None:
                        continue
                    catalog = json.loads(item['payload'])['catalog']
                    _update_owned(job, phase=f'同步基本信息 · {catalog["债券简称"]} ({item["code"]}) · {DETAIL_WORKERS} 路查询')
                    future = pool.submit(job, catalog)
                    active[future] = (item, attempt)
                if not active:
                    break
                done, _ = wait(active, timeout=.2, return_when=FIRST_COMPLETED)
                for future in done:
                    item, attempt = active.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {'error': f'{type(exc).__name__}: {str(exc)[:300]}'}
                    if result.get('denied') or pool.denied.value:
                        _accept_detail_result(job, item, {'error': result.get('error') or '公开数据源拒绝请求'})
                        raise RuntimeError(f'公开数据源返回 HTTP {result.get("denied") or pool.denied.value}，已停止本批请求并保留断点；稍后可继续同步')
                    if result.get('cancelled'):
                        raise SyncPaused()
                    error = _accept_detail_result(job, item, result)
                    if error:
                        _update_owned(job, message=f'{item["code"]} 基本信息失败（{attempt}/{MAX_ATTEMPTS}），已保存错误和进度。')
                        _backoff(job, attempt)
                        if attempt < MAX_ATTEMPTS:
                            queue.appendleft(item)
            materialize(job['jobId'])
            # Drain the bounded in-flight calls before publishing, then re-rank the
            # remaining catalog from newly saved same-date observations.


def _details(job):
    if DETAIL_WORKERS == 1:
        return _details_serial(job)
    return _details_parallel(job)


@contextmanager
def _worker_lock(job_id):
    store.DB.parent.mkdir(parents=True, exist_ok=True)
    with open(str(store.DB)+'.akshare-sync.lock', 'a+b') as lock:
        lock.seek(0)
        lock.write(b'0')
        lock.flush()
        lock.seek(0)
        acquired = False
        try:
            while not acquired:
                if _get_job(job_id)['status'] == 'paused':
                    raise SyncPaused()
                try:
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (OSError, BlockingIOError):
                    time.sleep(.3)
            yield
        finally:
            if acquired:
                lock.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_UN)


def execute(job_id):
    initialize()
    job = _get_job(job_id)
    try:
        with _worker_lock(job_id):
            job = _get_job(job_id)
            if job['status'] in ('completed', 'partial', 'failed', 'paused'):
                return _progress(job_id)
            with store.connection() as db:
                db.execute('BEGIN IMMEDIATE')
                _assert_owner(db, job_id, job['generation'], ('queued', 'running'))
                for table in ('akshare_sync_items', 'akshare_sync_partitions'):
                    db.execute(f"UPDATE {table} SET status='pending',attempts=MAX(attempts-1,0) WHERE job_id=? AND status='running'", (job_id,))
            _update_job(job_id, expected_generation=job['generation'], expected_states=('queued', 'running'),
                        status='running', phase='同步全市场地方政府债名单', message='后台同步运行中，可暂停并保留进度。')
            _catalog(job)
            _check(job_id, job['generation'])
            _details(job)
            _check(job_id, job['generation'])
            progress = _progress(job_id)
            complete = progress['catalogComplete'] and progress['failed'] == 0 and progress['pending'] == 0
            _update_owned(job, status='completed' if complete else 'partial', phase='同步完成' if complete else '部分同步完成',
                        message=f"名单与基本信息：已完成 {progress['completed']} 只，失败 {progress['failed']} 只。"
                                + ('名单分页已验证完整；行情仍为部分覆盖。' if complete else '可继续同步以重试失败项目，行情仍为部分覆盖。'))
            materialize(job_id)
    except SyncPaused:
        # A newer start() generation owns its own status; never overwrite it.
        try:
            materialize(job_id)
        except Exception:
            pass
    except Exception as exc:
        try:
            _update_owned(job, status='failed', phase='同步中断', message=f'{type(exc).__name__}: {str(exc)[:300]}；已保存进度，可继续。')
        except SyncPaused:
            pass
        try:
            materialize(job_id)
        except Exception:
            pass
    return _progress(job_id)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Resume an existing AKShare market sync job.')
    parser.add_argument('--job', required=True)
    args = parser.parse_args()
    print(provider._dump(execute(args.job)), flush=True)
