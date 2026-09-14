import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import storage as store
from backend.available_data import read_available
from backend.lineage import Recorder, dump
from backend.wind_mcp import WindError
from scripts.supplement_excel_fields import Supplement, question, TOOL
from tests.test_excel_acquisition import CODE, TARGET, FakeFactory, record, table


def indicator(codes, field, value):
    name = '收盘价到期收益率' if field == 'yieldPct' else '收盘价修正久期'
    return {'content': [{'type': 'text', 'text': json.dumps({'data': {'data': [{
        'columns': [{'name': 'Wind代码'}, {'name': name}], 'rows': [[code, value] for code in codes]}]}})}]}


class SupplementExcelFieldsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_patch = patch.object(store, 'DB', self.root/'data.sqlite3')
        self.db_patch.start()
        store.initialize(seed=False)
        self.main = self.root/'main.json'

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def main_state(self, codes, status='finished', market_status=None):
        state = dict(scope='excel_universe', targetDate=TARGET, codes=codes,
            sourceHash='excel-hash', universeHash=hashlib.sha256(dump(codes).encode('utf-8')).hexdigest(),
            status=status, manifest={code: {'market': {'status': (market_status or {}).get(code, 'partial')}} for code in codes})
        self.main.write_text(json.dumps(state), encoding='utf-8')

    def cache(self, codes, overrides=None):
        rec = Recorder('main group response', TARGET)
        record(rec, TOOL, 'original batch', table(codes, overrides={
            '收盘价收益率': None, '基于净价的收盘价修正久期': None, **(overrides or {})}))

    def supplement(self, budget=20, fields=None, resume=None, sleeper=asyncio.sleep):
        return Supplement(self.main, TARGET, fields or ['yieldPct', 'duration'], 100, budget,
                          resume=resume, sleeper=sleeper, emit=lambda *a, **k: None)

    def factory(self, value=0):
        factory = FakeFactory()
        factory.reply = lambda group, codes, n: indicator(codes,
            'yieldPct' if '到期收益率' in factory.calls[-1][2] else 'duration', value)
        return factory

    def execute(self, supplement, factory=None):
        factory = factory or self.factory()
        return asyncio.run(supplement.run(factory, key='test-key')), factory

    def test_two_single_indicators_once_per_code_and_zero_is_returned_value(self):
        self.main_state([CODE, '809337.IB'])
        self.cache([CODE, '809337.IB'])
        supplement = self.supplement()
        state, factory = self.execute(supplement)
        self.assertEqual((state['status'], state['dataCalls']), ('finished', 2))
        self.assertEqual(state['progress']['receivedValues'], 4)
        self.assertEqual(len(factory.calls), 2)
        self.assertIn('收盘价到期收益率（%）', factory.calls[0][2])
        self.assertIn('基于净价的收盘价修正久期（年）', factory.calls[1][2])
        self.assertTrue(all(TARGET in call[2] for call in factory.calls))
        self.assertTrue(all(bond['yieldPct'] == '0' and bond['duration'] == '0' for bond in read_available(TARGET)['bonds']))
        self.assertTrue(Path(state['summaryPlanPath']).exists())
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_runs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 0)

    def test_only_main_terminal_missing_nonmatured_codes_are_queried(self):
        codes = [CODE, '809337.IB', '809338.IB', '809339.IB']
        self.main_state(codes, market_status={'809337.IB': 'pending'})
        self.cache([CODE, '809337.IB'])
        self.cache(['809338.IB'], {'到期日期': TARGET})
        self.cache(['809339.IB'], {'收盘价收益率': 1.23, '基于净价的收盘价修正久期': 8.2})
        state, factory = self.execute(self.supplement())
        self.assertTrue(all(call[1] == [CODE] for call in factory.calls))
        self.assertEqual(state['skippedMaturedCodes'], ['809338.IB'])
        self.assertNotIn('809337.IB', state['manifest'])

    def test_waits_for_full_batch_then_drains_small_tail_after_main_stops(self):
        self.main_state([CODE], status='running')
        self.cache([CODE])
        waits = []

        async def sleeper(seconds):
            waits.append(seconds)
            self.main_state([CODE], status='finished')

        state, factory = self.execute(self.supplement(sleeper=sleeper))
        self.assertEqual(waits, [10])
        self.assertEqual((state['status'], len(factory.calls)), ('finished', 2))

    def test_completed_paid_call_survives_resume_and_budget_is_cumulative(self):
        self.main_state([CODE])
        self.cache([CODE])
        supplement = self.supplement(budget=1)
        first, first_factory = self.execute(supplement)
        sid = first['sessionId']
        self.assertEqual((first['status'], first['dataCalls']), ('budget_paused', 1))
        resumed = self.supplement(budget=3, resume=supplement.path)
        self.assertEqual(resumed.state['dataCalls'], 1)
        self.assertEqual(resumed.state['sessionId'], sid)
        state, second_factory = self.execute(resumed)
        self.assertEqual((state['status'], state['dataCalls'], state['sessionId']), ('finished', 2, sid))
        self.assertEqual(len(first_factory.calls), 1)
        self.assertEqual(len(second_factory.calls), 1)
        self.assertIn('修正久期', second_factory.calls[0][2])

    def test_committed_reply_checkpoint_lag_replays_without_paid_repeat(self):
        self.main_state([CODE])
        self.cache([CODE])
        supplement = self.supplement()
        rec = Recorder('interrupted supplemental', TARGET)
        supplement.state['sessionId'] = rec.id
        supplement.reserve('yieldPct', [CODE])
        rid = record(rec, TOOL, question('yieldPct', [CODE], TARGET), indicator([CODE], 'yieldPct', 1.25))
        state, factory = self.execute(self.supplement(resume=supplement.path))
        self.assertEqual(state['dataCalls'], 2)
        self.assertEqual(len(factory.calls), 1)
        self.assertIn('修正久期', factory.calls[0][2])
        self.assertEqual(state['manifest'][CODE]['yieldPct']['requestId'], rid)

    def test_null_and_absent_rows_are_terminal_across_restart(self):
        self.main_state([CODE, '809337.IB'])
        self.cache([CODE, '809337.IB'])
        supplement = self.supplement(fields=['yieldPct'])
        factory = FakeFactory(lambda group, codes, n: indicator([CODE], 'yieldPct', None))
        first, _ = self.execute(supplement, factory)
        self.assertEqual(first['manifest'][CODE]['yieldPct']['status'], 'null')
        self.assertEqual(first['manifest']['809337.IB']['yieldPct']['status'], 'unavailable')
        state, resumed = self.execute(self.supplement(fields=['yieldPct'], resume=supplement.path))
        self.assertEqual(state['dataCalls'], 1)
        self.assertEqual(resumed.calls, [])

    def test_uncertain_request_is_not_reissued_on_resume(self):
        self.main_state([CODE])
        self.cache([CODE])
        supplement = self.supplement()
        first, factory = self.execute(supplement, FakeFactory(lambda *args: WindError('timeout')))
        self.assertEqual((first['status'], first['dataCalls']), ('stopped', 1))
        state, resumed = self.execute(self.supplement(resume=supplement.path))
        self.assertEqual((state['status'], state['dataCalls']), ('stopped', 1))
        self.assertEqual(resumed.connections, 0)


if __name__ == '__main__':
    unittest.main()
