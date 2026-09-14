"""Real spawn-process regressions; every request and database is a fixture."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from backend import akshare_provider as provider, akshare_sync as sync, storage as store
from backend.akshare_query_pool import DetailQueryPool


class SpawnRecorder:
    """Pickleable SDK substitute that records actual process/request timing."""
    def __init__(self, run, timeout_seconds=240, on_response=None, before_request=None):
        self.run, self.on_response, self.before_request = run, on_response, before_request
        self.queries = []

    def query(self, name, arguments, columns, compatibility=False, resolved_lookup=None):
        assert name == 'bond_info_detail_cm' and columns == ['name', 'value']
        assert compatibility and len(resolved_lookup) == 1
        row = resolved_lookup[0]
        assert arguments == {'symbol': row['债券简称']}
        entry = dict(requestId='spawn-fixture-'+str(uuid.uuid4()), runId=self.run['runId'],
                     function=name, arguments=arguments, retrievedAt=store.now(),
                     executionMode='compatibility_adapter', responses=[])
        try:
            self.before_request(None)
            started = time.monotonic()
            # The marker table only exists in the explicitly assigned test DB.
            with store.connection() as db:
                db.execute('INSERT INTO fixture_starts VALUES (?,?,?)', (row['债券代码'], os.getpid(), started))
            time.sleep(row.get('fixtureDelay', .04))
            response = dict(status=429 if row.get('fixtureAction') == 'denied' else 200,
                            url='fixture://detail', body={'pid': os.getpid(), 'started': started,
                                                         'db': str(store.DB), 'target': self.run['targetDate']})
            entry['responses'].append(response)
            self.on_response(response)
            if row.get('fixtureAction') == 'fail':
                raise TimeoutError('fixture transient failure')
            code = '123456' if row.get('fixtureAction') == 'wrong_identity' else row['债券代码']
            detail = dict(bondCode=code, bondName=row['债券简称'], bondType='地方政府债',
                          bondFullName='2026年河北省政府一般债券', entyFullName='河北省人民政府',
                          issueDate='2026-04-20', mrtyDate='2036-04-21', issueAmnt='25', parCouponRate='1.85',
                          couponType='附息式固定利率', couponFrqncy='年',
                          frstValueDate='2026-04-21', frstCpnDt='2027-04-21')
            entry.update(status='succeeded')
            return [dict(name=key, value=value) for key, value in detail.items()], entry
        except Exception:
            entry['status'] = 'failed'
            raise
        finally:
            self.queries.append(entry)
            with store.connection() as db:
                db.execute('INSERT INTO akshare_queries VALUES (?,?,?,?,?,?)',
                           (entry['requestId'], self.run['runId'], self.run['targetDate'],
                            entry['retrievedAt'], name, provider._dump(entry)))


class SpawnDetailSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name)/'explicit-child-db.sqlite3'
        self.patches = [patch.object(store, 'DB', self.db_path),
                        patch.object(provider, 'QueryRecorder', SpawnRecorder),
                        patch.object(sync, 'DETAIL_WORKERS', 3)]
        for value in self.patches:
            value.start()
        sync.initialize()
        self.job = sync.start('2026-09-11')
        self.job = sync._update_job(self.job['jobId'], status='running', catalogComplete=True)
        with store.connection() as db:
            db.execute('CREATE TABLE fixture_starts (code TEXT,pid INTEGER,started REAL)')
            db.execute('CREATE TABLE wind_preserve (value TEXT)')
            db.execute("INSERT INTO wind_preserve VALUES ('existing Wind data')")
        self.children_before = {process.pid for process in multiprocessing.active_children()}

    def tearDown(self):
        for value in reversed(self.patches):
            value.stop()
        self.temp.cleanup()
        self.assertEqual({process.pid for process in multiprocessing.active_children()}, self.children_before)

    def add_items(self, count, **options):
        with store.connection() as db:
            for index in range(count):
                code = str(810000+index)
                catalog = {'债券代码': code, '债券简称': '26河北'+str(index), '发行人/受托机构': '河北省人民政府',
                           '债券类型': '地方政府债', '发行日期': '2026-04-20', '查询代码': 'fixture-'+code, **options}
                payload = dict(catalog=catalog, year='2026', evidence={'requestId': 'fixture-catalog', 'runId': self.job['jobId'], 'function': 'bond_info_cm'})
                db.execute('INSERT INTO akshare_sync_items VALUES (?,?,?,?,?,?,?)',
                           (self.job['jobId'], code+'.IB', 'pending', 0, provider._dump(payload), None, store.now()))

    def wait_for_starts(self, count=1):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            rows = provider._read_rows('SELECT * FROM fixture_starts')
            if len(rows) >= count:
                return rows
            time.sleep(.01)
        self.fail('Spawn worker did not begin a fixture request')

    def test_three_processes_share_request_gate_and_only_explicit_db_evidence(self):
        self.add_items(8, fixtureDelay=.36)
        sync._details(self.job)
        starts = provider._read_rows('SELECT * FROM fixture_starts ORDER BY started')
        self.assertEqual(len({row['pid'] for row in starts}), 3)
        self.assertNotIn(os.getpid(), {row['pid'] for row in starts})
        self.assertTrue(all(right['started']-left['started'] >= .135 for left, right in zip(starts, starts[1:])))
        self.assertEqual(sync.get(self.job['jobId'])['completed'], 8)
        for record in provider._read_rows('SELECT * FROM akshare_queries'):
            self.assertEqual(record['run_id'], self.job['jobId'])
            self.assertEqual(record['target_date'], '2026-09-11')
            self.assertEqual(json.loads(record['payload'])['responses'][0]['body']['db'], str(self.db_path.resolve()))
        self.assertEqual(provider._read_rows('SELECT value FROM wind_preserve'), [{'value': 'existing Wind data'}])

    def test_pause_then_resume_discards_old_child_results_and_preserves_generation(self):
        self.add_items(8, fixtureDelay=.6)
        with ThreadPoolExecutor(max_workers=1) as supervisor:
            future = supervisor.submit(sync._details, self.job)
            self.wait_for_starts(3)
            sync.pause(self.job['jobId'])
            resumed = sync.resume(self.job['jobId'])
            with self.assertRaises(sync.SyncPaused):
                future.result(timeout=5)
        current = sync.get(self.job['jobId'])
        self.assertEqual((current['generation'], current['status']), (resumed['generation'], 'queued'))
        self.assertEqual(provider._read_rows('SELECT * FROM akshare_details'), [])
        self.assertEqual(current['completed'], 0)
        self.assertEqual(current['pending'], 8)

    def test_denied_request_stops_scheduling_with_at_most_three_in_flight(self):
        self.add_items(8, fixtureAction='denied', fixtureDelay=.4)
        with self.assertRaisesRegex(RuntimeError, 'HTTP 429'):
            sync._details(self.job)
        self.assertLessEqual(len(provider._read_rows('SELECT * FROM fixture_starts')), 3)
        self.assertEqual(provider._read_rows('SELECT * FROM akshare_details'), [])
        untouched = provider._read_rows("SELECT * FROM akshare_sync_items WHERE status='pending' AND attempts=0")
        self.assertGreaterEqual(len(untouched), 5)

    def test_failed_queries_retry_three_times_and_invalid_identity_never_succeeds(self):
        self.add_items(1, fixtureAction='fail')
        with patch.object(sync, '_backoff') as backoff:
            sync._details(self.job)
        self.assertEqual(sync.get(self.job['jobId'])['failed'], 1)
        self.assertEqual(provider._read_rows('SELECT attempts FROM akshare_sync_items')[0]['attempts'], 3)
        self.assertEqual(backoff.call_count, 3)
        self.assertEqual(provider._read_rows('SELECT * FROM akshare_details'), [])
        item = provider._read_rows('SELECT * FROM akshare_sync_items')[0]
        bad = {'rows': [{'name': 'bondCode', 'value': '123456'}], 'evidence': {'runId': self.job['jobId']}}
        self.assertIsNotNone(sync._accept_detail_result(self.job, item, bad))

    def test_successful_result_cannot_write_detail_after_ownership_changes(self):
        self.add_items(1)
        item = provider._read_rows('SELECT * FROM akshare_sync_items')[0]
        catalog = json.loads(item['payload'])['catalog']
        with DetailQueryPool(3, recorder_factory=SpawnRecorder) as pool:
            result = pool.submit(self.job, catalog).result(timeout=5)
        self.assertNotIn('error', result)
        sync.pause(self.job['jobId'])
        sync.resume(self.job['jobId'])
        with self.assertRaises(sync.SyncPaused):
            sync._accept_detail_result(self.job, item, result)
        self.assertEqual(provider._read_rows('SELECT * FROM akshare_details'), [])

    def test_two_workers_remain_supported_and_larger_pools_are_rejected(self):
        self.add_items(2, fixtureDelay=.2)
        catalogs = [json.loads(item['payload'])['catalog'] for item in provider._read_rows('SELECT * FROM akshare_sync_items')]
        with DetailQueryPool(2, recorder_factory=SpawnRecorder) as pool:
            futures = [pool.submit(self.job, catalog) for catalog in catalogs]
            self.assertTrue(all('error' not in future.result(timeout=5) for future in futures))
        self.assertEqual(len({row['pid'] for row in provider._read_rows('SELECT * FROM fixture_starts')}), 2)
        with self.assertRaises(ValueError):
            DetailQueryPool(4)


class RequestGateHookTests(unittest.TestCase):
    def test_gate_runs_before_each_sdk_request_and_can_cancel_without_sending(self):
        import akshare
        import pandas as pd
        import requests

        events = []
        def sdk_query():
            for _ in range(2):
                requests.get('https://www.chinamoney.com.cn/ags/ms/cm-u-bond-md/fixture')
            return pd.DataFrame([{'fixture': 1}])

        def gate(request):
            events.append('gate')

        def sent(*args, **kwargs):
            events.append('send')
            response = requests.Response()
            response.status_code, response._content = 200, b'{}'
            return response

        with tempfile.TemporaryDirectory() as directory, patch.object(store, 'DB', Path(directory)/'hook.sqlite3'):
            sync.initialize()
            run = {'runId': 'hook-fixture', 'targetDate': '2026-09-11'}
            recorder = provider.QueryRecorder(run, before_request=gate)
            with patch.object(akshare, 'bond_spot_deal', sdk_query), patch.object(requests.Session, 'send', sent):
                recorder.query('bond_spot_deal', {}, ['fixture'])
            self.assertEqual(events, ['gate', 'send', 'gate', 'send'])
            events.clear()
            def cancelled(request):
                raise InterruptedError('fixture paused before send')
            recorder = provider.QueryRecorder(run, before_request=cancelled)
            with patch.object(akshare, 'bond_spot_deal', sdk_query), patch.object(requests.Session, 'send', sent):
                with self.assertRaises(InterruptedError):
                    recorder.query('bond_spot_deal', {}, ['fixture'])
            self.assertEqual(events, [])
            self.assertEqual(recorder.queries[0]['responses'], [])


if __name__ == '__main__':
    unittest.main()
