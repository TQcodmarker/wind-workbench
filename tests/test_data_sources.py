"""Provider selection isolates saved data and binds each queued job to its source."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import storage as store
from backend.api import app
from backend.domain import RULES, calculate
from backend.lineage import Recorder


TARGET = '2026-09-10'


class DataSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.object(store, 'DB', Path(self.temp.name) / 'sources.sqlite3'),
            patch.object(store, 'MODE', 'wind'),
            patch.dict('os.environ', {'BOND_DATA_SOURCE': 'akshare'}),
            patch('backend.api.wind_key', return_value=''),
        ]
        for item in self.patches:
            item.start()
        store.initialize(seed=False)
        self.client = TestClient(app)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def select(self, provider):
        response = self.client.post('/api/source/provider', json={'provider': provider})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/bootstrap').json()['mode'], provider)

    def save_wind(self):
        """Publish one offline full-grid fixture and preserve its individual evidence."""
        run = store.enqueue(TARGET, provider='wind')
        row = dict(bondId='809336', code='809336.IB', regionId='hebei',
                   bondType='general', issueDate='2025-08-07', valuationDate=TARGET,
                   yieldDate=TARGET, yieldMetric='ytm', yieldPriceBasis='close',
                   valueStatus='valid', yieldPct='1.7691', issueAmountYi='10',
                   remainingYears='10')
        recorder = Recorder('离线 Wind 隔离测试', TARGET, run['runId'])
        request_id = recorder.begin('tools/call', {'params': {'name': 'get_bond_market_data'}})
        table = {'columns': [{'name': 'Wind代码'}, {'name': '证券简称'},
                             {'name': '2026年9月10日的收盘价收益率', 'unit': '%'}],
                 'rows': [['809336.IB', '26河北23', '1.7691']]}
        recorder.finish(request_id, {'result': {'content': [{'type': 'text', 'text':
            json.dumps({'data': {'data': [table]}}, ensure_ascii=False)}]}}, http_status=200)
        store.publish(run['runId'], *calculate([row], TARGET), provenance={
            'source': 'wind', 'complete': True, 'evaluationDate': TARGET,
        })
        return run

    def test_fresh_real_database_defaults_to_akshare_without_wind_selection(self):
        self.assertEqual(self.client.get('/api/bootstrap').json()['mode'], 'akshare')
        source = self.client.get('/api/source').json()
        self.assertEqual(source['activeProvider'], 'akshare')
        self.assertIn('providers', source)
        self.assertEqual(self.client.get('/api/runs').json(), [])
        self.assertNotEqual(self.client.get('/api/rules').json()['version'], RULES['version'])

    def test_selection_survives_reinitialization_and_initial_default_changes(self):
        self.select('wind')
        # Initial environment defaults must not replace the user's saved choice.
        with patch.dict('os.environ', {'BOND_DATA_SOURCE': 'akshare', 'WIND_DATA_MODE': 'wind'}):
            store.initialize(seed=False)
            fresh_client = TestClient(app)
            self.assertEqual(fresh_client.get('/api/bootstrap').json()['mode'], 'wind')
        self.select('akshare')
        with patch.dict('os.environ', {'BOND_DATA_SOURCE': 'wind'}):
            store.initialize(seed=False)
            self.assertEqual(TestClient(app).get('/api/source').json()['activeProvider'], 'akshare')

    def test_switching_preserves_wind_history_and_akshare_reads_never_fall_back(self):
        run = self.save_wind()
        self.select('wind')
        saved = self.client.get(f'/api/datasets/{TARGET}').json()['snapshot']
        self.assertEqual(saved['publishedRunId'], run['runId'])
        wind_available = self.client.get(f'/api/datasets/{TARGET}/available').json()
        self.assertEqual(wind_available['counts']['bonds'], 1)

        with patch('backend.api.spawn_worker', side_effect=AssertionError('Read started collection')), \
             patch('backend.wind_mcp.WindMCP.__aenter__', side_effect=AssertionError('Unexpected Wind request')):
            self.select('akshare')
            boot = self.client.get('/api/bootstrap').json()
            self.assertIsNone(boot['latestDate'])
            self.assertIsNone(boot['latestSavedDate'])
            self.assertEqual(boot['availableDates'], [])
            day = self.client.get(f'/api/datasets/{TARGET}').json()
            self.assertIsNone(day['snapshot'])
            self.assertIsNone(day['latestAttempt'])
            available = self.client.get(f'/api/datasets/{TARGET}/available').json()
            self.assertEqual(available['bonds'], [])
            self.assertEqual(available['counts']['bonds'], 0)
            self.assertEqual(self.client.get('/api/runs').json(), [])
            detail = self.client.get(f'/api/datasets/{TARGET}/available/809336.IB')
            self.assertEqual(detail.status_code, 404)
            self.assertIsNone(self.client.post('/api/analysis', json={
                'context': {'date': TARGET}, 'action': 'query',
            }).json()['publishedRunId'])
            self.assertEqual(self.client.post('/api/analysis', json={
                'context': {'date': TARGET}, 'action': 'summary',
            }).json()['rows'], [])
            summary = self.client.get(f'/api/summaries/{TARGET}').json()
            self.assertIsNone(summary['publishedRunId'])
            self.assertEqual(summary['rows'], [])

            self.select('wind')
            restored = self.client.get(f'/api/datasets/{TARGET}').json()['snapshot']
            self.assertEqual(restored, saved)
            self.assertEqual(self.client.get(f'/api/datasets/{TARGET}/available').json(), wind_available)
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_requests').fetchone()[0], 1)

    def test_new_job_uses_selected_provider_without_modifying_wind_dates(self):
        with patch('backend.api.spawn_worker') as start:
            response = self.client.post('/api/runs', json={'targetDate': TARGET})
        self.assertEqual(response.status_code, 202, response.text)
        run = response.json()
        self.assertEqual(run['source'], 'akshare')
        self.assertNotEqual(run['rulesVersion'], RULES['version'])
        start.assert_called_once()
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM valuation_dates').fetchone()[0], 0)
        self.select('wind')
        self.assertEqual(store.get_run(run['runId'])['source'], 'akshare')
        self.assertIsNone(self.client.get(f'/api/datasets/{TARGET}').json()['latestAttempt'])

    def test_saved_akshare_sample_drives_analysis_without_becoming_wind_snapshot(self):
        from backend import akshare_provider as ak

        wind = self.save_wind()
        ak.initialize(seed=False)
        detail = dict(bondCode='809336', bondName='26河北23', bondType='地方政府债',
                      entyFullName='河北省人民政府', bondFullName='河北省政府一般债',
                      issueDate='2025-08-07', mrtyDate='2036-09-10',
                      issueAmnt='10', parCouponRate='1.89', couponType='附息式固定利率',
                      couponFrqncy='年', frstValueDate='2025-09-10', frstCpnDt='2026-09-10')
        trade = dict(bondcode='809336', showDate=TARGET, dmiLatestContraRate='2.5',
                     dmiLatestRate='100')
        bond = ak.normalize_bond(detail, TARGET, trade)
        ak._save_dataset(ak._dataset(TARGET, [bond], provenance={
            'source': 'akshare', 'complete': False, 'runId': 'offline-akshare',
            'collectedAt': '2026-09-10T17:00:00+08:00',
        }))
        context = {'date': TARGET, 'region': '河北', 'term': 10, 'scope': 'general'}
        with patch('backend.api.spawn_worker', side_effect=AssertionError('Read started collection')), \
             patch('backend.wind_mcp.WindMCP.__aenter__', side_effect=AssertionError('Unexpected Wind request')):
            analysis = self.client.post('/api/analysis', json={'context': context}).json()
            self.assertEqual(analysis['publishedRunId'], 'offline-akshare')
            self.assertEqual(len(analysis['rows']), 1)
            self.assertEqual(float(analysis['rows'][0]['yieldPct']), 2.5)
            self.assertIsNone(self.client.get(f'/api/datasets/{TARGET}').json()['snapshot'])
            self.select('wind')
            analysis = self.client.post('/api/analysis', json={'context': context}).json()
            self.assertEqual(analysis['publishedRunId'], wind['runId'])
            self.assertEqual(float(analysis['rows'][0]['yieldPct']), 1.7691)
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM akshare_datasets').fetchone()[0], 1)

    def test_assistant_summary_keeps_available_dates_when_parsing_prompt_dates(self):
        from backend import akshare_provider as ak
        from backend.analysis import respond

        def read(target):
            rate = '2.5' if target == TARGET else '2.4'
            snapshot = dict(
                source='akshare', publishedRunId='sample-' + target, publishedAt=target,
                rulesVersion=ak.RULES['version'], mappingVersion=ak.RULES['mappingVersion'],
                yieldDefinition=ak.YIELD_DEFINITION,
                regions=[{'id': 'hebei', 'name': '河北省', 'tier': 1}],
                cells=[dict(cohort='before_20250808', regionId='hebei', termYears=10,
                            bondScope='general', yieldPct=rate, sampleCount=1,
                            issueAmountSumYi='10', weightedYieldSum='25' if target == TARGET else '24')],
            )
            return dict(source='akshare', snapshot=snapshot, latestAttempt=None, dataState='ready')

        for prompt in ('', '生成2026年9月10日摘要'):
            with self.subTest(prompt=prompt):
                result = respond({
                    'action': 'summary', 'prompt': prompt,
                    'context': {'date': TARGET, 'region': '河北', 'term': 10, 'scope': 'general'},
                }, reader=read, dates=[TARGET, '2026-09-09'])
                self.assertEqual(result['baseDate'], '2026-09-09')
                self.assertEqual(result['baseRunId'], 'sample-2026-09-09')
                self.assertEqual(float(result['rows'][0]['changeBP']), 10)

    def test_analysis_projection_captures_provider_once_during_concurrent_switch(self):
        from backend import akshare_provider as ak, providers

        day = dict(source='akshare', snapshot=None, evaluationDate=TARGET,
                   latestAttempt=None, dataState='pending')
        ak_data = dict(bonds=[], counts={'bonds': 1}, cells=[{'yieldPct': '2.5'}],
                       rulesVersion=ak.RULES['version'], mappingVersion=ak.RULES['mappingVersion'],
                       yieldDefinition=ak.YIELD_DEFINITION, provenance={'runId': 'ak-sample'})
        wind_data = dict(bonds=[{'code': '809336.IB'}], cells=[{'yieldPct': '1.7691'}],
                         rulesVersion=RULES['version'], mappingVersion=RULES['mappingVersion'],
                         yieldDefinition=RULES['yieldDefinition'], provenance={'runId': 'wind-sample'})
        # The global preference may change after request entry, but its reads
        # must retain the originally chosen source and its financial definition.
        with patch('backend.providers.active_provider', side_effect=['akshare', 'wind']) as choose, \
             patch('backend.akshare_provider.read_day', return_value=day), \
             patch('backend.akshare_read_model.summary', return_value=ak_data), \
             patch('backend.available_data.read_available', return_value=wind_data) as wind_read:
            projection = providers.analysis_day(TARGET)
        self.assertEqual(projection['snapshot']['yieldDefinition'], ak.YIELD_DEFINITION)
        self.assertEqual(projection['snapshot']['publishedRunId'], 'ak-sample')
        self.assertEqual(projection['snapshot']['cells'], ak_data['cells'])
        self.assertEqual(projection['snapshot']['source'], 'akshare')
        choose.assert_called_once()
        wind_read.assert_not_called()

    def test_explicit_job_provider_is_bound_without_switching_selected_provider(self):
        with patch('backend.api.spawn_worker'):
            response = self.client.post('/api/runs', json={'targetDate': TARGET, 'provider': 'wind'})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['source'], 'wind')
        self.assertEqual(self.client.get('/api/bootstrap').json()['mode'], 'akshare')

    def test_active_job_for_other_provider_conflicts_instead_of_being_reused(self):
        original = store.enqueue(TARGET, provider='wind')
        with patch('backend.api.spawn_worker') as start:
            conflict = self.client.post('/api/runs', json={'targetDate': TARGET})
        self.assertEqual(conflict.status_code, 409, conflict.text)
        start.assert_not_called()
        self.assertEqual(store.get_run(original['runId']), original)
        self.assertEqual(len(store.runs()), 1)
        self.select('wind')
        with patch('backend.api.spawn_worker'):
            same = self.client.post('/api/runs', json={'targetDate': TARGET})
        self.assertEqual(same.status_code, 202, same.text)
        self.assertEqual(same.json()['runId'], original['runId'])

    def test_invalid_selection_and_job_provider_leave_saved_state_unchanged(self):
        for body in ({'provider': 'unknown'}, {'provider': ''}):
            with self.subTest(body=body):
                response = self.client.post('/api/source/provider', json=body)
                self.assertIn(response.status_code, (400, 422), response.text)
        with patch('backend.api.spawn_worker') as start:
            response = self.client.post('/api/runs', json={'targetDate': TARGET, 'provider': 'unknown'})
        self.assertIn(response.status_code, (400, 422), response.text)
        start.assert_not_called()
        self.assertEqual(self.client.get('/api/bootstrap').json()['mode'], 'akshare')
        self.assertEqual(store.runs(), [])

    def test_worker_executes_queued_akshare_job_after_selection_changes_to_wind(self):
        from backend.worker import drain

        queued = store.enqueue(TARGET, provider='akshare')
        self.select('wind')

        def collect(run):
            self.assertEqual(run['runId'], queued['runId'])
            self.assertEqual(run['source'], 'akshare')
            return {'counts': {'bonds': 0}, 'message': '离线 AKShare 采集完成'}

        with patch('backend.akshare_provider.collect', side_effect=collect) as acquire, \
             patch('backend.wind_pipeline.Pipeline.run', side_effect=AssertionError('Unexpected Wind pipeline')), \
             patch('backend.worker.time.sleep'):
            drain()
        acquire.assert_called_once()
        completed = store.get_run(queued['runId'])
        self.assertEqual(completed['source'], 'akshare')
        self.assertEqual(completed['status'], 'succeeded')
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM valuation_dates').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
