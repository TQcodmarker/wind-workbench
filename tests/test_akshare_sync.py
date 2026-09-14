from collections import Counter
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend import akshare_provider as provider, akshare_read_model as readmodel
from backend import akshare_sync as sync
from backend import storage as store


TARGET = '2026-09-11'


def catalog_row(code, year='2026', issued=None):
    return {'债券简称': '26河北'+code[-2:], '债券代码': code,
            '发行人/受托机构': '河北省政府', '债券类型': '地方政府债',
            '发行日期': issued or year+'-04-20', '查询代码': 'public-'+code}


def detail(row):
    return dict(bondCode=row['债券代码'], bondName=row['债券简称'],
                bondType='地方政府债', bondFullName='2026年河北省政府一般债券',
                entyFullName=row['发行人/受托机构'], issueDate=row['发行日期'],
                mrtyDate='2036-04-21', issueAmnt='20.5', parCouponRate='1.85',
                couponType='附息式固定利率', couponFrqncy='年',
                frstValueDate=row['发行日期'][:4]+'-04-21',
                frstCpnDt=str(int(row['发行日期'][:4])+1)+'-04-21')


def catalog_entry(rows, total=None, pages=1):
    raw = [dict(bondCode=row['债券代码'], bondName=row['债券简称'],
                entyFullName=row['发行人/受托机构'], bondType=row['债券类型'],
                issueStartDate=row['发行日期'], bondDefinedCode=row['查询代码']) for row in rows]
    return dict(responses=[dict(url='https://www.chinamoney.com.cn/ags/ms/cm-u-bond-md/BondMarketInfoList2',
                               requestBody='pageNo=1&pageSize=15', status=200,
                               body={'data': dict(total=len(rows) if total is None else total,
                                                  pageTotal=pages, resultList=raw)})])


class FakeRecorder:
    calls = []
    catalogs = {}
    failed_codes = set()
    denied_codes = set()
    pause_on_detail = False
    missing_page = False
    empty_sdk_error = False
    resume_then_timeout = None
    detail_overrides = {}

    def __init__(self, run, timeout_seconds=240, on_response=None):
        self.run, self.callback = run, on_response
        self.queries = []

    def query(self, name, arguments, columns, compatibility=False, resolved_lookup=None):
        type(self).calls.append((name, dict(arguments)))
        entry = dict(requestId='fake-'+str(len(type(self).calls)), runId=self.run['runId'],
                     function=name, arguments=arguments, retrievedAt='2026-09-12T12:00:00+08:00',
                     executionMode='compatibility_adapter' if compatibility else 'documented_sdk', responses=[])
        self.queries.append(entry)
        if self.resume_then_timeout == name:
            type(self).resume_then_timeout = None
            sync.pause(self.run['runId'])
            sync.resume(self.run['runId'])
            raise TimeoutError('old generation request timed out after resume')
        if name == 'bond_info_cm_query':
            assert arguments == {'symbol': '发行年份'}
            return [dict(name=year, code=year) for year in sorted(self.catalogs)], entry
        if name == 'bond_info_cm':
            assert arguments['bond_type'] == '地方政府债'
            rows = self.catalogs[arguments['issue_year']]
            entry.update(catalog_entry(rows, total=len(rows)+1 if self.missing_page else None))
            if self.callback:
                self.callback(entry['responses'][0])
            if not rows and self.empty_sdk_error:
                raise KeyError('empty dataframe columns')
            return rows, entry
        if name == 'bond_info_detail_cm':
            assert compatibility and len(resolved_lookup) == 1
            row = resolved_lookup[0]
            assert row['债券简称'] == arguments['symbol']
            if self.pause_on_detail:
                type(self).pause_on_detail = False
                sync.pause(self.run['runId'])
                if self.callback:
                    self.callback({'url': 'detail', 'body': {}, 'status': 200})
            if row['债券代码'] in self.failed_codes:
                raise TimeoutError('public upstream temporary failure')
            if row['债券代码'] in self.denied_codes:
                entry['responses'] = [{'status': 429, 'body': {}}]
                raise RuntimeError('upstream HTTP 429')
            values = detail(row)
            values.update(self.detail_overrides.get(row['债券代码'], {}))
            return [dict(name=key, value=value) for key, value in values.items()], entry
        raise AssertionError('Unexpected function '+name)


class FullMarketSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name)/'sync.sqlite3'
        self.patches = [patch.object(store, 'DB', self.db_path),
                        patch.object(sync, 'DETAIL_WORKERS', 1),
                        patch.object(provider, 'QueryRecorder', FakeRecorder),
                        patch.object(sync.time, 'sleep', return_value=None)]
        # The provider lock is added by integration in parallel; this keeps the
        # engine tests independent while testing its protected materialization.
        if not hasattr(provider, 'dataset_lock'):
            self.patches.append(patch.object(provider, 'dataset_lock', lambda: nullcontext(), create=True))
        for value in self.patches:
            value.start()
        FakeRecorder.calls = []
        FakeRecorder.catalogs = {'2025': [catalog_row('809331', '2025')],
                                '2026': [catalog_row('809332'), catalog_row('809333')]}
        FakeRecorder.failed_codes = set()
        FakeRecorder.denied_codes = set()
        FakeRecorder.pause_on_detail = FakeRecorder.missing_page = FakeRecorder.empty_sdk_error = False
        FakeRecorder.resume_then_timeout = None
        FakeRecorder.detail_overrides = {}
        sync.initialize()
        with store.connection() as db:
            db.execute('CREATE TABLE wind_preserve (value TEXT)')
            db.execute("INSERT INTO wind_preserve VALUES ('Wind untouched')")

    def tearDown(self):
        for value in reversed(self.patches):
            value.stop()
        self.temp.cleanup()

    def test_start_is_local_idempotent_and_rejects_other_scope_or_active_date(self):
        job = sync.start(TARGET)
        self.assertEqual(sync.start(TARGET)['jobId'], job['jobId'])
        self.assertEqual(FakeRecorder.calls, [])
        self.assertEqual(sync.pending_jobs(), [job['jobId']])
        self.assertIsNone(job['total'])
        with self.assertRaises(ValueError):
            sync.start(TARGET, 'excel')
        with self.assertRaises(ValueError):
            sync.start('2026-09-10')
        with self.assertRaises(KeyError):
            sync.pause('unknown')

    def test_all_catalog_bonds_sync_without_traded_universe(self):
        job = sync.start(TARGET)
        result = sync.execute(job['jobId'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['completed'], 3)
        self.assertTrue(result['catalogComplete'])
        self.assertFalse(result['marketDataComplete'])
        self.assertEqual(Counter(name for name, _ in FakeRecorder.calls),
                         Counter(bond_info_cm_query=1, bond_info_cm=2, bond_info_detail_cm=3))
        data = provider.read_available(TARGET, True)
        self.assertEqual(len(data['bonds']), 3)
        self.assertEqual(data['cells'], [])
        self.assertTrue(all(bond['yieldPct'] is None for bond in data['bonds']))
        self.assertTrue(all(bond['staticSyncStatus'] == 'completed' for bond in data['bonds']))
        with closing(sqlite3.connect(self.db_path)) as db:
            self.assertEqual(db.execute('SELECT value FROM wind_preserve').fetchone()[0], 'Wind untouched')

    def test_pause_keeps_all_catalog_placeholders_and_resume_skips_catalog(self):
        job = sync.start(TARGET)
        FakeRecorder.pause_on_detail = True
        paused = sync.execute(job['jobId'])
        self.assertEqual(paused['status'], 'paused')
        self.assertTrue(paused['catalogComplete'])
        self.assertEqual(len(provider.read_available(TARGET)['bonds']), 3)
        self.assertEqual(paused['completed'], 0)
        count = Counter(name for name, _ in FakeRecorder.calls)
        resumed = sync.resume(job['jobId'])
        self.assertEqual(resumed['jobId'], job['jobId'])
        self.assertEqual(resumed['status'], 'queued')
        self.assertEqual(sync.execute(job['jobId'])['status'], 'completed')
        after = Counter(name for name, _ in FakeRecorder.calls)
        self.assertEqual(after['bond_info_cm'], count['bond_info_cm'])
        self.assertEqual(after['bond_info_cm_query'], count['bond_info_cm_query'])

    def test_failures_are_not_completed_and_resume_retries_failed_only(self):
        job = sync.start(TARGET)
        FakeRecorder.failed_codes = {'809332'}
        result = sync.execute(job['jobId'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual((result['completed'], result['failed'], result['pending']), (2, 1, 0))
        calls = Counter(args.get('symbol') for name, args in FakeRecorder.calls if name == 'bond_info_detail_cm')
        failed_name = catalog_row('809332')['债券简称']
        self.assertEqual(calls[failed_name], 3)
        FakeRecorder.failed_codes = set()
        resumed = sync.resume(job['jobId'])
        self.assertEqual((resumed['completed'], resumed['failed'], resumed['pending']), (2, 0, 1))
        final = sync.execute(job['jobId'])
        self.assertEqual(final['status'], 'completed')
        final_calls = Counter(args.get('symbol') for name, args in FakeRecorder.calls if name == 'bond_info_detail_cm')
        self.assertEqual(final_calls[failed_name], 4)
        self.assertEqual(sum(final_calls.values()), sum(calls.values())+1)

    def test_cached_static_details_are_reused_only_for_exact_valid_identity(self):
        row = catalog_row('809332')
        provider._save_detail('809332.IB', detail(row), {'requestId': 'old-static', 'function': 'bond_info_detail_cm'})
        job = sync.start(TARGET)
        result = sync.execute(job['jobId'])
        self.assertEqual(result['completed'], 3)
        names = [args['symbol'] for name, args in FakeRecorder.calls if name == 'bond_info_detail_cm']
        self.assertNotIn(row['债券简称'], names)
        self.assertEqual(len(names), 2)

    def test_incomplete_pagination_cannot_claim_market_completion(self):
        FakeRecorder.missing_page = True
        job = sync.start(TARGET)
        result = sync.execute(job['jobId'])
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['catalogComplete'])
        self.assertIsNone(result['total'])
        self.assertEqual(result['catalogFailedPartitions'], 2)
        self.assertEqual(Counter(name for name, _ in FakeRecorder.calls)['bond_info_cm'], 6)

    def test_upstream_rate_limit_stops_requests_and_keeps_pending_for_resume(self):
        FakeRecorder.denied_codes = {'809331'}
        result = sync.execute(sync.start(TARGET)['jobId'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['pending'], 2)
        self.assertIn('429', result['message'])
        self.assertEqual(Counter(name for name, _ in FakeRecorder.calls)['bond_info_detail_cm'], 1)

    def test_explicit_zero_catalog_is_valid_despite_sdk_empty_parser_error(self):
        FakeRecorder.catalogs['2024'] = []
        FakeRecorder.empty_sdk_error = True
        result = sync.execute(sync.start(TARGET)['jobId'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['catalogCompletedPartitions'], 3)

    def test_future_issuance_not_labeled_as_target_day_universe(self):
        FakeRecorder.catalogs['2026'].append(catalog_row('809334', issued='2026-12-01'))
        result = sync.execute(sync.start(TARGET)['jobId'])
        self.assertEqual(result['total'], 3)
        self.assertNotIn('809334.IB', {bond['code'] for bond in provider.read_available(TARGET)['bonds']})

    def test_same_day_individual_trade_provenance_survives_static_sync(self):
        row = catalog_row('809332')
        observation = dict(bondcode='809332', showDate=TARGET+' 16:12:00', dmiLatestContraRate='1.95')
        bond = provider.normalize_bond(detail(row), TARGET, observation,
                                        {'indexYieldPct': '1.7', 'indexDurationYears': '9', 'curveNodes': []},
                                        trade_evidence={'requestId': 'real-trade', 'runId': 'market-query',
                                                        'function': 'bond_spot_deal', 'retrievedAt': '2026-09-12'})
        provider._save_dataset(provider._dataset(TARGET, [bond]))
        sync.execute(sync.start(TARGET)['jobId'])
        current = provider.read_available_bond(TARGET, '809332.IB')
        self.assertEqual(current['yieldPct'], '1.95')
        self.assertEqual(current['fieldSources']['yieldPct'][0]['requestId'], 'real-trade')
        self.assertEqual(current['fieldSources']['yieldPct'][0]['sourceDate'], TARGET)

    def test_interrupted_item_is_resumable_without_losing_completed_items(self):
        job = sync.start(TARGET)
        FakeRecorder.failed_codes = {'809332'}
        sync.execute(job['jobId'])
        with store.connection() as db:
            db.execute("UPDATE akshare_sync_items SET status='running',attempts=1 WHERE job_id=? AND code='809332.IB'", (job['jobId'],))
        sync._update_job(job['jobId'], status='running')
        FakeRecorder.failed_codes = set()
        result = sync.execute(job['jobId'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['completed'], 3)

    def test_old_enumeration_timeout_cannot_fail_new_resumed_generation(self):
        job = sync.start(TARGET)
        FakeRecorder.resume_then_timeout = 'bond_info_cm_query'
        result = sync.execute(job['jobId'])
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['generation'], 2)
        self.assertEqual(result['phase'], '等待继续同步')
        self.assertEqual(sync.pending_jobs(), [job['jobId']])
        self.assertEqual(sync.execute(job['jobId'])['status'], 'completed')

    def test_old_detail_timeout_cannot_fail_new_pending_item(self):
        job = sync.start(TARGET)
        FakeRecorder.resume_then_timeout = 'bond_info_detail_cm'
        result = sync.execute(job['jobId'])
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(result['generation'], 2)
        self.assertEqual((result['failed'], result['pending']), (0, 3))
        self.assertEqual(sync.execute(job['jobId'])['status'], 'completed')

    def test_progress_update_checks_generation_atomically_after_pause_race(self):
        initial = sync.start(TARGET)
        job = sync._update_job(initial['jobId'], status='running', phase='old-running')
        recorder = sync._recorder(job, 60, 'old-request-progress')
        original_check = sync._check
        def race_after_check(job_id, generation):
            original_check(job_id, generation)
            sync.pause(job_id)
            sync.resume(job_id)
        with patch.object(sync, '_check', race_after_check):
            with self.assertRaises(sync.SyncPaused):
                recorder.callback({'url': 'public', 'body': {}, 'status': 200})
        current = sync.get(job['jobId'])
        self.assertEqual((current['status'], current['generation'], current['phase']),
                         ('queued', 2, '等待继续同步'))
        with self.assertRaises(sync.SyncPaused):
            sync._update_owned(job, status='completed', phase='stale completion')
        self.assertEqual(sync.get(job['jobId'])['status'], 'queued')

    def test_missing_trade_code_preserves_official_identity_without_detail_retries(self):
        unknown = catalog_row('---')
        unknown['查询代码'] = 'unallocated-registry-1'
        FakeRecorder.catalogs['2026'].append(unknown)
        result = sync.execute(sync.start(TARGET)['jobId'])
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['catalogComplete'])
        self.assertEqual((result['total'], result['completed'], result['failed'], result['unmapped']), (4, 3, 1, 1))
        self.assertEqual(Counter(name for name, _ in FakeRecorder.calls)['bond_info_detail_cm'], 3)
        bond = provider.read_available_bond(TARGET, 'CFETS:unallocated-registry-1')
        self.assertEqual(bond['registryId'], 'unallocated-registry-1')
        self.assertIsNone(bond['tradingCode'])
        self.assertEqual(bond['codeKind'], 'official_registry_id')
        self.assertEqual(bond['fieldSources']['code'][0]['value'], 'unallocated-registry-1')
        self.assertIn('不是交易代码', bond['fieldSources']['code'][0]['name'])
        self.assertIsNone(bond['yieldPct'])

    def test_future_registry_without_trade_code_counts_for_pages_then_is_excluded(self):
        unknown = catalog_row('---', issued='2026-09-18')
        unknown['查询代码'] = 'future-unallocated-registry'
        FakeRecorder.catalogs['2026'].append(unknown)
        result = sync.execute(sync.start(TARGET)['jobId'])
        self.assertEqual((result['status'], result['total'], result['unmapped']), ('completed', 3, 0))

    def test_interrupted_enum_prefix_is_requeried_and_completed(self):
        job = sync.start(TARGET)
        sync._set_partition(job['jobId'], '2026', 'pending')
        result = sync.execute(job['jobId'])
        self.assertTrue(result['enumComplete'])
        self.assertEqual(set(result['enumYears']), {'2025', '2026'})
        self.assertEqual((result['catalogCompletedPartitions'], result['total']), (2, 3))
        self.assertEqual(Counter(name for name, _ in FakeRecorder.calls)['bond_info_cm_query'], 1)

    def test_enum_marker_and_partitions_commit_atomically(self):
        job = sync.start(TARGET)
        original_write = sync._write_job
        def crash_before_enum_commit(db, current):
            if current.get('enumComplete'):
                raise RuntimeError('interrupted before enum transaction commit')
            return original_write(db, current)
        with patch.object(sync, '_write_job', crash_before_enum_commit):
            failed = sync.execute(job['jobId'])
        self.assertEqual(failed['status'], 'failed')
        self.assertFalse(failed.get('enumComplete'))
        self.assertEqual(failed['catalogTotalPartitions'], 0)
        sync.resume(job['jobId'])
        final = sync.execute(job['jobId'])
        self.assertEqual((final['status'], final['catalogTotalPartitions'], final['total']), ('completed', 2, 3))

    def _record_catalog_query(self, job_id, query_status):
        records = FakeRecorder.catalogs['2026']
        entry = dict(catalog_entry(records), requestId='saved-complete-catalog', runId=job_id,
                     function='bond_info_cm', arguments={'bond_type': '地方政府债', 'issue_year': '2026'},
                     status=query_status, records=records, retrievedAt='2026-09-12T10:00:00+08:00')
        with store.connection() as db:
            db.execute('INSERT INTO akshare_queries VALUES (?,?,?,?,?,?)',
                       (entry['requestId'], job_id, TARGET, entry['retrievedAt'], 'bond_info_cm', json.dumps(entry)))

    def test_reuses_verified_succeeded_catalog_response_with_original_evidence(self):
        job = sync.start(TARGET)
        self._record_catalog_query(job['jobId'], 'succeeded')
        result = sync.execute(job['jobId'])
        self.assertEqual((result['status'], result['total']), ('completed', 3))
        queried_years = [args['issue_year'] for name, args in FakeRecorder.calls if name == 'bond_info_cm']
        self.assertEqual(queried_years, ['2025'])
        evidence = provider.read_available_bond(TARGET, '809332.IB')['catalogSource']
        self.assertEqual(evidence['requestId'], 'saved-complete-catalog')
        self.assertEqual(evidence['retrievedAt'], '2026-09-12T10:00:00+08:00')

    def test_failed_query_is_not_reused_as_completed_catalog(self):
        job = sync.start(TARGET)
        self._record_catalog_query(job['jobId'], 'failed')
        result = sync.execute(job['jobId'])
        self.assertEqual(result['status'], 'completed')
        queried_years = [args['issue_year'] for name, args in FakeRecorder.calls if name == 'bond_info_cm']
        self.assertEqual(queried_years, ['2026', '2025'])

    def _record_trade(self, code, yield_pct='1.95', observed_at=None, term='9.61Y', request_id=None):
        observed_at = observed_at or TARGET+' 17:12:00'
        request_id = request_id or 'saved-trade-'+code
        raw = dict(bondcode=code, showDate=observed_at, dmiLatestContraRate=yield_pct,
                   dmiLatestRate='101.25', termToMaturity=term,
                   abdAssetEncdShrtDesc=catalog_row(code)['债券简称'])
        entry = dict(requestId=request_id, runId='recorded-market-query',
                     function='bond_spot_deal', arguments={}, status='succeeded',
                     retrievedAt='2026-09-12T12:00:00+08:00',
                     responses=[dict(url='https://www.chinamoney.com.cn/ags/ms/cm-u-md-bond/CbtPri',
                                     status=200, body={'records': [raw]})],
                     records=[{'债券简称': raw['abdAssetEncdShrtDesc'], '最新收益率': yield_pct, '成交净价': '101.25'}])
        with store.connection() as db:
            db.execute('INSERT INTO akshare_queries VALUES (?,?,?,?,?,?)',
                       (request_id, entry['runId'], TARGET, entry['retrievedAt'], 'bond_spot_deal', json.dumps(entry)))
        return request_id

    def test_saved_trade_appears_before_detail_then_becomes_eligible_automatically(self):
        request_id = self._record_trade('809333')
        created = sync.start(TARGET)
        job = sync._update_job(created['jobId'], status='running')
        sync._catalog(job)
        partial = provider.read_available_bond(TARGET, '809333.IB')
        self.assertEqual(partial['yieldPct'], '1.95')
        self.assertEqual(partial['tradeObservedAt'], TARGET+'T17:12:00+08:00')
        self.assertEqual(partial['fieldSources']['yieldPct'][0]['requestId'], request_id)
        self.assertEqual(partial['disposition'], 'incomplete')
        self.assertIsNone(partial['remainingYears'])
        self.assertEqual(provider.read_available(TARGET)['cells'], [])
        current_job = sync.get(job['jobId'])
        self.assertEqual((current_job['marketObserved'], current_job['marketWithDetails'], current_job['marketEligible']), (1, 0, 0))
        sync._details(job)
        complete = provider.read_available_bond(TARGET, '809333.IB')
        self.assertEqual(complete['disposition'], 'eligible')
        self.assertEqual(complete['durationCalculation']['status'], 'estimated')
        self.assertIsNotNone(complete['duration'])
        self.assertEqual(complete['yieldPct'], '1.95')
        self.assertEqual(complete['tradeObservedAt'], partial['tradeObservedAt'])
        self.assertEqual(complete['fieldSources']['yieldPct'][0]['requestId'], request_id)
        published = provider.read_available(TARGET)['fullSync']
        current_job = sync.get(job['jobId'])
        for key in ('marketObserved', 'marketWithDetails', 'marketEligible'):
            self.assertEqual(published[key], current_job[key])
            self.assertEqual(current_job[key], 1)

    def test_full_sync_publishes_duration_group_for_previously_excluded_eleven_year_term(self):
        self._record_trade('809333')
        FakeRecorder.detail_overrides = {'809333': {'mrtyDate': '2037-04-21'}}
        result = sync.execute(sync.start(TARGET)['jobId'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['marketEligible'], 1)
        bond = provider.read_available_bond(TARGET, '809333.IB')
        self.assertGreater(float(bond['remainingYears']), 10.5)
        self.assertLess(float(bond['remainingYears']), 11.5)
        self.assertEqual(bond['termYears'], 10)
        self.assertEqual(bond['durationCalculation']['status'], 'estimated')
        self.assertEqual(readmodel.bond(TARGET, '809333.IB'), bond)
        summary = readmodel.summary(TARGET)
        self.assertEqual(summary['counts']['eligible'], 1)
        self.assertEqual(summary['counts']['durationEstimated'], 1)
        self.assertEqual(summary['counts']['durationUnavailable'], 2)
        self.assertTrue(summary['cells'])
        self.assertTrue(all(cell['termYears'] == 10 for cell in summary['cells']))

    def test_detail_priority_uses_saved_market_then_raw_tenor_only_as_queue_hint(self):
        self._record_trade('809332', term='8.2Y')
        self._record_trade('809333', term='9.61Y')
        result = sync.execute(sync.start(TARGET)['jobId'])
        detail_names = [args['symbol'] for name, args in FakeRecorder.calls if name == 'bond_info_detail_cm']
        self.assertEqual(detail_names, [catalog_row(code)['债券简称'] for code in ('809333', '809332', '809331')])
        # Raw remaining term only affects queue priority; the detail cashflows
        # and same-day yield supply the duration bucket used in aggregation.
        self.assertEqual(provider.read_available_bond(TARGET, '809332.IB')['termYears'], 10)
        self.assertEqual(result['marketEligible'], 2)

    def test_new_saved_trade_reorders_next_batch_without_worker_restart(self):
        original_materialize = sync.materialize
        inserted = False
        def save_and_add_observation(job_id):
            nonlocal inserted
            result = original_materialize(job_id)
            detail_calls = sum(name == 'bond_info_detail_cm' for name, _ in FakeRecorder.calls)
            if detail_calls == 1 and not inserted:
                self._record_trade('809333')
                inserted = True
            return result
        with patch.object(sync, 'PUBLISH_EVERY', 1), patch.object(sync, 'materialize', save_and_add_observation):
            result = sync.execute(sync.start(TARGET)['jobId'])
        detail_names = [args['symbol'] for name, args in FakeRecorder.calls if name == 'bond_info_detail_cm']
        self.assertEqual(detail_names, [catalog_row(code)['债券简称'] for code in ('809331', '809333', '809332')])
        self.assertEqual(result['marketEligible'], 1)

    def test_conflicted_cached_observation_does_not_retain_old_eligible_quote(self):
        self._record_trade('809333')
        result = sync.execute(sync.start(TARGET)['jobId'])
        self.assertEqual(result['marketEligible'], 1)
        self._record_trade('809333', yield_pct='2.10', request_id='conflicting-same-time-trade')
        sync.materialize(result['jobId'])
        bond = provider.read_available_bond(TARGET, '809333.IB')
        self.assertIsNone(bond['yieldPct'])
        self.assertEqual(bond['disposition'], 'incomplete')
        self.assertEqual(sync.get(result['jobId'])['marketObserved'], 0)
        self.assertEqual(sync.get(result['jobId'])['marketEligible'], 0)


if __name__ == '__main__':
    unittest.main()
