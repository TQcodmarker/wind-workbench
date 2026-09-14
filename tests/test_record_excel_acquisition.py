import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import storage as store
from backend.domain import RULES
from backend.excel_acquisition import FIELDS
from backend.lineage import Recorder, dump
from scripts.record_excel_acquisition import record as record_summary
from tests.test_excel_acquisition import CODE, TARGET, record, table


class RecordExcelAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_patch = patch.object(store, 'DB', self.root/'data.sqlite3')
        self.db_patch.start()
        store.initialize(seed=False)
        self.checkpoint = self.root/'checkpoint.json'
        self.outputs = self.root/'outputs'

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def session(self, calls=1, values=None, target=TARGET):
        rec = Recorder('test source', target)
        for index in range(calls):
            result = values if index == 0 and values else table([])
            record(rec, 'get_bond_market_data', 'query '+str(index), result)
        return rec

    def pilot(self, name, rec, calls=999, target=TARGET):
        path = self.root/(name+'-plan.json')
        path.write_text(json.dumps({'targetDate': target}), encoding='utf-8')
        path.with_name(path.stem+'-result.json').write_text(json.dumps(dict(
            targetDate=target, status='finished', sessionId=rec.id, dataCalls=calls,
            receivedCodes=['misleading-outside-code'])), encoding='utf-8')
        return path

    def save_checkpoint(self, rec, codes, status='finished', overrides=None):
        state = dict(scope='excel_universe', targetDate=TARGET, status=status, codes=codes,
            manifest={code: {group: dict(status='partial', requestIds=[]) for group in FIELDS} for code in codes},
            universeHash=hashlib.sha256(dump(codes).encode('utf-8')).hexdigest(),
            sourceHash='workbook-hash', sourcePath='example.xlsx', sheet='原数据-WIND',
            sessionId=rec.id, dataCalls=9999, createdAt=store.now(), startedAt=store.now(), finishedAt=store.now())
        state.update(overrides or {})
        self.checkpoint.write_text(json.dumps(state), encoding='utf-8')

    def run_summary(self, pilots=()):
        with patch('backend.wind_mcp.WindMCP.__init__', side_effect=AssertionError('No Wind query')):
            return record_summary(self.checkpoint, pilots, self.outputs)

    def test_finished_partial_values_are_honest_and_calls_exclude_old_round(self):
        self.session(22, table([CODE], overrides={'跨市场代码': '236633.SH'}))
        pilot50 = self.session(2, table(['809337.IB'], overrides={'收盘价收益率': None, '基于净价的收盘价修正久期': None}))
        pilot100 = self.session(3)
        runner = self.session(4)
        self.save_checkpoint(runner, ['236633.SH', '809337.IB', '809338.IB'])
        paths = [self.pilot('pilot50', pilot50), self.pilot('pilot100', pilot100)]
        report = self.run_summary(paths)
        self.assertEqual((report['dataCalls'], report['pilotDataCalls'], report['runnerDataCalls']), (9, 5, 4))
        self.assertEqual(report['run']['status'], 'succeeded')
        self.assertEqual(report['run']['outcome'], 'excel_universe')
        self.assertFalse(report['publishedSnapshot'])
        coverage = report['coverage']
        self.assertEqual((coverage['requestedCodes'], coverage['presentCodes'], coverage['fullyPopulatedCodes']), (3, 2, 1))
        self.assertEqual(coverage['processedCodes'], 3)
        self.assertFalse(coverage['allFieldsComplete'])
        self.assertEqual(report['missingFieldCounts']['yieldPct'], 2)
        self.assertEqual(report['bonds'][0]['matchedCode'], CODE)
        csv_path = Path(report['outputPaths']['missingCsv'])
        self.assertEqual(csv_path.read_bytes()[:3], b'\xef\xbb\xbf')
        with csv_path.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row['Excel代码'] for row in rows], ['809337.IB', '809338.IB'])
        self.assertTrue(Path(report['outputPaths']['json']).name.startswith('20260911-Excel全部债券'))
        data_csv = Path(report['outputPaths']['dataCsv'])
        self.assertEqual(data_csv.read_bytes()[:3], b'\xef\xbb\xbf')
        with data_csv.open(encoding='utf-8-sig', newline='') as stream:
            all_rows = list(csv.DictReader(stream))
        self.assertEqual([row['Excel代码'] for row in all_rows], ['236633.SH', '809337.IB', '809338.IB'])
        self.assertEqual(all_rows[0]['收盘价到期收益率（%）'], '1.7691')
        self.assertEqual(all_rows[2]['收盘价到期收益率（%）'], '')
        self.assertEqual(all_rows[2]['记录覆盖'], '未取得')

    def test_repeat_is_idempotent_and_preserves_existing_snapshot(self):
        old = store.enqueue(TARGET)
        store.update_run(old['runId'], status='succeeded', outcome='data')
        with store.connection() as db:
            db.execute('INSERT INTO snapshots VALUES (?,?,?,?,?)', (old['runId'], store.now(), RULES['version'], 'old', '[]'))
            db.execute("UPDATE valuation_dates SET state='ready',published_id=? WHERE date=?", (old['runId'], TARGET))
        runner = self.session(1, table([CODE], overrides={'票面利率': 0, '发行总额': 0, '基于净价的收盘价修正久期': 0}))
        self.save_checkpoint(runner, [CODE])
        first = self.run_summary()
        second = self.run_summary()
        self.assertEqual(first['runId'], second['runId'])
        self.assertEqual(second['coverage']['fullyPopulatedCodes'], 1)
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_runs').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM job_runs WHERE status IN ('queued','running')").fetchone()[0], 0)
            day = db.execute('SELECT * FROM valuation_dates WHERE date=?', (TARGET,)).fetchone()
            self.assertEqual((day['state'], day['published_id'], day['latest_id']), ('ready', old['runId'], first['runId']))

    def test_stopped_is_failed_and_resumed_terminal_updates_same_logical_run(self):
        runner = self.session(1, table([CODE]))
        self.save_checkpoint(runner, [CODE], status='budget_paused')
        first = self.run_summary()
        self.assertEqual(first['run']['status'], 'failed')
        self.assertEqual(first['run']['acquisitionStatus'], 'budget_paused')
        self.save_checkpoint(runner, [CODE], status='finished')
        second = self.run_summary()
        self.assertEqual(first['runId'], second['runId'])
        self.assertEqual(second['run']['status'], 'succeeded')
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_runs').fetchone()[0], 1)

    def test_running_or_falsely_finished_manifest_cannot_create_terminal_job(self):
        runner = self.session()
        self.save_checkpoint(runner, [CODE], status='running')
        with self.assertRaises(ValueError):
            self.run_summary()
        self.save_checkpoint(runner, [CODE], overrides={'manifest': {CODE: {
            'basic': {'status': 'complete'}, 'market': {'status': 'pending'}}}})
        with self.assertRaises(ValueError):
            self.run_summary()
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_runs').fetchone()[0], 0)
        self.assertFalse(self.outputs.exists())

    def test_duplicate_pilot_sessions_are_counted_once_and_cross_date_rejected(self):
        runner = self.session(2)
        pilot = self.session(3)
        plan = self.pilot('pilot', pilot)
        self.save_checkpoint(runner, [CODE])
        report = self.run_summary([plan, plan])
        self.assertEqual(report['dataCalls'], 5)
        wrong = self.session(1, target='2026-09-10')
        wrong_plan = self.pilot('wrong', wrong, target='2026-09-10')
        with self.assertRaises(ValueError):
            self.run_summary([wrong_plan])

    def test_previously_associated_old_session_cannot_be_rebilled_as_new_pilot(self):
        old = store.enqueue(TARGET)
        store.update_run(old['runId'], status='succeeded')
        old_session = self.session(22)
        with store.connection() as db:
            db.execute('UPDATE source_sessions SET run_id=? WHERE id=?', (old['runId'], old_session.id))
        runner = self.session(1)
        self.save_checkpoint(runner, [CODE])
        with self.assertRaises(ValueError):
            self.run_summary([self.pilot('old', old_session)])
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_runs').fetchone()[0], 1)
            self.assertIsNone(db.execute('SELECT run_id FROM source_sessions WHERE id=?', (runner.id,)).fetchone()[0])

    def test_supplement_checkpoint_counts_actual_calls_once_and_updates_completeness(self):
        runner = self.session(2, table([CODE], overrides={'收盘价收益率': None, '基于净价的收盘价修正久期': None}))
        supplement = self.session(3, table([CODE]))
        self.save_checkpoint(runner, [CODE])
        universe_hash = json.loads(self.checkpoint.read_text())['universeHash']
        supplement_path = self.root/'supplement.json'
        supplement_path.write_text(json.dumps(dict(scope='excel_field_supplement', targetDate=TARGET,
            universeHash=universe_hash, sessionId=supplement.id, status='finished', dataCalls=999)), encoding='utf-8')
        report = record_summary(self.checkpoint, [], self.outputs,
                                supplement_checkpoints=[supplement_path, supplement_path])
        self.assertEqual((report['dataCalls'], report['runnerDataCalls'], report['supplementDataCalls']), (5, 2, 3))
        self.assertEqual(report['coverage']['fullyPopulatedCodes'], 1)
        self.assertEqual(report['run']['supplementDataCalls'], 3)
        supplement_path.write_text(json.dumps(dict(scope='excel_field_supplement', targetDate=TARGET,
            universeHash=universe_hash, sessionId=supplement.id, status='running')), encoding='utf-8')
        with self.assertRaises(ValueError):
            record_summary(self.checkpoint, [], self.outputs, supplement_checkpoints=[supplement_path])


if __name__ == '__main__':
    unittest.main()
