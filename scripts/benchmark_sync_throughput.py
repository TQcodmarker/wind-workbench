"""Offline throughput check of the real detail-sync loop with fixed I/O delay.

No public endpoint is called. The temporary database contains only generated
fixtures. A budget catches serialized I/O in the same loop used by the worker.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from unittest.mock import patch

from backend import akshare_provider as provider, akshare_sync as sync, storage as store


class DelayedRecorder:
    def __init__(self, run, timeout_seconds=240, on_response=None, **kwargs):
        self.run, self.callback = run, on_response
        self.before_request = kwargs.get('before_request')
        self.queries = []

    def query(self, name, arguments, columns, compatibility=False, resolved_lookup=None):
        if name != 'bond_info_detail_cm' or not compatibility or not resolved_lookup:
            raise AssertionError('Benchmark only exercises verified-catalog detail queries')
        if self.before_request:
            self.before_request(None)
        time.sleep(float(os.environ.get('AKSHARE_BENCHMARK_LATENCY', '.12')))
        row = resolved_lookup[0]
        detail = dict(bondCode=row['债券代码'], bondName=row['债券简称'], bondType='地方政府债',
                      bondFullName='2026年河北省政府一般债券', entyFullName='河北省人民政府',
                      issueDate='2026-04-20', mrtyDate='2036-04-21', issueAmnt='25', parCouponRate='1.85')
        entry = dict(requestId='fixture-'+str(uuid.uuid4()), runId=self.run['runId'], function=name,
                     arguments=arguments, retrievedAt=store.now(), executionMode='compatibility_adapter',
                     status='succeeded', responses=[dict(status=200, url='fixture://fixed-latency', body={})])
        self.queries.append(entry)
        if self.callback:
            self.callback(entry['responses'][0])
        with store.connection() as db:
            db.execute('INSERT INTO akshare_queries VALUES (?,?,?,?,?,?)',
                       (entry['requestId'], self.run['runId'], self.run['targetDate'],
                        entry['retrievedAt'], name, provider._dump(entry)))
        return [dict(name=key, value=value) for key, value in detail.items()], entry


def measure(rows=24, latency=.12, workers=None):
    os.environ['AKSHARE_BENCHMARK_LATENCY'] = str(latency)
    with tempfile.TemporaryDirectory() as directory, patch.object(store, 'DB', Path(directory)/'benchmark.sqlite3'), \
         patch.object(provider, 'QueryRecorder', DelayedRecorder):
        sync.initialize()
        job = sync.start('2026-09-11')
        job = sync._update_job(job['jobId'], status='running', catalogComplete=True)
        with store.connection() as db:
            for index in range(rows):
                code = str(800000+index)
                catalog = {'债券代码': code, '债券简称': '26河北'+str(index), '发行人/受托机构': '河北省人民政府',
                           '债券类型': '地方政府债', '发行日期': '2026-04-20', '查询代码': 'fixture-'+code}
                payload = dict(catalog=catalog, year='2026', evidence={'requestId': 'fixture-catalog', 'runId': job['jobId'], 'function': 'bond_info_cm'})
                db.execute('INSERT INTO akshare_sync_items VALUES (?,?,?,?,?,?,?)',
                           (job['jobId'], code+'.IB', 'pending', 0, provider._dump(payload), None, store.now()))
        started = time.perf_counter()
        if workers is None:
            sync._details(job)
        else:
            with patch.object(sync, 'DETAIL_WORKERS', workers, create=True):
                sync._details(job)
        elapsed = time.perf_counter()-started
        status = sync.get(job['jobId'])
        if status['completed'] != rows or status['failed'] or status['pending']:
            raise AssertionError('Benchmark did not complete the same verified detail workload')
        return dict(measuredAt=datetime.now().astimezone().isoformat(), fixtureRows=rows,
                    fixedNetworkDelaySeconds=latency, elapsedSeconds=round(elapsed, 4),
                    rowsPerSecond=round(rows/elapsed, 3), workers=workers or getattr(sync, 'DETAIL_WORKERS', 1),
                    completed=status['completed'], publicNetworkRequests=0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', type=int, default=24)
    parser.add_argument('--latency', type=float, default=.12)
    parser.add_argument('--workers', type=int, choices=(1, 2, 3))
    parser.add_argument('--budget', type=float, default=4.8)
    parser.add_argument('--no-assert', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = measure(args.rows, args.latency, args.workers)
    result.update(budgetSeconds=args.budget, passed=result['elapsedSeconds'] <= args.budget)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if not args.no_assert and not result['passed']:
        raise SystemExit('FAIL: detail sync exceeds the fixed-latency throughput budget')
