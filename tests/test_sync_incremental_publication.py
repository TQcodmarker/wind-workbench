"""Incremental full-sync publication must match a cold rebuild.

All records, raw quotes and task transitions are local fixtures in a temporary
database. Normalization call counts are the deterministic performance signal.
"""
from copy import deepcopy
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import akshare_provider as provider, akshare_read_model as readmodel, akshare_sync as sync, storage as store


TARGET = '2026-09-11'
FROZEN_TIME = '2026-09-12T12:00:00+08:00'
POOL_SIZE = 12


class IncrementalPublicationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(store, 'DB', Path(directory) / 'incremental.sqlite3'))
        self.enterContext(patch.object(store, 'MODE', 'wind'))
        self.enterContext(patch.object(store, 'now', return_value=FROZEN_TIME))
        self.enterContext(patch('requests.sessions.Session.send', side_effect=AssertionError('No external HTTP')))
        self.enterContext(patch('backend.wind_mcp.WindMCP.__aenter__', side_effect=AssertionError('No Wind session')))
        # Simulate a fresh synchronization process without coupling the tests to
        # a private cache variable. No database state is removed by reloading.
        importlib.reload(sync)
        store.initialize(seed=False)
        provider.initialize(seed=False)
        sync.initialize()
        self.job = sync.start(TARGET)
        self.job = sync._update_job(self.job['jobId'], status='running', catalogComplete=True)
        self.codes = [str(809300 + index) + '.IB' for index in range(POOL_SIZE)]
        for code in self.codes:
            catalog = self.catalog(code)
            payload = dict(catalog=catalog, year='2026', evidence=self.evidence('catalog-' + code, 'bond_info_cm'))
            with store.connection() as db:
                db.execute('INSERT INTO akshare_sync_items VALUES (?,?,?,?,?,?,?)',
                           (self.job['jobId'], code, 'pending', 0, provider._dump(payload), None, store.now()))
        self.complete_detail(self.codes[0])
        self.add_raw_trades('initial-trades', [self.trade(self.codes[0], '1.91'), self.trade(self.codes[1], '1.92')])

    @staticmethod
    def catalog(code):
        return {'债券代码': code.removesuffix('.IB'), '债券简称': '26河北' + code[:6],
                '发行人/受托机构': '河北省人民政府', '债券类型': '地方政府债',
                '发行日期': '2026-04-20', '查询代码': 'fixture-' + code}

    @staticmethod
    def evidence(request_id, function='bond_info_detail_cm'):
        return dict(requestId=request_id, runId='fixture-observations', function=function,
                    retrievedAt=FROZEN_TIME, executionMode='documented_sdk')

    def detail(self, code, **changes):
        value = dict(bondCode=code.removesuffix('.IB'), bondName=self.catalog(code)['债券简称'],
                     bondType='地方政府债', bondFullName='2026年河北省政府一般债券',
                     entyFullName='河北省人民政府', issueDate='2026-04-20', mrtyDate='2036-04-21',
                     issueAmnt='20.5', parCouponRate='1.85', couponType='附息式固定利率',
                     couponFrqncy='年', frstValueDate='2026-04-21', frstCpnDt='2027-04-21')
        value.update(changes)
        return value

    def complete_detail(self, code, **changes):
        provider._save_detail(code, self.detail(code, **changes), self.evidence('detail-' + code))
        with store.connection() as db:
            db.execute("UPDATE akshare_sync_items SET status='completed',updated_at=? WHERE job_id=? AND code=?",
                       (store.now(), self.job['jobId'], code))

    @staticmethod
    def trade(code, value, observed='16:00:00'):
        return dict(bondcode=code.removesuffix('.IB'), showDate=TARGET + ' ' + observed,
                    dmiLatestContraRate=value, dmiLatestRate='101.25')

    def add_raw_trades(self, request_id, rows):
        entry = dict(self.evidence(request_id, 'bond_spot_deal'), status='succeeded', source='akshare',
                     arguments={}, responses=[dict(
                         url='https://www.chinamoney.com.cn/ags/ms/cm-u-md-bond/CbtPri', status=200,
                         body={'records': rows})])
        with store.connection() as db:
            db.execute('INSERT INTO akshare_queries VALUES (?,?,?,?,?,?)',
                       (request_id, entry['runId'], TARGET, store.now(), 'bond_spot_deal', provider._dump(entry)))

    def saved(self):
        return provider.read_available(TARGET, True)

    @staticmethod
    def comparable(dataset):
        value = deepcopy(dataset)
        value.pop('version', None)
        if value.get('fullSync'):
            value['fullSync'].pop('updatedAt', None)
        if value.get('provenance'):
            value['provenance'].pop('collectedAt', None)
        return value

    def assert_cold_equivalent(self, warm):
        importlib.reload(sync)
        with patch.object(provider, 'normalize_bond', wraps=provider.normalize_bond) as normalize:
            sync.materialize(self.job['jobId'])
        self.assertGreaterEqual(normalize.call_count, POOL_SIZE, 'A fresh process must rebuild the catalog')
        self.assertEqual(self.comparable(self.saved()), self.comparable(warm))

    def test_one_new_detail_normalizes_one_bond_and_matches_cold_rebuild(self):
        sync.materialize(self.job['jobId'])
        before = self.saved()
        self.assertEqual(before['counts']['bonds'], POOL_SIZE)
        self.complete_detail(self.codes[1])
        with patch.object(provider, 'normalize_bond', wraps=provider.normalize_bond) as normalize:
            sync.materialize(self.job['jobId'])
        after = self.saved()
        self.assertEqual(normalize.call_count, 1, 'One changed detail must not normalize the whole catalog')
        self.assertEqual(normalize.call_args.args[0]['bondCode'], self.codes[1].removesuffix('.IB'))
        self.assertEqual(after['counts']['eligible'], before['counts']['eligible'] + 1)
        self.assertEqual(after['counts']['durationEstimated'], before['counts']['durationEstimated'] + 1)
        self.assertEqual(after['counts']['durationUnavailable'], before['counts']['durationUnavailable'] - 1)
        self.assertEqual(next(bond for bond in after['bonds'] if bond['code'] == self.codes[1])['yieldPct'], '1.92')
        self.assert_cold_equivalent(after)

    def test_no_input_change_does_not_normalize_any_bond(self):
        sync.materialize(self.job['jobId'])
        before = self.saved()
        with patch.object(provider, 'normalize_bond', wraps=provider.normalize_bond) as normalize:
            sync.materialize(self.job['jobId'])
        self.assertEqual(normalize.call_count, 0, 'An unchanged batch must reuse all normalized bonds')
        self.assertEqual(self.comparable(self.saved()), self.comparable(before))

    def test_changed_duration_rules_invalidate_warm_cache_and_match_cold_rebuild(self):
        self.complete_detail(self.codes[0], mrtyDate='2037-04-21')
        with patch.dict(provider.RULES, version='rules-akshare-v1-dated-trade', mappingVersion='akshare-local-v1'):
            sync.materialize(self.job['jobId'])
            self.assertEqual(self.saved()['rulesVersion'], 'rules-akshare-v1-dated-trade')
        with patch.object(provider, 'normalize_bond', wraps=provider.normalize_bond) as normalize:
            sync.materialize(self.job['jobId'])
        self.assertGreaterEqual(normalize.call_count, POOL_SIZE)
        after = self.saved()
        self.assertEqual(after['rulesVersion'], provider.RULES['version'])
        self.assertEqual(after['mappingVersion'], provider.RULES['mappingVersion'])
        bond = next(bond for bond in after['bonds'] if bond['code'] == self.codes[0])
        self.assertEqual(bond['termYears'], 10)
        self.assertEqual(bond['durationCalculation']['status'], 'estimated')
        self.assertEqual(after['counts']['durationEstimated'], 1)
        self.assertEqual(after['counts']['durationUnavailable'], POOL_SIZE - 1)
        self.assert_cold_equivalent(after)

    def test_external_dataset_version_is_merged_without_losing_quote_or_new_bond(self):
        sync.materialize(self.job['jobId'])
        previous = self.saved()
        old_version = readmodel.summary(TARGET)['version']
        incoming = provider.normalize_bond(
            self.detail(self.codes[0]), TARGET, self.trade(self.codes[0], '2.12', '17:00:00'),
            detail_evidence=self.evidence('normal-detail'),
            trade_evidence=self.evidence('normal-new-quote', 'bond_spot_deal'))
        old = next(bond for bond in previous['bonds'] if bond['code'] == self.codes[0])
        incoming.update({key: old[key] for key in ('staticSyncStatus', 'catalogSource', 'syncJobId')})
        new_code = '819999.IB'
        outside_catalog = provider.normalize_bond(
            self.detail(new_code), TARGET, self.trade(new_code, '1.99'),
            detail_evidence=self.evidence('normal-added-detail'),
            trade_evidence=self.evidence('normal-added-quote', 'bond_spot_deal'))
        rows = [incoming if bond['code'] == self.codes[0] else bond for bond in previous['bonds']]
        external = provider._dataset(TARGET, [*rows, outside_catalog], previous['benchmarks'],
                                     previous['provenance'], previous['warnings'])
        external['fullSync'] = previous['fullSync']
        provider._save_dataset(external)
        self.assertNotEqual(readmodel.summary(TARGET)['version'], old_version)
        with patch.object(provider, 'normalize_bond', wraps=provider.normalize_bond) as normalize:
            sync.materialize(self.job['jobId'])
        self.assertEqual(normalize.call_count, POOL_SIZE, 'An external publisher invalidates the cached source dataset')
        after = self.saved()
        bonds = {bond['code']: bond for bond in after['bonds']}
        self.assertEqual(len(bonds), POOL_SIZE + 1)
        self.assertEqual(bonds[self.codes[0]]['yieldPct'], '2.12')
        self.assertEqual(bonds[self.codes[0]]['fieldSources']['yieldPct'][0]['requestId'], 'normal-new-quote')
        self.assertEqual(bonds[new_code], outside_catalog)
        self.assert_cold_equivalent(after)

    def test_new_same_day_raw_quote_only_recalculates_its_bond(self):
        sync.materialize(self.job['jobId'])
        self.add_raw_trades('new-raw-quote', [self.trade(self.codes[2], '2.05', '17:00:00')])
        with patch.object(provider, 'normalize_bond', wraps=provider.normalize_bond) as normalize:
            sync.materialize(self.job['jobId'])
        self.assertEqual(normalize.call_count, 1)
        self.assertEqual(normalize.call_args.args[0]['bondCode'], self.codes[2].removesuffix('.IB'))
        after = self.saved()
        bond = next(bond for bond in after['bonds'] if bond['code'] == self.codes[2])
        self.assertEqual(bond['yieldPct'], '2.05')
        self.assertEqual(bond['fieldSources']['yieldPct'][0]['requestId'], 'new-raw-quote')
        self.assertEqual(bond['disposition'], 'incomplete', 'A newly observed pending detail must not become eligible')
        self.assertEqual(after['fullSync']['marketObserved'], 3)
        self.assert_cold_equivalent(after)

    def test_same_time_raw_conflict_only_recalculates_and_quarantines_its_bond(self):
        sync.materialize(self.job['jobId'])
        before = self.saved()
        self.add_raw_trades('conflicting-raw-quote', [self.trade(self.codes[0], '2.10')])
        with patch.object(provider, 'normalize_bond', wraps=provider.normalize_bond) as normalize:
            sync.materialize(self.job['jobId'])
        self.assertEqual(normalize.call_count, 1)
        self.assertEqual(normalize.call_args.args[0]['bondCode'], self.codes[0].removesuffix('.IB'))
        after = self.saved()
        bond = next(bond for bond in after['bonds'] if bond['code'] == self.codes[0])
        self.assertIsNone(bond['yieldPct'])
        self.assertIsNone(bond['duration'])
        self.assertIsNone(bond['termYears'])
        self.assertEqual(bond['durationCalculation']['status'], 'unavailable')
        self.assertNotEqual(bond['disposition'], 'eligible')
        self.assertEqual(after['counts']['eligible'], before['counts']['eligible'] - 1)
        self.assertEqual(after['fullSync']['marketObserved'], 1)
        self.assert_cold_equivalent(after)

    def test_other_day_raw_quote_does_not_invalidate_current_day_bonds(self):
        sync.materialize(self.job['jobId'])
        before = self.saved()
        raw = self.trade(self.codes[0], '9.99')
        raw['showDate'] = '2026-09-10 16:00:00'
        self.add_raw_trades('other-day-quote', [raw])
        with patch.object(provider, 'normalize_bond', wraps=provider.normalize_bond) as normalize:
            sync.materialize(self.job['jobId'])
        self.assertEqual(normalize.call_count, 0)
        self.assertEqual(self.comparable(self.saved()), self.comparable(before))

    def assert_rejected_commit_keeps_durable_data_and_cache(self, replacement_generation):
        sync.materialize(self.job['jobId'])
        before, cache = self.saved(), deepcopy(sync._PUBLICATION_CACHE)
        version = readmodel.summary(TARGET)['version']
        self.complete_detail(self.codes[1])
        save_delta = readmodel.save_delta

        def interrupt_before_commit(*args, **kwargs):
            if replacement_generation:
                # The new owner retains the task ID but owns a new generation.
                current = sync.get(self.job['jobId'])
                sync._update_job(self.job['jobId'], generation=current['generation'] + 1, status='running')
            else:
                sync.pause(self.job['jobId'])
            return save_delta(*args, **kwargs)

        with patch.object(readmodel, 'save_delta', side_effect=interrupt_before_commit):
            with self.assertRaises(sync.SyncPaused):
                sync.materialize(self.job['jobId'])
        self.assertEqual(readmodel.summary(TARGET)['version'], version)
        self.assertEqual(self.saved(), before, 'A rejected transaction must not publish rows or metadata')
        self.assertEqual(sync._PUBLICATION_CACHE, cache, 'Rejected normalized rows must not poison the reusable cache')
        sync._update_job(self.job['jobId'], status='running')
        with patch.object(provider, 'normalize_bond', wraps=provider.normalize_bond) as normalize:
            sync.materialize(self.job['jobId'])
        self.assertEqual(normalize.call_count, 1, 'The uncommitted changed bond must still be recalculated on retry')
        after = self.saved()
        self.assertEqual(after['counts']['eligible'], before['counts']['eligible'] + 1)
        self.assertEqual(after['fullSync']['generation'], sync.get(self.job['jobId'])['generation'])
        self.assert_cold_equivalent(after)

    def test_pause_before_commit_rejects_publication_without_poisoning_cache(self):
        self.assert_rejected_commit_keeps_durable_data_and_cache(replacement_generation=False)

    def test_generation_change_before_commit_rejects_publication_without_poisoning_cache(self):
        self.assert_rejected_commit_keeps_durable_data_and_cache(replacement_generation=True)


if __name__ == '__main__':
    unittest.main()
