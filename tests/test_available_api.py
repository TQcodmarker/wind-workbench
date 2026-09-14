"""Saved-data routes never acquire data or publish an incomplete snapshot."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend import storage as store
from backend.api import app
from backend.lineage import Recorder


class AvailableApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(store, 'DB', Path(self.temp.name) / 'saved.sqlite3')
        self.mode_patch = patch.object(store, 'MODE', 'wind')
        self.db_patch.start()
        self.mode_patch.start()
        store.initialize(seed=False)
        # These regression tests exercise the explicitly selected legacy source.
        from backend.providers import select_provider
        select_provider('wind')
        run = store.enqueue('2026-09-11')
        recorder = Recorder('已保存的失败任务样本', '2026-09-11', run['runId'])
        rid = recorder.begin('tools/call', {'params': {'name': 'get_bond_market_data'}})
        table = {'columns': [{'name': 'Wind代码'}, {'name': '证券简称'},
                             {'name': '2026年9月11日的收盘价收益率', 'unit': '%'}],
                 'rows': [['809336.IB', '26河北23', 1.7691]]}
        recorder.finish(rid, {'result': {'content': [{'type': 'text', 'text': json.dumps(
            {'data': {'data': [table]}}, ensure_ascii=False)}]}}, http_status=200)
        store.fail(run['runId'], '后续名单查询失败')

    def tearDown(self):
        self.mode_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def database_state(self):
        with store.connection() as db:
            return {name: db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                    for name in ('job_runs', 'source_sessions', 'source_requests',
                                 'bond_observations', 'snapshots', 'aggregate_results')}

    def test_saved_data_routes_are_read_only_and_keep_full_snapshot_unpublished(self):
        before = self.database_state()
        with patch('backend.api.spawn_worker', side_effect=AssertionError('Cannot start collection')), \
             patch('backend.wind_mcp.WindMCP.__aenter__', side_effect=AssertionError('Cannot call Wind')):
            client = TestClient(app)
            response = client.get('/api/datasets/2026-09-11/available')
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload['evaluationDate'], '2026-09-11')
            self.assertEqual(payload['scope'], 'saved_sample')
            self.assertFalse(payload['complete'])
            self.assertEqual(payload['counts']['bonds'], 1)
            self.assertEqual(payload['counts']['eligible'], 0)
            self.assertEqual(payload['bonds'][0]['yieldPct'], '1.7691')
            self.assertEqual(payload['cells'], [])
            full = client.get('/api/datasets/2026-09-11').json()
            self.assertIsNone(full['snapshot'])
            self.assertEqual(full['latestAttempt']['status'], 'failed')
            boot = client.get('/api/bootstrap').json()
            self.assertIsNone(boot['latestDate'])
            self.assertEqual(boot['latestSavedDate'], '2026-09-11')
            self.assertEqual(boot['availableDates'], ['2026-09-11'])
        self.assertEqual(self.database_state(), before)

    def test_saved_data_endpoint_validates_dates_and_never_fills_adjacent_days(self):
        client = TestClient(app)
        for target in ('not-a-date', '2026-08-31', '2026-9-11'):
            self.assertEqual(client.get(f'/api/datasets/{target}/available').status_code, 400)
        response = client.get('/api/datasets/2026-09-10/available')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['counts']['bonds'], 0)
        self.assertEqual(response.json()['bonds'], [])


if __name__ == '__main__':
    unittest.main()
