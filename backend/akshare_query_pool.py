"""Bounded process isolation for AKShare's process-global SDK patches.

Children only acquire and persist query evidence. The owning sync process is
responsible for validating and atomically accepting each result. Shared ctypes
and synchronization primitives use the spawn context; no manager is started.
"""
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
from pathlib import Path
import time

from . import akshare_provider as provider, storage as store


class QueryStopped(Exception):
    pass


def _initialize_worker(db_path, mode, next_start, request_lock, stop, denied, interval, recorder_factory):
    global _next_start, _request_lock, _stop, _denied, _interval, _recorder_factory
    # Spawn re-imports storage with its default path. Override it explicitly
    # before any read or evidence write, including in offline regression tests.
    store.DB = Path(db_path)
    store.MODE = mode
    _next_start, _request_lock, _stop, _denied = next_start, request_lock, stop, denied
    _interval, _recorder_factory = interval, recorder_factory


def _check_owner(job):
    if _stop.is_set():
        raise QueryStopped('同步批次已停止')
    rows = provider._read_rows('SELECT payload FROM akshare_sync_jobs WHERE job_id=?', (job['jobId'],))
    current = json.loads(rows[0]['payload']) if rows else {}
    if current.get('generation') != job['generation'] or current.get('status') != 'running':
        raise QueryStopped('同步任务已暂停或被新一代继续任务替代')


def _before_request(job):
    # Space request starts across all processes. Time spent awaiting a response
    # counts toward the interval instead of adding another delay afterward.
    while True:
        _check_owner(job)
        with _request_lock:
            delay = _next_start.value - time.monotonic()
            if delay <= 0:
                _next_start.value = time.monotonic() + _interval
                return
        _stop.wait(min(delay, .05))


def _after_response(job, response):
    status = response.get('status')
    if status in (401, 403, 429):
        with _denied.get_lock():
            _denied.value = status
        _stop.set()
        raise QueryStopped(f'公开数据源返回 HTTP {status}')
    _check_owner(job)


def _query_detail(job, catalog):
    recorder = None
    try:
        _check_owner(job)
        recorder = _recorder_factory(
            {'runId': job['jobId'], 'source': 'akshare', 'targetDate': job['targetDate']},
            timeout_seconds=60, before_request=lambda request: _before_request(job),
            on_response=lambda response: _after_response(job, response))
        rows, entry = recorder.query('bond_info_detail_cm', {'symbol': catalog['债券简称']},
                                     ['name', 'value'], compatibility=True, resolved_lookup=[catalog])
        _check_owner(job)
        evidence = {key: entry.get(key) for key in ('requestId', 'runId', 'function', 'arguments',
                    'retrievedAt', 'executionMode', 'dictionaryUrl')}
        return {'rows': rows, 'evidence': evidence, 'denied': _denied.value}
    except Exception as exc:
        statuses = {response.get('status') for entry in (recorder.queries if recorder else [])
                    for response in entry.get('responses', [])}
        denied = min(statuses & {401, 403, 429}, default=_denied.value)
        if denied:
            with _denied.get_lock():
                _denied.value = denied
            _stop.set()
        return {'error': f'{type(exc).__name__}: {str(exc)[:300]}', 'denied': denied,
                'cancelled': isinstance(exc, QueryStopped)}


class DetailQueryPool:
    """At most three isolated SDK calls; callers also bound submitted work."""
    def __init__(self, workers=3, interval=.15, recorder_factory=None):
        if workers not in (1, 2, 3):
            raise ValueError('详情同步仅支持 1、2 或 3 个隔离查询进程')
        context = multiprocessing.get_context('spawn')
        self.stop_event = context.Event()
        self.denied = context.Value('i', 0)
        self.executor = ProcessPoolExecutor(
            max_workers=workers, mp_context=context, initializer=_initialize_worker,
            initargs=(str(store.DB.resolve()), store.MODE, context.Value('d', 0),
                      context.Lock(), self.stop_event, self.denied, interval,
                      recorder_factory or provider.QueryRecorder))

    def submit(self, job, catalog):
        if self.stop_event.is_set():
            if self.denied.value:
                raise RuntimeError(f'公开数据源返回 HTTP {self.denied.value}，已停止本批请求并保留断点；稍后可继续同步')
            raise QueryStopped('同步批次已停止')
        identity = {key: job[key] for key in ('jobId', 'targetDate', 'generation')}
        return self.executor.submit(_query_detail, identity, catalog)

    def close(self):
        self.stop_event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
