import json
from contextlib import closing
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend import akshare_provider as provider
from backend import storage as store


TARGET = '2026-09-11'


def detail(code='809336', **overrides):
    values = dict(bondCode=code, bondType='地方政府债', bondName='26河北23',
                bondFullName='2026年河北省政府一般债券', entyFullName='河北省政府',
                issueDate='2026-04-20', mrtyDate='2036-04-21',
                issueAmnt='88.2', parCouponRate='1.89', couponType='附息式固定利率',
                couponFrqncy='年', frstValueDate='2026-04-21', frstCpnDt='2027-04-21')
    values.update(overrides)
    return values


def trade(code='809336', target=TARGET, value='1.76'):
    return dict(bondcode=code, showDate=target+' 16:20:00',
                dmiLatestContraRate=value, dmiLatestRate='101.25')


BENCHMARKS = dict(indexYieldPct='1.70', indexDurationYears='8.95',
                  curveNodes=[['0.083', '1.0'], ['10', '1.8'], ['30', '2.2']])


class AKShareNormalizationTests(unittest.TestCase):
    def test_index_and_issue_size_never_impersonate_individual_metrics(self):
        bond = provider.normalize_bond(detail(), TARGET, benchmarks=BENCHMARKS)
        self.assertIsNone(bond['yieldPct'])
        self.assertIsNone(bond['duration'])
        self.assertIsNone(bond['outstandingBalanceYi'])
        self.assertIsNone(bond['closeNetPrice'])
        self.assertEqual(bond['issueAmountYi'], '88.2')
        self.assertEqual(bond['referenceFields']['indexYieldPct'], '1.70')
        self.assertEqual(bond['referenceFields']['indexDurationYears'], '8.95')
        self.assertEqual(bond['disposition'], 'incomplete')
        self.assertEqual(bond['durationCalculation']['status'], 'unavailable')
        self.assertIsNone(bond['termYears'])
        self.assertTrue(any(item['field'] == 'outstandingBalanceYi' and item['isProxy'] for item in bond['substitutions']))
        self.assertEqual(provider.aggregate([bond]), [])

    def test_same_date_trade_eligible_and_decimal_weighted(self):
        first = provider.normalize_bond(detail(), TARGET, trade(), BENCHMARKS)
        second_detail = detail('809337')
        second_detail['issueAmnt'] = '11.8'
        second = provider.normalize_bond(second_detail, TARGET, trade('809337', value='2'), BENCHMARKS)
        self.assertEqual(first['disposition'], 'eligible')
        self.assertEqual(first['yieldDate'], TARGET)
        self.assertEqual(first['tradeNetPrice'], '101.25')
        self.assertIsNone(first['closeNetPrice'])
        self.assertEqual(first['durationCalculation']['status'], 'estimated')
        self.assertGreater(Decimal(first['duration']), 0)
        self.assertLess(Decimal(first['duration']), Decimal(first['remainingYears']))
        self.assertNotEqual(first['duration'], BENCHMARKS['indexDurationYears'])
        cells = provider.aggregate([first, second])
        self.assertEqual(len(cells), 2)
        self.assertEqual(cells[0]['yieldPct'], '1.78832000')
        self.assertEqual(cells[0]['sampleCount'], 2)
        self.assertEqual(cells[0]['estimatedDurationCount'], 2)
        self.assertEqual(cells[0]['groupingBasis'], 'modified_duration')

    def test_eleven_year_remaining_term_groups_by_individual_duration(self):
        values = detail(mrtyDate='2037-09-11', frstValueDate=TARGET, frstCpnDt='2027-09-11')
        bond = provider.normalize_bond(values, TARGET, trade(), BENCHMARKS)
        rounded_remaining = int(Decimal(bond['remainingYears']).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        self.assertEqual(rounded_remaining, 11, 'This bond was excluded by the former remaining-term rule')
        self.assertEqual(bond['disposition'], 'eligible')
        self.assertEqual(bond['termYears'], 10)
        self.assertEqual(bond['durationCalculation']['status'], 'estimated')
        self.assertGreater(Decimal(bond['duration']), Decimal('8.5'))
        self.assertLess(Decimal(bond['duration']), Decimal('12.5'))
        self.assertTrue(all(cell['termYears'] == 10 for cell in provider.aggregate([bond])))

    def test_missing_cashflow_input_keeps_actual_quote_but_prevents_aggregation(self):
        for missing in ('parCouponRate', 'couponType', 'couponFrqncy', 'frstValueDate', 'frstCpnDt', 'mrtyDate'):
            with self.subTest(missing=missing):
                values = detail()
                values.pop(missing)
                bond = provider.normalize_bond(values, TARGET, trade(), BENCHMARKS)
                self.assertEqual(bond['yieldPct'], '1.76')
                self.assertIsNone(bond['duration'])
                self.assertIsNone(bond['termYears'])
                self.assertEqual(bond['durationCalculation']['status'], 'unavailable')
                self.assertEqual(bond['disposition'], 'incomplete')
                self.assertIn('可计算的个券修正久期', bond['missingFields'])
                self.assertEqual(provider.aggregate([bond]), [])

    def test_duration_coverage_counts_distinguish_estimated_and_unavailable(self):
        estimated = provider.normalize_bond(detail(), TARGET, trade())
        unavailable = provider.normalize_bond(detail('809337'), TARGET, benchmarks=BENCHMARKS)
        counts = provider._dataset(TARGET, [estimated, unavailable])['counts']
        self.assertEqual((counts['durationEstimated'], counts['durationCalculated'], counts['durationUnavailable']),
                         (1, 0, 1))

    def test_stale_trade_and_wrong_identity_rejected_without_stale_fallback(self):
        for observation in (trade(target='2026-09-10'), trade(code='999999')):
            bond = provider.normalize_bond(detail(), TARGET, observation, BENCHMARKS)
            self.assertIsNone(bond['yieldPct'])
            self.assertIsNone(bond['yieldDate'])
            self.assertIsNone(bond['duration'])
            self.assertIsNone(bond['termYears'])
            self.assertEqual(bond['durationCalculation']['status'], 'unavailable')
            self.assertEqual(bond['referenceFields']['yieldReferenceKind'], 'index_reference')

    def test_matured_bond_has_no_market_or_risk_references(self):
        values = detail()
        values['mrtyDate'] = '2026-06-03'
        bond = provider.normalize_bond(values, TARGET, trade(), BENCHMARKS)
        self.assertEqual(bond['disposition'], 'excluded')
        self.assertEqual(bond['remainingYears'], '0')
        for key in ('yieldPct', 'duration', 'outstandingBalanceYi'):
            self.assertIsNone(bond[key])
        for key, value in bond['referenceFields'].items():
            if key != 'issueSizeReferenceYi':
                self.assertIsNone(value, key)
        self.assertEqual(bond['issueAmountYi'], '88.2')

    def test_zero_yield_is_real_and_curve_does_not_extrapolate(self):
        bond = provider.normalize_bond(detail(), TARGET, trade(value='0'), BENCHMARKS)
        self.assertEqual(bond['disposition'], 'eligible')
        self.assertEqual(bond['yieldPct'], '0')
        self.assertIsNone(provider._interpolate([['1', '1.2'], ['5', '1.5']], '0.5'))
        self.assertIsNone(provider._interpolate([['1', '1.2'], ['5', '1.5']], '10'))

    def test_exact_identity_and_local_type_required(self):
        for invalid in ({**detail(), 'bondType': '公司债'}, {**detail(), 'bondCode': 'invalid'}):
            with self.assertRaises(ValueError):
                provider.normalize_bond(invalid, TARGET, trade())

    def test_city_and_corps_issuer_identity_takes_precedence(self):
        self.assertEqual(provider._region('福建省厦门市人民政府'), 'xiamen')
        self.assertEqual(provider._region('新疆维吾尔自治区生产建设兵团'), 'xpcc')
        self.assertIsNone(provider._region('地方融资平台有限公司'))

    def test_latest_trade_timestamp_and_conflict_detection(self):
        older = trade(value='1.4')
        older['showDate'] = TARGET+' 15:20:00'
        entry = {'responses': [{'body': {'records': [older, trade(), trade(target='2026-09-10')]}}]}
        values, conflicts = provider._trades_from(entry, TARGET)
        self.assertEqual(values['809336.IB']['dmiLatestContraRate'], '1.76')
        self.assertEqual(conflicts, [])
        entry['responses'][0]['body']['records'].append(trade(value='1.77'))
        values, conflicts = provider._trades_from(entry, TARGET)
        self.assertEqual(values, {})
        self.assertEqual(conflicts, ['809336.IB'])


class AKShareStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name)/'test.sqlite3'
        self.db_patch = patch.object(store, 'DB', self.db_path)
        self.db_patch.start()
        provider.initialize(seed=False)

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_empty_read_does_not_create_wind_tables_or_fallback(self):
        self.assertEqual(provider.read_available(TARGET)['counts']['bonds'], 0)
        self.assertIsNone(provider.read_day(TARGET)['snapshot'])
        with closing(sqlite3.connect(self.db_path)) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {'akshare_datasets', 'akshare_details', 'akshare_queries'})

    def test_verified_import_is_dated_idempotent_and_proxy_safe(self):
        provider.initialize()
        full = provider.read_available(TARGET, True)
        self.assertEqual(full['counts']['bonds'], 5)
        self.assertEqual(full['counts']['referenceOnly'], 3)
        self.assertEqual(sum(bond['yieldPct'] is not None for bond in full['bonds']), 1)
        self.assertEqual(provider.available_dates(), [TARGET])
        self.assertEqual(provider.read_available('2026-09-10')['bonds'], [])
        self.assertTrue(all(not bond['fieldSources'] for bond in provider.read_available(TARGET)['bonds']))
        self.assertTrue(any(bond['fieldSources'] for bond in full['bonds']))
        provider.initialize()
        self.assertEqual(provider.read_available(TARGET, True), full)
        request_id = full['bonds'][0]['requestIds'][0]
        self.assertEqual(provider.query_detail(request_id)['origin'], 'verified_import')

    def test_failed_spot_refresh_preserves_last_good_sample(self):
        provider.initialize()
        before = provider.read_available(TARGET, True)
        with patch.object(provider.QueryRecorder, 'query', side_effect=TimeoutError('timeout')):
            with self.assertRaises(TimeoutError):
                provider.collect(dict(runId='failed', source='akshare', targetDate=TARGET))
        self.assertEqual(provider.read_available(TARGET, True), before)

    def test_saved_rule_upgrade_archives_once_and_rebuilds_from_local_same_day_evidence(self):
        from backend import akshare_read_model as readmodel

        static = detail(mrtyDate='2037-09-11', frstValueDate=TARGET, frstCpnDt='2027-09-11')
        no_market = detail('809337')
        source = dict(requestId='saved-static', function='bond_info_detail_cm',
                      runId='fixture-static', retrievedAt='2026-09-12T09:00:00+08:00')
        market_source = dict(requestId='saved-market', function='bond_spot_deal',
                             runId='fixture-market', retrievedAt='2026-09-12T09:00:00+08:00')
        provider._save_detail('809336.IB', static, source)
        provider._save_detail('809337.IB', no_market, source)
        observations = [trade(), trade('809337', target='2026-09-10')]
        entry = dict(market_source, status='succeeded', arguments={},
                     responses=[dict(status=200, body={'records': observations})])
        with store.connection() as db:
            db.execute('INSERT INTO akshare_queries VALUES (?,?,?,?,?,?)',
                       ('saved-market', 'fixture-market', TARGET, market_source['retrievedAt'],
                        'bond_spot_deal', provider._dump(entry)))
        formerly_excluded = provider.normalize_bond(static, TARGET, trade(), BENCHMARKS, source, market_source)
        formerly_excluded.update(duration=None, termYears=11, disposition='excluded',
                                  reason='剩余期限取整为 11 年，不在目标期限内',
                                  staticSyncStatus='completed', syncJobId='historical-full-sync')
        formerly_excluded.pop('durationCalculation')
        formerly_excluded['fieldSources'].pop('duration', None)
        reference_only = provider.normalize_bond(no_market, TARGET, benchmarks=BENCHMARKS, detail_evidence=source)
        old = provider._dataset(TARGET, [formerly_excluded, reference_only], BENCHMARKS)
        old.update(rulesVersion='rules-akshare-v1-dated-trade', mappingVersion='akshare-local-v1',
                   groupingBasis='remaining_term')
        old['rules'].update(version=old['rulesVersion'], mappingVersion=old['mappingVersion'])
        provider._save_dataset(old)
        before = provider.read_available(TARGET, True)
        queries_before = provider._read_rows('SELECT * FROM akshare_queries')
        details_before = provider._read_rows('SELECT * FROM akshare_details')
        with patch.object(provider.QueryRecorder, 'query', side_effect=AssertionError('Migration is local only')):
            result = provider.recalculate_saved(TARGET, archive=True)
        self.assertTrue(result['changed'])
        after = provider.read_available(TARGET, True)
        self.assertEqual(after['rulesVersion'], provider.RULES['version'])
        self.assertEqual(after['mappingVersion'], provider.RULES['mappingVersion'])
        self.assertEqual(after['groupingBasis'], 'modified_duration')
        self.assertEqual(after['counts']['eligible'], 1)
        self.assertEqual(after['counts']['durationEstimated'], 1)
        self.assertEqual(after['counts']['durationUnavailable'], 1)
        upgraded = readmodel.bond(TARGET, '809336.IB')
        self.assertEqual(upgraded['termYears'], 10)
        self.assertEqual(upgraded['durationCalculation']['status'], 'estimated')
        self.assertEqual(upgraded['durationCalculation']['inputs']['targetDate'], TARGET)
        self.assertEqual(set(upgraded['durationCalculation']['sourceRequestIds']), {'saved-static', 'saved-market'})
        self.assertTrue(upgraded['durationCalculation']['assumptions'])
        self.assertEqual({item['requestId'] for item in upgraded['fieldSources']['duration']},
                         {'saved-static', 'saved-market'})
        self.assertTrue(all(item['calculationStatus'] == 'estimated' and item['sourceDate'] == TARGET
                            for item in upgraded['fieldSources']['duration']))
        self.assertEqual(upgraded['staticSyncStatus'], 'completed')
        self.assertEqual(upgraded['syncJobId'], 'historical-full-sync')
        unavailable = readmodel.bond(TARGET, '809337.IB')
        self.assertIsNone(unavailable['yieldPct'])
        self.assertIsNone(unavailable['duration'])
        self.assertEqual(unavailable['referenceFields']['indexDurationYears'], BENCHMARKS['indexDurationYears'])
        self.assertEqual(readmodel.summary(TARGET)['counts'], after['counts'])
        archive = provider._read_rows('SELECT * FROM akshare_dataset_archive')
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive[0]['rules_version'], 'rules-akshare-v1-dated-trade')
        self.assertEqual(json.loads(archive[0]['payload']), before)
        self.assertFalse(provider.recalculate_saved(TARGET, archive=True)['changed'])
        self.assertEqual(provider.read_available(TARGET, True), after)
        self.assertEqual(provider._read_rows('SELECT * FROM akshare_dataset_archive'), archive)
        self.assertEqual(provider._read_rows('SELECT * FROM akshare_queries'), queries_before)
        self.assertEqual(provider._read_rows('SELECT * FROM akshare_details'), details_before)

    def test_live_historical_refresh_does_not_relabel_today_as_requested_day(self):
        provider.initialize()
        target = '2026-09-10'
        def fake_query(recorder, name, arguments, columns, compatibility=False):
            entry = dict(requestId=name, runId='history', function=name, retrievedAt='2026-09-12', responses=[])
            recorder.queries.append(entry)
            if name == 'bond_spot_deal':
                entry['responses'] = [{'body': {'records': [trade()]}}]
                return [], entry
            if name == 'bond_index_general_cbond':
                return [dict(date=target, value='1.7')], entry
            return [dict(日期=target, 期限=1, 到期收益率=1.2), dict(日期=target, 期限=30, 到期收益率=2.2)], entry
        with patch.object(provider.QueryRecorder, 'query', fake_query):
            result = provider.collect(dict(runId='history', source='akshare', targetDate=target))
        self.assertEqual(result['counts']['bonds'], 5)
        bonds = provider.read_available(target)['bonds']
        self.assertTrue(all(bond['yieldPct'] is None for bond in bonds))
        self.assertEqual(provider.read_available(TARGET)['provenance']['origin'], 'verified_import')

    def test_partial_benchmark_failure_retains_same_day_value_and_original_source(self):
        provider.initialize()
        old = provider.read_available(TARGET, True)
        def fake_query(recorder, name, arguments, columns, compatibility=False):
            if name != 'bond_spot_deal':
                raise TimeoutError('temporary benchmark failure')
            entry = dict(requestId='new-spot', runId='partial', function=name,
                         retrievedAt='2026-09-12', responses=[{'body': {'records': []}}])
            recorder.queries.append(entry)
            return [], entry
        with patch.object(provider.QueryRecorder, 'query', fake_query):
            provider.collect(dict(runId='partial', source='akshare', targetDate=TARGET))
        fresh = provider.read_available(TARGET, True)
        self.assertEqual(fresh['benchmarks'], old['benchmarks'])
        for code in ('809336.IB', '101900.IB', '2571241.IB'):
            before = next(bond for bond in old['bonds'] if bond['code'] == code)
            after = next(bond for bond in fresh['bonds'] if bond['code'] == code)
            self.assertEqual(after['referenceFields'], before['referenceFields'])
            for field in ('indexYieldPct', 'indexDurationYears', 'curveYieldPct'):
                before_source, after_source = before['fieldSources'][field][0], after['fieldSources'][field][0]
                for key in ('requestId', 'retrievedAt', 'sourceFunction', 'sourceDate'):
                    self.assertEqual(after_source[key], before_source[key])
                self.assertTrue(after_source['reusedSameDate'])
        self.assertEqual(len(fresh['provenance']['reusedBenchmarkFields']), 3)


if __name__ == '__main__':
    unittest.main()
