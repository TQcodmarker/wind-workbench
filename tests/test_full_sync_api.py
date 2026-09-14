"""Full-market AKShare synchronization is durable, asynchronous, and source-bound."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import storage as store
from backend import akshare_sync as sync
from backend.api import app


TARGET = '2026-09-10'


class FullSyncApiTests(unittest.TestCase):
    def test_startup_resumes_older_active_job_even_when_newest_job_is_paused(self):
        first=sync.start('2026-09-10')
        sync.pause(first['jobId'])
        newest=sync.start('2026-09-11')
        sync.pause(newest['jobId'])
        sync.resume(first['jobId'])
        self.assertEqual(sync.status()['jobId'],newest['jobId'])
        with patch('backend.api.spawn_sync_worker') as spawn,patch('backend.api.spawn_worker'):
            with TestClient(app):
                pass
        spawn.assert_called_once_with(first['jobId'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(store, 'DB', Path(self.temp.name) / 'sync.sqlite3'),
            patch.object(store, 'MODE', 'wind'),
            patch.dict('os.environ', {'BOND_DATA_SOURCE': 'akshare'}),
            patch('backend.api.spawn_sync_worker'),
            patch('backend.api.spawn_worker', side_effect=AssertionError('Unexpected legacy worker')),
            patch('backend.akshare_sync.execute', side_effect=AssertionError('Collection ran inside API request')),
            patch('backend.akshare_provider.QueryRecorder.query', side_effect=AssertionError('Unexpected live AKShare query')),
            patch('backend.wind_mcp.WindMCP.__aenter__', side_effect=AssertionError('Unexpected Wind query')),
            patch('requests.sessions.Session.send', side_effect=AssertionError('Unexpected external HTTP request')),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.spawn = self.mocks[3]
        self.execute = self.mocks[5]
        store.initialize(seed=False)
        sync.initialize()
        self.client = TestClient(app)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def start(self, target=TARGET):
        response = self.client.post('/api/akshare/sync', json={'targetDate': target})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def test_first_start_returns_a_saved_job_before_any_collection(self):
        job = self.start()
        self.assertEqual(job['targetDate'], TARGET)
        self.assertEqual(job['source'], 'akshare')
        self.assertEqual(job['universe'], 'market')
        self.assertEqual(job['status'], 'queued')
        self.assertIsNone(job['total'])
        self.assertFalse(job['catalogComplete'])
        self.assertFalse(job['marketDataComplete'])
        self.spawn.assert_called_once_with(job['jobId'])
        self.execute.assert_not_called()
        # The response is already durable even though the worker never ran.
        sync.initialize()
        status = TestClient(app).get('/api/akshare/sync', params={'target': TARGET}).json()
        self.assertEqual(status['jobId'], job['jobId'])
        self.assertEqual(status['status'], 'queued')
        self.assertEqual(store.runs(), [])

    def test_status_is_read_only_and_distinguishes_dates_without_a_job(self):
        self.assertIsNone(self.client.get('/api/akshare/sync').json())
        job = self.start()
        self.spawn.reset_mock()
        self.assertIsNone(self.client.get('/api/akshare/sync', params={'target': '2026-09-09'}).json())
        for _ in range(2):
            self.assertEqual(self.client.get('/api/akshare/sync').json()['jobId'], job['jobId'])
        self.spawn.assert_not_called()
        self.execute.assert_not_called()

    def test_repeated_start_reuses_the_active_job_without_resetting_identity(self):
        first = self.start()
        second = self.start()
        self.assertEqual(second['jobId'], first['jobId'])
        self.assertEqual(second['createdAt'], first['createdAt'])
        self.assertEqual(second['targetDate'], first['targetDate'])
        self.assertEqual(second['source'], 'akshare')
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM akshare_sync_jobs').fetchone()[0], 1)

    def test_pause_and_resume_keep_job_date_and_source_after_global_selection_changes(self):
        job = self.start()
        completed_payload = '{"code":"809336.IB","name":"已保存的地方债资料"}'
        with store.connection() as db:
            db.executemany('INSERT INTO akshare_sync_items VALUES (?,?,?,?,?,?,?)', [
                (job['jobId'], '809336.IB', 'completed', 1, completed_payload, None, store.now()),
                (job['jobId'], '809337.IB', 'failed', 3, '{"code":"809337.IB"}', '离线超时样例', store.now()),
            ])
        response = self.client.post(f"/api/akshare/sync/{job['jobId']}/pause")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'paused')
        self.assertEqual(response.json()['jobId'], job['jobId'])
        self.assertEqual(response.json()['completed'], 1)
        self.assertEqual(response.json()['failed'], 1)
        self.assertEqual(self.client.post('/api/source/provider', json={'provider': 'wind'}).status_code, 200)
        self.spawn.reset_mock()
        # The route identifies the original job; later UI selections cannot
        # rewrite its source or target date when work continues.
        resumed = self.client.post(f"/api/akshare/sync/{job['jobId']}/resume")
        self.assertEqual(resumed.status_code, 202, resumed.text)
        payload = resumed.json()
        self.assertEqual(payload['jobId'], job['jobId'])
        self.assertEqual(payload['targetDate'], TARGET)
        self.assertEqual(payload['source'], 'akshare')
        self.assertEqual(payload['createdAt'], job['createdAt'])
        self.assertEqual(payload['completed'], response.json()['completed'])
        self.assertEqual(payload['failed'], 0)
        self.assertEqual(payload['pending'], 1)
        self.assertEqual(self.client.get('/api/bootstrap').json()['mode'], 'wind')
        self.spawn.assert_called_once_with(job['jobId'])
        with store.connection() as db:
            saved = db.execute('SELECT status,attempts,payload FROM akshare_sync_items WHERE job_id=? AND code=?',
                               (job['jobId'], '809336.IB')).fetchone()
        self.assertEqual(tuple(saved), ('completed', 1, completed_payload))

    def test_explicit_sync_with_wind_selected_keeps_legacy_jobs_and_evidence(self):
        self.assertEqual(self.client.post('/api/source/provider', json={'provider': 'wind'}).status_code, 200)
        wind = store.enqueue(TARGET, provider='wind')
        with store.connection() as db:
            db.execute("INSERT INTO metadata VALUES ('wind_verification',?)", ('{"saved":true}',))
            original_dates = [tuple(row) for row in db.execute('SELECT * FROM valuation_dates')]
        job = self.start()
        self.assertEqual(job['source'], 'akshare')
        self.assertEqual(self.client.get('/api/bootstrap').json()['mode'], 'wind')
        self.assertEqual(store.get_run(wind['runId']), wind)
        self.assertEqual(self.client.get('/api/runs').json(), [wind])
        self.assertNotIn('wind_verification', json.dumps(job))
        with store.connection() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM valuation_dates')], original_dates)
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='wind_verification'").fetchone()[0], '{"saved":true}')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_requests').fetchone()[0], 0)

    def test_another_target_cannot_silently_retarget_an_existing_job(self):
        first = self.start()
        response = self.client.post('/api/akshare/sync', json={'targetDate': '2026-09-09'})
        self.assertIn(response.status_code, (202, 409), response.text)
        if response.status_code == 202:
            self.assertEqual(response.json()['targetDate'], '2026-09-09')
            self.assertNotEqual(response.json()['jobId'], first['jobId'])
        original = self.client.get('/api/akshare/sync', params={'target': TARGET}).json()
        self.assertEqual(original['jobId'], first['jobId'])
        self.assertEqual(original['targetDate'], TARGET)

    def test_invalid_dates_are_rejected_before_starting_a_worker(self):
        for target in ('not-a-date', '2026-9-10', '2026-08-31', '2099-01-01'):
            with self.subTest(target=target):
                response = self.client.post('/api/akshare/sync', json={'targetDate': target})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.client.get('/api/akshare/sync', params={'target': target}).status_code, 400)
        self.assertEqual(self.client.post('/api/akshare/sync', json={}).status_code, 422)
        self.spawn.assert_not_called()
        self.assertIsNone(sync.status())

    def test_unknown_pause_and_resume_return_not_found_without_starting_work(self):
        for action in ('pause', 'resume'):
            with self.subTest(action=action):
                response = self.client.post(f'/api/akshare/sync/missing-job/{action}')
                self.assertEqual(response.status_code, 404, response.text)
        self.spawn.assert_not_called()
        self.assertIsNone(sync.status())

    def test_demo_service_rejects_real_sync_without_mutating_existing_job(self):
        job = self.start()
        self.spawn.reset_mock()
        with patch.object(store, 'MODE', 'demo'):
            response = self.client.post('/api/akshare/sync', json={'targetDate': TARGET})
            self.assertEqual(response.status_code, 400, response.text)
            response = self.client.post(f"/api/akshare/sync/{job['jobId']}/resume")
            self.assertEqual(response.status_code, 400, response.text)
        self.spawn.assert_not_called()
        self.assertEqual(sync.status(TARGET)['jobId'], job['jobId'])


if __name__ == '__main__':
    unittest.main()
