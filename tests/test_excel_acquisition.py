import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import storage as store
from backend.available_data import read_available
from backend.excel_acquisition import Acquisition, FIELDS, TOOLS, question, populated
from backend.lineage import Recorder
from backend.wind_mcp import WindError


TARGET = '2026-09-11'
CODE = '809336.IB'


def table(codes, group=None, overrides=None):
    values = {'Wind代码': None, '主证券代码': None, '证券简称': '26河北23',
              '债务主体名称': '河北省人民政府', '所属概念板块': '地方政府一般债',
              '发行起始日期': '2026-04-20', '到期日期': '2036-04-21',
              '发行总额': 88.2, '票面利率': 1.89, '交易币种': 'CNY',
              '收盘价收益率': 1.7691, '实际剩余期限': 9.6082,
              '基于净价的收盘价修正久期': 8.7035, '债券余额': 88.2, '收盘价净价': 101.0634}
    market = {'Wind代码', '收盘价收益率', '实际剩余期限', '基于净价的收盘价修正久期', '债券余额', '收盘价净价'}
    if group:
        values = {key: value for key, value in values.items()
                  if key in market if group == 'market'} if group == 'market' else {
                      key: value for key, value in values.items() if key not in market or key == 'Wind代码'}
    values.update(overrides or {})
    rows = [[code if key in ('Wind代码', '主证券代码') and value is None else value
             for key, value in values.items()] for code in codes]
    return {'content': [{'type': 'text', 'text': json.dumps({'data': {'data': [{
        'columns': [{'name': key} for key in values], 'rows': rows}]}}, ensure_ascii=False)}]}


def record(recorder, tool, text, result, error=None, status=200):
    rid = recorder.begin('tools/call', {'params': {'name': tool, 'arguments': {'question': text}}})
    recorder.finish(rid, {'result': result}, error=error, http_status=status)
    return rid


class FakeFactory:
    def __init__(self, reply=None):
        self.reply = reply
        self.calls = []
        self.connections = 0

    def __call__(self, key, recorder):
        factory = self

        class Client:
            async def __aenter__(self):
                factory.connections += 1
                return self

            async def __aexit__(self, *args):
                return None

            async def list_tools(self):
                return [{'name': value} for value in TOOLS.values()]

            async def call(self, tool, arguments):
                text = arguments['question']
                codes = re.findall(r'\d{6,9}\.(?:IB|SH|SZ|BC)', text)
                group = next(group for group, name in TOOLS.items() if name == tool)
                factory.calls.append((group, codes, text))
                response = factory.reply(group, codes, len(factory.calls)) if factory.reply else table(codes, group)
                if isinstance(response, Exception):
                    record(recorder, tool, text, {}, error='transport uncertain', status=None)
                    raise response
                record(recorder, tool, text, response)
                return response

        return Client()


class ExcelAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_patch = patch.object(store, 'DB', self.root/'data.sqlite3')
        self.db_patch.start()
        store.initialize(seed=False)
        self.path = self.root/'universe.json'

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def universe(self, codes):
        self.path.write_text(json.dumps({'codes': codes, 'sourceHash': 'excel-hash', 'sheet': '原数据-WIND',
                                        'sourcePath': 'example.xlsx'}), encoding='utf-8')

    def acquisition(self, codes=None, budget=20, batch=100, resume=None, target=TARGET, retry_rejected=False):
        if codes is not None:
            self.universe(codes)
        return Acquisition(self.path, target, batch, budget, resume=resume, emit=lambda *a, **k: None,
                           retry_rejected=retry_rejected)

    def execute(self, acquisition, factory=None):
        factory = factory or FakeFactory()
        return asyncio.run(acquisition.run(factory, key='not-a-real-key')), factory

    def test_complete_requested_universe_is_visible_without_national_publication(self):
        acquisition = self.acquisition([CODE, '809337.IB', CODE])
        state, factory = self.execute(acquisition)
        self.assertEqual(state['status'], 'finished')
        self.assertEqual(state['dataCalls'], 2)
        self.assertEqual(state['requestedCodes'], 2)
        self.assertEqual(state['coverage']['fullyPopulatedCodes'], 2)
        self.assertFalse(state['coverage']['nationalMarketComplete'])
        self.assertEqual(len(read_available(TARGET)['bonds']), 2)
        self.assertTrue(all(TARGET in call[2] for call in factory.calls))
        self.assertFalse(any('赎回' in call[2] or '提前偿还' in call[2] for call in factory.calls))
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_runs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 0)

    def test_reuse_exact_date_and_cross_market_cached_values_including_zero(self):
        rec = Recorder('cache', TARGET)
        record(rec, TOOLS['basic'], 'cache', table([CODE], overrides={
            '跨市场代码': '236633.SH', '票面利率': 0, '发行总额': 0, '基于净价的收盘价修正久期': 0}))
        rec_old = Recorder('old', '2026-09-10')
        record(rec_old, TOOLS['basic'], 'old date', table(['809337.IB']))
        acquisition = self.acquisition(['236633.SH', '809337.IB'])
        state, factory = self.execute(acquisition)
        self.assertEqual(len(factory.calls), 2)
        self.assertTrue(all(codes == ['809337.IB'] for _, codes, _ in factory.calls))
        self.assertTrue(all(group['status'] == 'cached' for group in state['manifest']['236633.SH'].values()))

    def test_missing_optional_market_field_only_requests_market_and_null_is_terminal(self):
        rec = Recorder('cache', TARGET)
        record(rec, TOOLS['basic'], 'cache', table([CODE], 'basic'))
        acquisition = self.acquisition([CODE])
        factory = FakeFactory(lambda group, codes, n: table(codes, group, {'基于净价的收盘价修正久期': None}))
        state, factory = self.execute(acquisition, factory)
        self.assertEqual([group for group, _, _ in factory.calls], ['market'])
        self.assertEqual(state['manifest'][CODE]['market']['status'], 'partial')
        self.assertIn('duration', state['manifest'][CODE]['market']['missingFields'])
        resume = self.acquisition(resume=acquisition.path)
        _, resumed = self.execute(resume)
        self.assertEqual(resumed.connections, 0)

    def test_cached_explicit_null_group_is_reused_without_paid_repeat(self):
        rec = Recorder('pilot returns null', TARGET)
        record(rec, TOOLS['basic'], 'pilot', table([CODE], overrides={
            '基于净价的收盘价修正久期': None, '收盘价收益率': None}))
        acquisition = self.acquisition([CODE])
        state, factory = self.execute(acquisition)
        self.assertEqual(factory.connections, 0)
        self.assertEqual(state['manifest'][CODE]['market']['status'], 'partial')
        self.assertEqual(state['progress']['fullyPopulated'], 0)
        self.assertEqual(state['progress']['attempted'], 1)

    def test_budget_pause_resume_counts_cumulative_and_does_not_repeat(self):
        acquisition = self.acquisition([CODE, '809337.IB'], budget=1)
        state, factory = self.execute(acquisition)
        self.assertEqual((state['status'], state['dataCalls']), ('budget_paused', 1))
        resume = self.acquisition(budget=2, resume=acquisition.path)
        state, resumed = self.execute(resume)
        self.assertEqual((state['status'], state['dataCalls']), ('finished', 2))
        self.assertEqual([call[0] for call in factory.calls+resumed.calls], ['basic', 'market'])

    def test_replays_committed_raw_after_checkpoint_lag_without_duplicate_paid_call(self):
        acquisition = self.acquisition([CODE])
        rec = Recorder('interrupted Excel', TARGET)
        acquisition.state['sessionId'] = rec.id
        task = acquisition.next_task()
        acquisition.save()
        rid = record(rec, TOOLS['basic'], task['question'], table([CODE], 'basic'))
        resume = self.acquisition(resume=acquisition.path)
        state, factory = self.execute(resume)
        self.assertEqual(state['dataCalls'], 2)
        self.assertEqual([group for group, _, _ in factory.calls], ['market'])
        self.assertIn(rid, state['manifest'][CODE]['basic']['requestIds'])

    def test_transport_or_quota_failure_stops_and_resume_cannot_repeat_uncertain(self):
        for quota in (False, True):
            with self.subTest(quota=quota):
                codes = [CODE] if not quota else ['809337.IB']
                if self.path.exists():
                    self.path = self.root/'quota-universe.json'
                acquisition = self.acquisition(codes)
                response = {'isError': True, 'content': [{'type': 'text', 'text': '余额不足，请先充值'}]} if quota else WindError('timeout')
                state, factory = self.execute(acquisition, FakeFactory(lambda *args: response))
                self.assertEqual((state['status'], state['dataCalls']), ('stopped', 1))
                self.assertEqual(state['manifest'][codes[0]]['basic']['status'], 'uncertain')
                resume = self.acquisition(resume=acquisition.path)
                resumed_state, resumed = self.execute(resume)
                self.assertEqual(resumed_state['status'], 'stopped')
                self.assertEqual(resumed.connections, 0)

    def test_missing_rows_split_and_single_fallback_is_bounded(self):
        codes = [CODE, '809337.IB']
        acquisition = self.acquisition(codes, budget=20)
        factory = FakeFactory(lambda group, asked, n: table([CODE] if CODE in asked else [], group))
        state, factory = self.execute(acquisition, factory)
        self.assertEqual(state['status'], 'finished')
        self.assertEqual(len(factory.calls), 4)  # Two group batches + one missing singleton per group.
        self.assertTrue(all(entry['status'] == 'unavailable' for entry in state['manifest']['809337.IB'].values()))
        self.assertEqual(state['coverage']['presentCodes'], 1)

    def test_returned_outside_universe_rows_are_preserved_but_not_counted_as_coverage(self):
        acquisition = self.acquisition([CODE])
        factory = FakeFactory(lambda group, codes, n: table([*codes, '809399.IB'], group))
        state, _ = self.execute(acquisition, factory)
        self.assertIn('809399.IB', state['unexpectedCodes'])
        self.assertEqual(state['coverage']['presentCodes'], 1)
        self.assertEqual(len(read_available(TARGET)['bonds']), 2)

    def test_resume_scope_and_date_validation_and_finite_numeric_values(self):
        acquisition = self.acquisition([CODE], budget=0)
        self.execute(acquisition)
        with self.assertRaises(ValueError):
            self.acquisition(target='2026-09-10', resume=acquisition.path)
        self.universe(['809399.IB'])
        with self.assertRaises(ValueError):
            self.acquisition(resume=acquisition.path)
        for value in (None, '', '  ', 'NaN', 'Infinity', float('nan'), True):
            self.assertFalse(populated(value, 'duration'))
        for value in (0, '0', '1.5E-4'):
            self.assertTrue(populated(value, 'duration'))

    def test_explicit_quota_retry_preserves_raw_count_and_skips_successful_codes(self):
        existing = Recorder('old complete row', TARGET)
        record(existing, TOOLS['basic'], 'already acquired', table([CODE]))
        acquisition = self.acquisition([CODE, '809337.IB'])
        quota = {'isError': True, 'content': [{'type': 'text', 'text': '余额不足，请先充值'}]}
        factory = FakeFactory(lambda group, codes, n: quota if n == 2 else table(codes, group))
        first, _ = self.execute(acquisition, factory)
        self.assertEqual((first['status'], first['dataCalls']), ('stopped', 2))
        rejected = first['manifest']['809337.IB']['market']['requestIds'][-1]
        with store.connection() as db:
            before = dict(db.execute('SELECT * FROM source_requests WHERE id=?', (rejected,)).fetchone())
        # Ordinary --resume cannot reopen the refused paid question.
        blocked, no_calls = self.execute(self.acquisition(resume=acquisition.path))
        self.assertEqual((blocked['status'], no_calls.connections), ('stopped', 0))
        resumed = self.acquisition(resume=acquisition.path, retry_rejected=True)
        state, retry_factory = self.execute(resumed)
        self.assertEqual((state['status'], state['dataCalls']), ('finished', 3))
        self.assertEqual([(group, codes) for group, codes, _ in retry_factory.calls], [('market', ['809337.IB'])])
        self.assertEqual(state['rejectedRetries'][0]['requestId'], rejected)
        self.assertIn(rejected, state['manifest']['809337.IB']['market']['requestIds'])
        with store.connection() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM source_requests WHERE id=?', (rejected,)).fetchone()), before)

    def test_explicit_retry_flag_never_reopens_transport_uncertainty_or_response_with_rows(self):
        for variant in ('transport', 'quota_with_rows'):
            with self.subTest(variant=variant):
                if self.path.exists():
                    self.path = self.root/(variant+'-universe.json')
                acquisition = self.acquisition([CODE])
                if variant == 'transport':
                    response = WindError('request timed out')
                else:
                    response = table([CODE], 'basic')
                    response.update(isError=True)
                    response['content'].append({'type': 'text', 'text': '余额不足，请先充值'})
                self.execute(acquisition, FakeFactory(lambda *args: response))
                resumed = self.acquisition(resume=acquisition.path, retry_rejected=True)
                client = FakeFactory()
                with self.assertRaises(ValueError):
                    self.execute(resumed, client)
                self.assertEqual(client.connections, 0)

    def test_explicit_rejection_retry_still_obeys_cumulative_budget(self):
        acquisition = self.acquisition([CODE], budget=1)
        quota = {'isError': True, 'content': [{'type': 'text', 'text': '余额不足，请先充值'}]}
        self.execute(acquisition, FakeFactory(lambda *args: quota))
        resumed = self.acquisition(budget=1, resume=acquisition.path, retry_rejected=True)
        state, factory = self.execute(resumed)
        self.assertEqual((state['status'], state['dataCalls'], factory.connections), ('budget_paused', 1, 0))
        # The explicit authorization is durable; enlarging the budget later
        # does not need to erase or modify the original refusal evidence.
        state, factory = self.execute(self.acquisition(budget=3, resume=acquisition.path))
        self.assertEqual((state['status'], state['dataCalls']), ('finished', 3))
        self.assertEqual(len(factory.calls), 2)


if __name__ == '__main__':
    unittest.main()
