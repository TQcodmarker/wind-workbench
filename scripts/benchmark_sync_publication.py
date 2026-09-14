"""Offline, reproducible benchmark of real sync.materialize on a temporary DB.

The generated catalog and detail responses are synthetic fixtures using the
documented field structure. No real database, service, or network is touched.
"""
import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch
from uuid import UUID

from backend import akshare_provider as provider, akshare_sync as sync, storage as store
from backend import akshare_read_model as index
from backend.domain import REGIONS

TARGET = '2026-09-11'
STAMP = '2026-09-12T09:00:00+08:00'
FIXTURE_VERSION = 'catalog-details-18540-v1'


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def catalog_row(position):
    region = REGIONS[position % len(REGIONS)]
    code = str(900000 + position)
    return {'债券代码': code, '债券简称': f'26{region["name"]}{position+1}',
            '发行人/受托机构': region['name'] + '人民政府', '债券类型': '地方政府债',
            '发行日期': '2026-04-20' if position % 2 else '2025-08-07',
            '查询代码': 'fixture-registry-' + code}


def detail_row(position, catalog):
    return dict(bondCode=catalog['债券代码'], bondName=catalog['债券简称'],
                bondType='地方政府债', bondFullName=catalog['发行人/受托机构'] +
                ('一般债券' if position % 2 else '专项债券'), entyFullName=catalog['发行人/受托机构'],
                issueDate=catalog['发行日期'], mrtyDate='2036-09-11',
                issueAmnt=str(10 + position % 80), parCouponRate='1.85')


def seed_catalog(job, count, traded_count):
    with store.connection() as db:
        rows = []
        for position in range(count):
            catalog = catalog_row(position)
            evidence = dict(requestId='fixture-catalog', runId=job['jobId'], function='bond_info_cm',
                            arguments={'bond_type': '地方政府债'}, retrievedAt=STAMP)
            payload = dict(catalog=catalog, year=catalog['发行日期'][:4], evidence=evidence)
            rows.append((job['jobId'], catalog['债券代码'] + '.IB', 'pending', 0,
                         provider._dump(payload), None, STAMP))
        db.executemany('INSERT INTO akshare_sync_items VALUES (?,?,?,?,?,?,?)', rows)
        raw_trades = [dict(bondcode=str(900000 + position), showDate=TARGET + ' 16:20:00',
                          dmiLatestContraRate='0' if position == 0 else str(1.5 + (position % 10) / 100),
                          dmiLatestRate='101.25', termToMaturity='10Y') for position in range(traded_count)]
        query = dict(requestId='fixture-trades', runId=job['jobId'], function='bond_spot_deal',
                     arguments={}, retrievedAt=STAMP, status='succeeded', source='akshare',
                     executionMode='documented_sdk', responses=[dict(status=200,
                         url='https://www.chinamoney.com.cn/ags/ms/cm-u-md-bond/CbtPri',
                         body={'records': raw_trades})])
        db.execute('INSERT INTO akshare_queries VALUES (?,?,?,?,?,?)',
                   ('fixture-trades', job['jobId'], TARGET, STAMP, 'bond_spot_deal', provider._dump(query)))


def add_details(job, begin, end):
    with store.connection() as db:
        for position in range(begin, end):
            catalog = catalog_row(position)
            detail = detail_row(position, catalog)
            evidence = dict(requestId='fixture-detail-' + catalog['债券代码'], runId=job['jobId'],
                            function='bond_info_detail_cm', arguments={'symbol': catalog['债券简称']},
                            retrievedAt=STAMP, executionMode='compatibility_adapter')
            code = catalog['债券代码'] + '.IB'
            db.execute('INSERT INTO akshare_details VALUES (?,?,?,?)',
                       (code, STAMP, provider._dump(detail), provider._dump(evidence)))
            db.execute("UPDATE akshare_sync_items SET status='completed',attempts=1 WHERE job_id=? AND code=?",
                       (job['jobId'], code))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=18540)
    parser.add_argument('--batch-size', type=int, default=250)
    parser.add_argument('--max-second-seconds', type=float, default=1.5)
    parser.add_argument('--output', type=Path, default=Path('research/sync-performance-v2/publication.json'))
    parser.add_argument('--compare', type=Path, help='Compare deterministic semantic hashes against a previous report')
    parser.add_argument('--no-assert', action='store_true')
    args = parser.parse_args()
    if args.batch_size < 1 or args.count < 2 * args.batch_size:
        raise SystemExit('Catalog must contain at least two positive detail batches.')
    report = dict(fixtureVersion=FIXTURE_VERSION, synthetic=True, target=TARGET,
                  count=args.count, batchSize=args.batch_size, maxSecondSeconds=args.max_second_seconds,
                  python=sys.version, executable=sys.executable, platform=platform.platform(),
                  timingsInclude='Real materialize: read, normalize, aggregate, serialize, SQLite save/index and commit. Fixture setup and correctness reads excluded.',
                  sourceHashes={name: sha256((store.ROOT / 'backend' / name).read_bytes()).hexdigest()
                                for name in ('akshare_provider.py', 'akshare_sync.py', 'akshare_read_model.py')}, phases=[])
    with ExitStack() as stack:
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix='bond-publication-'))
        stack.enter_context(patch.object(store, 'DB', Path(directory) / 'benchmark.sqlite3'))
        stack.enter_context(patch.object(store, 'MODE', 'wind'))
        stack.enter_context(patch.object(store, 'now', return_value=STAMP))
        stack.enter_context(patch('requests.Session.send', side_effect=AssertionError('Offline benchmark cannot send HTTP')))
        stack.enter_context(patch.object(provider.QueryRecorder, 'query', side_effect=AssertionError('Offline benchmark cannot collect')))
        store.initialize(seed=False)
        sync.initialize()
        with patch.object(sync.uuid, 'uuid4', return_value=UUID('12345678-1234-5678-1234-567812345678')):
            job = sync.start(TARGET)
        sync._update_job(job['jobId'], status='running', catalogComplete=True)
        seed_catalog(job, args.count, 2 * args.batch_size)
        for phase, detail_count in [('catalog_initial', 0), ('detail_batch_1', args.batch_size),
                                    ('detail_batch_2', 2 * args.batch_size)]:
            if detail_count:
                add_details(job, detail_count - args.batch_size, detail_count)
            began = time.perf_counter()
            sync.materialize(job['jobId'])
            seconds = time.perf_counter() - began
            data = provider.read_available(TARGET, True)
            summary = index.summary(TARGET)
            assert len(data['bonds']) == args.count, 'Publication lost catalog rows'
            assert data['counts']['eligible'] == detail_count, 'Completed traded detail count differs'
            assert summary['coverage'] == {'staticCompleted': detail_count, 'observed': 2 * args.batch_size}
            assert data['cells'] == provider.aggregate(data['bonds']), 'Saved aggregate differs from full recomputation'
            semantics = dict(bonds=data['bonds'], counts=data['counts'], cells=data['cells'],
                             coverage=summary['coverage'], benchmarks=data.get('benchmarks'))
            measurement = dict(phase=phase, seconds=round(seconds, 6), detailsCompleted=detail_count,
                               counts=data['counts'], coverage=summary['coverage'],
                               semanticSha256=digest(semantics), cellsSha256=digest(data['cells']))
            report['phases'].append(measurement)
            print(json.dumps(measurement, ensure_ascii=False), flush=True)
        report['finalBondHashes'] = {bond['code']: digest(bond) for bond in data['bonds']}
        with store.connection() as db:
            report['finalDatabaseBytes'] = db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
            report['finalDatasetCharacters'] = db.execute('SELECT length(payload) FROM akshare_datasets WHERE target_date=?', (TARGET,)).fetchone()[0]
        report['secondBatchWithinBudget'] = report['phases'][-1]['seconds'] < args.max_second_seconds
    if args.compare:
        reference = json.loads(args.compare.read_text(encoding='utf-8'))
        if (reference['fixtureVersion'], reference['count'], reference['batchSize']) != (FIXTURE_VERSION, args.count, args.batch_size):
            raise AssertionError('Reference fixture parameters differ.')
        differences = [current['phase'] for current, before in zip(report['phases'], reference['phases'])
                       if current['semanticSha256'] != before['semanticSha256']]
        changed_codes = [code for code in set(report['finalBondHashes']) | set(reference['finalBondHashes'])
                         if report['finalBondHashes'].get(code) != reference['finalBondHashes'].get(code)]
        report['comparison'] = dict(reference=str(args.compare), differentPhases=differences,
                                  differentBondCount=len(changed_codes), differentCodes=sorted(changed_codes)[:100])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(report=str(args.output), secondBatchWithinBudget=report['secondBatchWithinBudget'],
                          comparison=report.get('comparison')), ensure_ascii=False), flush=True)
    if report.get('comparison', {}).get('differentPhases'):
        raise SystemExit('Publication semantics differ from the reference report.')
    if not args.no_assert and not report['secondBatchWithinBudget']:
        raise SystemExit('Second detail batch exceeded the 1.5-second publication budget.')


if __name__ == '__main__':
    main()
