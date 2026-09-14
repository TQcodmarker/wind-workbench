"""A normal market refresh preserves catalog rows materialized during its I/O."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import akshare_provider as provider, akshare_sync as sync, storage as store


TARGET = '2026-09-10'


class SyncDatasetMergeTests(unittest.TestCase):
    def test_normal_refresh_merges_concurrent_catalog_and_keeps_same_day_trade(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(store, 'DB', Path(directory) / 'merge.sqlite3'), \
             patch.object(store, 'MODE', 'wind'):
            store.initialize(seed=False)
            provider.initialize(seed=False)
            sync.initialize()
            detail = dict(bondCode='809336', bondName='26河北23', bondType='地方政府债',
                          bondFullName='河北省政府一般债', entyFullName='河北省人民政府',
                          issueDate='2025-08-07', mrtyDate='2036-09-10',
                          issueAmnt='10', parCouponRate='1.89', couponType='附息式固定利率',
                          couponFrqncy='年', frstValueDate='2025-09-10', frstCpnDt='2026-09-10')
            detail_evidence = {'requestId': 'old-detail', 'function': 'bond_info_detail_cm'}
            trade_evidence = {'requestId': 'same-day-trade', 'function': 'bond_spot_deal',
                              'runId': 'previous-market-query', 'retrievedAt': TARGET + 'T16:00:00+08:00'}
            benchmarks = {'indexYieldPct': '2', 'indexDurationYears': '9', 'curveNodes': []}
            bond = provider.normalize_bond(detail, TARGET,
                {'bondcode': '809336', 'showDate': TARGET, 'dmiLatestContraRate': '1.95'},
                benchmarks, detail_evidence, trade_evidence)
            provider._save_detail('809336.IB', detail, detail_evidence)
            provider._save_dataset(provider._dataset(TARGET, [bond], benchmarks))
            job = sync.start(TARGET)
            run = store.enqueue(TARGET, provider='akshare')
            testcase = self

            class ConcurrentRecorder:
                def __init__(self, submitted_run, **kwargs):
                    self.run = submitted_run
                    self.queries = []
                    self.deadline = time.monotonic() + 240

                def query(self, name, arguments, columns, **kwargs):
                    entry = dict(requestId='new-query-' + str(len(self.queries)),
                                 runId=self.run['runId'], function=name, arguments=arguments,
                                 responses=[{'body': {'records': []}}])
                    self.queries.append(entry)
                    if name == 'bond_spot_deal':
                        # collect() already read its original dataset. The full
                        # catalog process now publishes a newly discovered bond.
                        catalog = {'债券代码': '809337', '债券简称': '26河北24',
                                   '发行人/受托机构': '河北省人民政府', '债券类型': '地方政府债',
                                   '发行日期': '2026-04-01', '查询代码': 'public-809337'}
                        payload = {'catalog': catalog, 'year': '2026',
                                   'evidence': {'requestId': 'catalog-query', 'function': 'bond_info_cm'}}
                        with store.connection() as db:
                            db.execute('INSERT INTO akshare_sync_items VALUES (?,?,?,?,?,?,?)',
                                       (job['jobId'], '809337.IB', 'pending', 0,
                                        provider._dump(payload), None, store.now()))
                        sync.materialize(job['jobId'])
                        testcase.assertEqual(provider.read_available(TARGET)['counts']['bonds'], 2)
                        return [], entry
                    if name in ('bond_index_general_cbond', 'bond_china_close_return'):
                        # No new observations: same-day values must survive.
                        return [], entry
                    raise AssertionError('Unexpected live query: ' + name)

            with patch.object(provider, 'QueryRecorder', ConcurrentRecorder), \
                 patch('backend.wind_mcp.WindMCP.__aenter__', side_effect=AssertionError('Wind must not run')), \
                 patch('requests.sessions.Session.send', side_effect=AssertionError('No external HTTP')):
                result = provider.collect(run)
            saved = provider.read_available(TARGET, True)
            by_code = {item['code']: item for item in saved['bonds']}
            self.assertEqual(set(by_code), {'809336.IB', '809337.IB'})
            self.assertEqual(result['counts']['bonds'], 2)
            self.assertEqual(by_code['809336.IB']['yieldPct'], '1.95')
            self.assertEqual(by_code['809336.IB']['fieldSources']['yieldPct'][0]['requestId'], 'same-day-trade')
            self.assertEqual(by_code['809336.IB']['fieldSources']['yieldPct'][0]['sourceDate'], TARGET)
            self.assertEqual(by_code['809337.IB']['staticSyncStatus'], 'pending')
            self.assertEqual(by_code['809337.IB']['syncJobId'], job['jobId'])
            self.assertIsNone(by_code['809337.IB']['yieldPct'])
            self.assertEqual(saved['fullSync']['jobId'], job['jobId'])
            self.assertEqual(saved['counts']['eligible'], 1)

    def test_normal_refresh_uses_static_details_completed_during_its_network_requests(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(store, 'DB', Path(directory) / 'static-merge.sqlite3'), \
             patch.object(store, 'MODE', 'wind'):
            store.initialize(seed=False)
            provider.initialize(seed=False)
            sync.initialize()
            full_detail = dict(bondCode='809336', bondName='26河北23', bondType='地方政府债',
                               bondFullName='河北省政府一般债', entyFullName='河北省人民政府',
                               issueDate='2025-08-07', mrtyDate='2036-09-10',
                               issueAmnt='10', parCouponRate='1.89', couponType='附息式固定利率',
                               couponFrqncy='年', frstValueDate='2025-09-10', frstCpnDt='2026-09-10')
            partial_detail = dict(full_detail)
            partial_detail.pop('mrtyDate')
            new_detail = dict(full_detail, bondCode='809337', bondName='26河北24')
            benchmarks = {'indexYieldPct': '2', 'indexDurationYears': '9', 'curveNodes': []}
            provider._save_detail('809336.IB', partial_detail, {'requestId': 'old-incomplete'})
            provider._save_dataset(provider._dataset(TARGET, [
                provider.normalize_bond(partial_detail, TARGET, benchmarks=benchmarks),
            ], benchmarks))
            job = sync.start(TARGET)
            run = store.enqueue(TARGET, provider='akshare')

            def catalog(detail):
                return {'债券代码': detail['bondCode'], '债券简称': detail['bondName'],
                        '债券类型': '地方政府债', '发行人/受托机构': detail['entyFullName'],
                        '发行日期': detail['issueDate'], '查询代码': 'lookup-' + detail['bondCode']}

            with store.connection() as db:
                payload = {'catalog': catalog(full_detail), 'year': '2025',
                           'evidence': {'requestId': 'catalog-query', 'function': 'bond_info_cm'}}
                db.execute('INSERT INTO akshare_sync_items VALUES (?,?,?,?,?,?,?)',
                           (job['jobId'], '809336.IB', 'pending', 0,
                            provider._dump(payload), None, store.now()))
            testcase = self

            class UpgradingRecorder:
                def __init__(self, submitted_run, **kwargs):
                    self.run = submitted_run
                    self.queries = []
                    self.deadline = time.monotonic() + 240

                def query(self, name, arguments, columns, **kwargs):
                    entry = dict(requestId='query-' + str(len(self.queries)),
                                 runId=self.run['runId'], function=name, arguments=arguments,
                                 responses=[])
                    self.queries.append(entry)
                    if name == 'bond_spot_deal':
                        entry['responses'] = [{'body': {'records': [dict(
                            bondcode='809337', abdAssetEncdShrtDesc='26河北24',
                            showDate=TARGET, dmiLatestContraRate='1.97',
                        )]}}]
                        return [], entry
                    if name in ('bond_index_general_cbond', 'bond_china_close_return'):
                        return [], entry
                    if name == 'bond_info_cm':
                        # Normal collection has cached the incomplete 809336
                        # detail already. Full sync now finishes that detail.
                        provider._save_detail('809336.IB', full_detail, {
                            'requestId': 'new-complete-static', 'function': 'bond_info_detail_cm',
                        })
                        with store.connection() as db:
                            db.execute("UPDATE akshare_sync_items SET status='completed' WHERE job_id=? AND code=?",
                                       (job['jobId'], '809336.IB'))
                        sync.materialize(job['jobId'])
                        testcase.assertEqual(provider.read_available_bond(TARGET, '809336.IB')['maturityDate'], '2036-09-10')
                        return [catalog(new_detail)], entry
                    if name == 'bond_info_detail_cm':
                        return [{'name': key, 'value': value} for key, value in new_detail.items()], entry
                    raise AssertionError('Unexpected live query: ' + name)

            with patch.object(provider, 'QueryRecorder', UpgradingRecorder), \
                 patch('requests.sessions.Session.send', side_effect=AssertionError('No external HTTP')):
                provider.collect(run)
            saved = provider.read_available_bond(TARGET, '809336.IB')
            self.assertEqual(saved['maturityDate'], '2036-09-10')
            self.assertEqual(saved['staticSyncStatus'], 'completed')
            self.assertEqual(saved['fieldSources']['maturityDate'][0]['requestId'], 'new-complete-static')
            self.assertEqual(provider.read_available(TARGET)['counts']['bonds'], 2)

    def test_normal_refresh_updates_or_quarantines_pending_catalog_yields(self):
        for conflicted in (False, True):
            with self.subTest(conflicted=conflicted), tempfile.TemporaryDirectory() as directory, \
                 patch.object(store, 'DB', Path(directory)/'pending-quote.sqlite3'), \
                 patch.object(store, 'MODE', 'wind'):
                store.initialize(seed=False)
                provider.initialize(seed=False)
                sync.initialize()
                job = sync.start(TARGET)
                catalog_source = dict(requestId='original-catalog-query', runId=job['jobId'],
                                      function='bond_info_cm', retrievedAt=TARGET+'T10:00:00+08:00')
                catalog = {'债券代码': '809337', '债券简称': '26河北24',
                           '发行人/受托机构': '河北省人民政府', '债券类型': '地方政府债',
                           '发行日期': '2026-04-01', '查询代码': 'public-809337'}
                with store.connection() as db:
                    db.execute('INSERT INTO akshare_sync_items VALUES (?,?,?,?,?,?,?)',
                               (job['jobId'], '809337.IB', 'pending', 0,
                                provider._dump({'catalog': catalog, 'year': '2026', 'evidence': catalog_source}),
                                None, store.now()))
                partial_detail = dict(bondCode='809337', bondName='26河北24', bondType='地方政府债',
                                      entyFullName='河北省人民政府', issueDate='2026-04-01')
                observed_at = TARGET+' 17:00:00'
                previous = provider.normalize_bond(partial_detail, TARGET,
                    dict(bondcode='809337', showDate=observed_at if conflicted else TARGET+' 16:00:00',
                         dmiLatestContraRate='1.96' if conflicted else '1.85', dmiLatestRate='101.25'),
                    detail_evidence=catalog_source,
                    trade_evidence={'requestId': 'old-trade', 'function': 'bond_spot_deal'})
                previous.update(staticSyncStatus='pending', catalogSource=catalog_source, syncJobId=job['jobId'])
                provider._save_dataset(provider._dataset(TARGET, [previous]))
                run = store.enqueue(TARGET, provider='akshare')

                class PendingMarketRecorder:
                    def __init__(self, submitted_run, **kwargs):
                        self.run = submitted_run
                        self.queries = []
                        self.deadline = time.monotonic()+240

                    def query(self, name, arguments, columns, **kwargs):
                        entry = dict(requestId='incoming-'+str(len(self.queries)), runId=self.run['runId'],
                                     function=name, arguments=arguments, status='succeeded',
                                     retrievedAt='2026-09-12T12:00:00+08:00', responses=[])
                        self.queries.append(entry)
                        if name == 'bond_spot_deal':
                            raw = dict(bondcode='809337', abdAssetEncdShrtDesc='26河北24', showDate=observed_at,
                                       dmiLatestContraRate='1.96', dmiLatestRate='101.25', termToMaturity='9.6Y')
                            records = [raw, dict(raw, dmiLatestContraRate='2.10')] if conflicted else [raw]
                            entry['responses'] = [dict(url='https://www.chinamoney.com.cn/ags/ms/cm-u-md-bond/CbtPri',
                                                       status=200, body={'records': records})]
                            with store.connection() as db:
                                db.execute('INSERT INTO akshare_queries VALUES (?,?,?,?,?,?)',
                                           (entry['requestId'], entry['runId'], TARGET, entry['retrievedAt'],
                                            name, provider._dump(entry)))
                            return [], entry
                        if name == 'bond_index_general_cbond':
                            return [{'date': TARGET, 'value': '2'}], entry
                        if name == 'bond_china_close_return':
                            return [], entry
                        if name == 'bond_info_cm':
                            raise TimeoutError('Details still unavailable; keep the catalog placeholder')
                        raise AssertionError('Unexpected query: '+name)

                with patch.object(provider, 'QueryRecorder', PendingMarketRecorder), \
                     patch('requests.sessions.Session.send', side_effect=AssertionError('No external HTTP')), \
                     patch('backend.wind_mcp.WindMCP.__aenter__', side_effect=AssertionError('Wind must not run')):
                    provider.collect(run)
                saved = provider.read_available_bond(TARGET, '809337.IB')
                self.assertEqual(saved['catalogSource'], catalog_source)
                self.assertEqual(saved['staticSyncStatus'], 'pending')
                self.assertEqual(saved['syncJobId'], job['jobId'])
                self.assertEqual(saved['disposition'], 'incomplete')
                self.assertIsNone(saved['remainingYears'])
                self.assertEqual(provider.read_available(TARGET)['counts']['eligible'], 0)
                if conflicted:
                    self.assertIsNone(saved['yieldPct'])
                    self.assertIsNone(saved['tradeObservedAt'])
                else:
                    self.assertEqual(saved['yieldPct'], '1.96')
                    self.assertEqual(saved['tradeObservedAt'], TARGET+'T17:00:00+08:00')
                    self.assertEqual(saved['fieldSources']['yieldPct'][0]['requestId'], 'incoming-0')


if __name__ == '__main__':
    unittest.main()
