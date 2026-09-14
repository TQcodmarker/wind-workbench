"""Indexed pages preserve complete saved-data semantics without parsing a day."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend import akshare_provider as provider, providers, storage as store
from backend.api import app
from backend.lineage import Recorder


TARGET = '2026-09-11'


def sample_bonds():
    rows = [
        ('809108', '河北省', 'general', '2026-04-20', 'eligible', '0'),
        ('809102', '上海市', 'special', '2025-08-07', 'eligible', '1.7'),
        ('809104', '河北省', 'general', '2025-08-07', 'incomplete', None),
        ('809103', '广西壮族自治区', 'special', '2026-04-20', 'excluded', None),
        ('809107', '未知发行人', None, '2026-04-20', 'incomplete', None),
        ('809101', '河北省', 'general', '2026-04-20', 'conflicted', None),
        ('809106', '广西壮族自治区', 'general', '2026-04-20', 'eligible', '1.9'),
        ('809105', '上海市', 'special', '2025-08-07', 'incomplete', None),
    ]
    bonds = []
    for code, issuer, kind, issued, disposition, value in rows:
        detail = dict(bondCode=code, bondType='地方政府债', bondName='测试券' + code,
                      entyFullName=issuer, issueDate=issued, mrtyDate='2036-09-11',
                      bondFullName=issuer + {'general': '一般债券', 'special': '专项债券', None: '债券'}[kind],
                      issueAmnt='10', parCouponRate='1.89', couponType='附息式固定利率', couponFrqncy='年',
                      frstValueDate=issued[:4]+'-09-11',
                      frstCpnDt=str(int(issued[:4])+1)+'-09-11')
        evidence = dict(requestId='detail-' + code, function='bond_info_detail_cm',
                        runId='fixture-details', retrievedAt='2026-09-12T09:00:00+08:00')
        raw = None if value is None else dict(bondcode=code, showDate=TARGET + ' 16:00:00',
                                             dmiLatestContraRate=value, dmiLatestRate='101')
        bond = provider.normalize_bond(detail, TARGET, raw, detail_evidence=evidence,
                 trade_evidence=dict(requestId='trade-' + code, runId='fixture-trades',
                                     function='bond_spot_deal', retrievedAt='2026-09-12T09:00:00+08:00'))
        bond['disposition'] = disposition
        bond['staticSyncStatus'] = 'pending' if code in ('809104', '809107') else 'completed'
        bonds.append(bond)
    bonds[1]['name'] = '测试沪债%专项'
    bonds[1]['codes'].append('ALIAS-809102')
    return bonds


class AvailablePaginationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(store, 'DB', Path(directory) / 'pages.sqlite3'))
        self.stack.enter_context(patch.object(store, 'MODE', 'wind'))
        self.stack.enter_context(patch.dict('os.environ', {'BOND_DATA_SOURCE': 'akshare'}))
        self.stack.enter_context(patch('requests.Session.send', side_effect=AssertionError('No network in saved reads')))
        self.stack.enter_context(patch.object(provider.QueryRecorder, 'query', side_effect=AssertionError('No collection')))
        self.stack.enter_context(patch('backend.api.spawn_worker', side_effect=AssertionError('No worker from saved read')))
        self.stack.enter_context(patch('backend.api.spawn_sync_worker', side_effect=AssertionError('No sync from saved read')))
        store.initialize(seed=False)
        provider.initialize(seed=False)
        providers.select_provider('akshare')
        self.bonds = sample_bonds()
        self.dataset = provider._dataset(TARGET, self.bonds)
        provider._save_dataset(self.dataset)
        self.client = TestClient(app)

    def get(self, suffix, **params):
        response = self.client.get(f'/api/datasets/{TARGET}/available' + suffix, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def codes(self, **params):
        data = self.get('/page', **params)
        return {bond['code'] for bond in data['bonds']}

    def test_summary_is_global_lightweight_and_does_not_confuse_filtered_total(self):
        summary = self.get('/summary')
        self.assertEqual(summary['bonds'], [])
        self.assertEqual(summary['source'], 'akshare')
        self.assertEqual(summary['counts'], self.dataset['counts'])
        self.assertEqual(summary['cells'], self.dataset['cells'])
        self.assertEqual(summary['coverage']['staticCompleted'], 6)
        self.assertEqual(summary['coverage']['observed'], 3)
        filtered = self.get('/page', region='河北', status='eligible')
        self.assertEqual(filtered['total'], 1)
        self.assertEqual(filtered['bonds'][0]['yieldPct'], '0')
        self.assertEqual(self.get('/summary')['counts']['bonds'], 8)

    def test_filters_apply_to_entire_dataset_and_combine(self):
        examples = [
            ({'tier': '1'}, {'809102.IB', '809105.IB'}),
            ({'tier': '3'}, {'809103.IB', '809106.IB'}),
            ({'region': '河北'}, {'809101.IB', '809104.IB', '809108.IB'}),
            ({'cohort': 'before_20250808'}, {'809102.IB', '809104.IB', '809105.IB'}),
            ({'scope': 'special'}, {'809102.IB', '809103.IB', '809105.IB'}),
            ({'status': 'conflicted'}, {'809101.IB'}),
            ({'status': 'eligible', 'cohort': 'on_or_after_20250808', 'scope': 'general', 'tier': '3'}, {'809106.IB'}),
            ({'tier': '1', 'region': '河北'}, set()),
        ]
        for params, expected in examples:
            with self.subTest(params=params):
                self.assertEqual(self.codes(**params), expected)
        self.assertEqual(self.codes(scope='overall'), self.codes())

    def test_search_is_literal_and_matches_bond_code_name_and_alias(self):
        self.assertEqual(self.codes(q='809106'), {'809106.IB'})
        self.assertEqual(self.codes(q='测试沪债'), {'809102.IB'})
        self.assertEqual(self.codes(q='alias-809102'), {'809102.IB'})
        self.assertEqual(self.codes(q='%'), {'809102.IB'})
        self.assertEqual(self.codes(q='_'), set())
        self.assertEqual(self.codes(q="' OR 1=1 --"), set())

    def test_pages_have_stable_order_no_duplicates_and_no_evidence(self):
        expected = [bond['code'] for bond in self.dataset['bonds']]
        seen, versions = [], set()
        for number in range(1, 5):
            page = self.get('/page', page=number, page_size=2)
            self.assertEqual((page['page'], page['pageSize'], page['total']), (number, 2, 8))
            self.assertEqual(len(page['bonds']), 2)
            self.assertTrue(all(bond['fieldSources'] == {} for bond in page['bonds']))
            seen.extend(bond['code'] for bond in page['bonds'])
            versions.add(page['version'])
        self.assertEqual(seen, expected)
        self.assertEqual(len(versions), 1)

    def test_page_bounds_remain_bounded_and_invalid_filters_are_rejected(self):
        final_page = self.get('/page', page=999, page_size=2)
        self.assertEqual((final_page['page'], final_page['total']), (4, 8))
        self.assertEqual(len(final_page['bonds']), 2)
        for params in ({'page': 0}, {'page': -1}, {'page_size': 0}, {'page_size': 101},
                       {'scope': 'invalid'}, {'tier': '4'}, {'cohort': 'invalid'}, {'status': 'invalid'}):
            with self.subTest(params=params):
                response = self.client.get(f'/api/datasets/{TARGET}/available/page', params=params)
                self.assertEqual(response.status_code, 400, response.text)

    def test_point_read_keeps_alias_and_full_evidence_while_full_export_remains_complete(self):
        point = self.get('/ALIAS-809102')
        self.assertEqual(point['code'], '809102.IB')
        self.assertEqual(point['fieldSources']['yieldPct'][0]['requestId'], 'trade-809102')
        self.get('/page', status='eligible', page_size=1)
        export = self.get('')
        self.assertEqual(export['counts']['bonds'], 8)
        self.assertEqual(len(export['bonds']), 8)
        self.assertEqual({bond['code'] for bond in export['bonds']}, {bond['code'] for bond in self.bonds})

    def test_saved_update_insert_and_delete_refresh_index_without_stale_rows(self):
        before = self.get('/page')
        updated = deepcopy(self.bonds)
        updated = [bond for bond in updated if bond['code'] != '809105.IB']
        next(bond for bond in updated if bond['code'] == '809108.IB')['name'] = '已更新名称'
        added = deepcopy(updated[0])
        added.update(code='809109.IB', bondId='809109.IB', codes=['809109.IB'], name='新加入券')
        updated.append(added)
        provider._save_dataset(provider._dataset(TARGET, updated))
        after = self.get('/page')
        self.assertNotEqual(after['version'], before['version'])
        self.assertEqual(after['total'], 8)
        self.assertEqual(self.get('/809108.IB')['name'], '已更新名称')
        self.assertEqual(self.get('/809109.IB')['name'], '新加入券')
        self.assertEqual(self.client.get(f'/api/datasets/{TARGET}/available/809105.IB').status_code, 404)
        self.assertNotIn('809105.IB', self.codes())

    def test_projection_failure_rolls_back_primary_dataset_and_index_together(self):
        from backend import akshare_read_model as index
        old_dataset = provider.read_available(TARGET, True)
        old_summary = index.summary(TARGET)
        write_projection = index._write_projection
        def write_then_fail(*args):
            write_projection(*args)
            raise RuntimeError('simulated interruption after projection writes')
        revised = deepcopy(self.bonds)
        revised[0]['name'] = '必须回滚的变更'
        with patch.object(index, '_write_projection', side_effect=write_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                provider._save_dataset(provider._dataset(TARGET, revised))
        self.assertEqual(provider.read_available(TARGET, True), old_dataset)
        self.assertEqual(index.summary(TARGET), old_summary)
        self.assertEqual(index.bond(TARGET, revised[0]['code'])['name'], self.bonds[0]['name'])

    def test_warm_summary_page_and_point_never_parse_full_day_again(self):
        self.get('/summary')
        original_loads = json.loads
        lengths = []
        full_length = len(provider._dump(self.dataset))
        def tracked_loads(value, *args, **kwargs):
            if isinstance(value, (str, bytes, bytearray)):
                lengths.append(len(value))
            return original_loads(value, *args, **kwargs)
        with patch.object(json, 'loads', side_effect=tracked_loads), patch.object(
                provider, 'read_available', side_effect=AssertionError('Warm indexed reads must not load the full day')):
            self.get('/summary')
            self.get('/page', page_size=2)
            self.get('/809102.IB')
        self.assertTrue(lengths)
        self.assertLess(max(lengths), full_length / 2)

    def test_first_read_of_legacy_dataset_builds_index_without_acquisition(self):
        from backend import akshare_read_model as index
        legacy = '2026-09-10'
        legacy_bonds = deepcopy(self.bonds)
        for bond in legacy_bonds:
            if bond.get('yieldDate'):
                bond['yieldDate'] = legacy
        data = provider._dataset(legacy, legacy_bonds)
        with store.connection() as db:
            db.execute('INSERT INTO akshare_datasets VALUES (?,?,?)', (legacy, store.now(), provider._dump(data)))
        index.ensure_index(legacy)
        self.assertEqual(index.summary(legacy)['counts']['bonds'], 8)
        self.assertEqual(index.page(legacy)['total'], 8)
        self.assertEqual(index.bond(legacy, '809102.IB')['yieldDate'], legacy)
        self.assertEqual(self.get('/809102.IB')['yieldDate'], TARGET)

    def test_other_date_and_missing_bond_never_reuse_current_date(self):
        for suffix in ('/summary', '/page'):
            response = self.client.get('/api/datasets/2026-09-10/available' + suffix)
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload.get('total', payload.get('counts', {}).get('bonds')), 0)
        self.assertEqual(self.client.get('/api/datasets/2026-09-10/available/809102.IB').status_code, 404)
        for target in ('not-a-date', '2026-08-31'):
            self.assertEqual(self.client.get(f'/api/datasets/{target}/available/page').status_code, 400)

    def test_provider_switch_separates_same_code_quotes_and_index(self):
        run = store.enqueue(TARGET, provider='wind')
        recorder = Recorder('离线 Wind 分页测试', TARGET, run['runId'])
        request = recorder.begin('tools/call', {'params': {'name': 'get_bond_market_data'}})
        table = {'columns': [{'name': 'Wind代码'}, {'name': '证券简称'},
                            {'name': '2026年9月11日的收盘价收益率', 'unit': '%'}],
                 'rows': [['809108.IB', 'Wind 同代码样本', '2.7654']]}
        recorder.finish(request, {'result': {'content': [{'type': 'text', 'text': json.dumps(
            {'data': {'data': [table]}}, ensure_ascii=False)}]}}, http_status=200)
        store.fail(run['runId'], '离线样本')
        providers.select_provider('wind')
        wind = self.get('/page')
        self.assertEqual(wind['source'], 'wind')
        self.assertEqual(wind['total'], 1)
        self.assertEqual(wind['bonds'][0]['yieldPct'], '2.7654')
        self.assertEqual(self.get('/summary')['counts']['bonds'], 1)
        providers.select_provider('akshare')
        self.assertEqual(self.get('/page')['total'], 8)
        self.assertEqual(self.get('/809108.IB')['yieldPct'], '0')
        self.assertEqual(self.get('/summary')['source'], 'akshare')


if __name__ == '__main__':
    unittest.main()
