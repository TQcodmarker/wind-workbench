"""Saved public responses supply dated trades without another market request."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import akshare_provider as provider, storage as store


TARGET = '2026-09-11'
CODE = '809336.IB'
OFFICIAL = 'https://www.chinamoney.com.cn/ags/ms/cm-u-md-bond/CbtPri'


def trade(value='1.76', observed=TARGET + ' 16:20:00', code='809336', price='101.25'):
    return dict(bondcode=code, showDate=observed,
                dmiLatestContraRate=value, dmiLatestRate=price)


def evidence(request_id='public-trade', retrieved=TARGET + 'T16:30:00+08:00'):
    return dict(requestId=request_id, runId='run-' + request_id,
                function='bond_spot_deal', retrievedAt=retrieved,
                executionMode='documented_sdk', dictionaryUrl=provider.DOC)


def old_bond(observed=TARGET + 'T16:20:00+08:00', value='1.76'):
    return dict(code=CODE, source='akshare', yieldPct=value, yieldDate=TARGET,
                yieldMetric='ytm', yieldPriceBasis='latest_trade', tradeNetPrice='101.25',
                tradeObservedAt=observed,
                fieldSources={'yieldPct': [dict(requestId='old-trade', sessionId='old-run',
                    source='akshare', sourceFunction='bond_spot_deal', sourceDate=TARGET,
                    retrievedAt=TARGET + 'T17:00:00+08:00', observationTime=observed,
                    executionMode='documented_sdk')]})


class CachedTradeObservationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(store, 'DB', Path(directory) / 'cached.sqlite3'))
        self.stack.enter_context(patch.object(store, 'MODE', 'wind'))
        self.no_network = self.stack.enter_context(patch(
            'requests.Session.send', side_effect=AssertionError('Cache reads must not use the network')))
        self.no_query = self.stack.enter_context(patch.object(
            provider.QueryRecorder, 'query', side_effect=AssertionError('Cache reads must not collect')))
        provider.initialize(seed=False)

    def save(self, request_id, rows, *, query_target=TARGET, retrieved=None,
             function='bond_spot_deal', stored_function=None, status='succeeded',
             url=OFFICIAL, response_status=200):
        entry = evidence(request_id, retrieved or TARGET + 'T16:30:00+08:00')
        entry.update(function=function, status=status, arguments={},
                     responses=[dict(url=url, status=response_status, body={'records': rows})])
        with store.connection() as db:
            db.execute('INSERT INTO akshare_queries VALUES (?,?,?,?,?,?)',
                       (request_id, entry['runId'], query_target, entry['retrievedAt'],
                        stored_function or function, provider._dump(entry)))
        return entry

    def test_empty_cache_is_read_only_and_has_no_fallback(self):
        with store.connection() as db:
            before = list(db.iterdump())
        self.assertEqual(provider.cached_trade_observations(TARGET), ({}, {}, set()))
        with store.connection() as db:
            self.assertEqual(list(db.iterdump()), before)
        self.no_network.assert_not_called()
        self.no_query.assert_not_called()

    def test_actual_observation_date_wins_over_requested_date_and_keeps_original_evidence(self):
        original = self.save('historical-request', [trade(value='0')], query_target='2026-09-10',
                             retrieved='2026-09-12T09:00:00+08:00')
        trades, sources, conflicts = provider.cached_trade_observations(TARGET)
        self.assertEqual(trades[CODE]['dmiLatestContraRate'], '0')
        self.assertEqual(conflicts, set())
        for key in ('requestId', 'runId', 'function', 'retrievedAt'):
            self.assertEqual(sources[CODE][key], original[key])
        self.assertTrue(sources[CODE]['reusedSameDate'])
        self.assertEqual(provider.cached_trade_observations('2026-09-10'), ({}, {}, set()))

    def test_only_successful_documented_official_response_is_accepted(self):
        rejected = [dict(status='failed'), dict(function='bond_index_general_cbond'),
                    dict(stored_function='wind_wss'),
                    dict(function='wind_wss', stored_function='bond_spot_deal'),
                    dict(url=OFFICIAL.replace('https:', 'http:')),
                    dict(url=OFFICIAL.replace('www.chinamoney.com.cn', 'example.com')),
                    dict(url=OFFICIAL.replace('/CbtPri', '/OtherEndpoint')),
                    dict(response_status=500)]
        for index, options in enumerate(rejected):
            with self.subTest(options=options):
                self.save('rejected-' + str(index), [trade()], **options)
                self.assertEqual(provider.cached_trade_observations(TARGET), ({}, {}, set()))
        self.save('accepted', [trade()])
        trades, sources, conflicts = provider.cached_trade_observations(TARGET)
        self.assertEqual(set(trades), {CODE})
        self.assertEqual(sources[CODE]['requestId'], 'accepted')
        self.assertEqual(conflicts, set())

    def test_wrong_date_invalid_time_and_non_trade_value_do_not_become_target_quotes(self):
        self.save('invalid-observations', [trade(observed='2026-09-10 16:20:00'),
                  trade(observed='2026-09-12 00:00:00'),
                  trade(observed=TARGET + ' garbage'), trade(value='--'),
                  dict(bondcode='809336', showDate=TARGET, value='1.9', indexYieldPct='1.9')])
        self.assertEqual(provider.cached_trade_observations(TARGET), ({}, {}, set()))

    def test_newest_market_time_wins_even_if_older_snapshot_was_retrieved_later(self):
        self.save('new-market-time', [trade('1.8', TARGET + ' 16:20:00')],
                  retrieved=TARGET + 'T16:30:00+08:00')
        self.save('late-fetch-of-old-trade', [trade('1.5', TARGET + ' 15:00:00')],
                  retrieved='2026-09-12T09:00:00+08:00')
        trades, sources, conflicts = provider.cached_trade_observations(TARGET)
        self.assertEqual(trades[CODE]['dmiLatestContraRate'], '1.8')
        self.assertEqual(sources[CODE]['requestId'], 'new-market-time')
        self.assertEqual(conflicts, set())

    def test_iso_timezone_uses_china_observation_day_and_chronological_order(self):
        self.save('local-midnight', [trade('1.5', TARGET + ' 00:04:00')])
        self.save('utc-midnight', [trade('1.8', '2026-09-10T16:05:00Z')])
        trades, sources, conflicts = provider.cached_trade_observations(TARGET)
        self.assertEqual(trades[CODE]['dmiLatestContraRate'], '1.8')
        self.assertEqual(sources[CODE]['requestId'], 'utc-midnight')
        self.assertEqual(conflicts, set())

    def test_same_time_conflict_is_isolated_and_later_real_observation_releases_it(self):
        self.save('first', [trade('1.7'), trade('1.6', code='809337')])
        self.save('contradicting', [trade('1.8', TARGET + 'T08:20:00Z')])
        trades, sources, conflicts = provider.cached_trade_observations(TARGET)
        self.assertNotIn(CODE, trades)
        self.assertNotIn(CODE, sources)
        self.assertEqual(conflicts, {CODE})
        self.assertIn('809337.IB', trades)
        self.save('reliable-later', [trade('1.9', TARGET + ' 16:21:00')])
        trades, sources, conflicts = provider.cached_trade_observations(TARGET)
        self.assertEqual(trades[CODE]['dmiLatestContraRate'], '1.9')
        self.assertEqual(sources[CODE]['requestId'], 'reliable-later')
        self.assertEqual(conflicts, set())

    def test_equal_numeric_values_with_different_precision_are_not_conflicts(self):
        self.save('precision-a', [trade('1.76', price='101.25')])
        self.save('precision-b', [trade('1.760', price='101.250')])
        trades, _, conflicts = provider.cached_trade_observations(TARGET)
        self.assertIn(CODE, trades)
        self.assertEqual(conflicts, set())


class SelectTradeObservationTests(unittest.TestCase):
    def test_normalized_bond_keeps_market_observation_time_and_original_fetch_time(self):
        detail = dict(bondCode='809336', bondType='地方政府债', bondName='26河北23',
                      bondFullName='2026年河北省政府一般债券', entyFullName='河北省政府',
                      issueDate='2026-04-20', mrtyDate='2036-04-21',
                      issueAmnt='88.2', parCouponRate='1.89', couponType='附息式固定利率',
                      couponFrqncy='年', frstValueDate='2026-04-21', frstCpnDt='2027-04-21')
        source = evidence('original-fetch', '2026-09-12T09:00:00+08:00')
        bond = provider.normalize_bond(detail, TARGET, trade('0', TARGET + 'T08:20:00Z'),
                                       trade_evidence=source)
        self.assertEqual(bond['yieldPct'], '0')
        self.assertEqual(bond['tradeObservedAt'], TARGET + 'T16:20:00+08:00')
        field_source = bond['fieldSources']['yieldPct'][0]
        self.assertEqual(field_source['observationTime'], bond['tradeObservedAt'])
        self.assertEqual(field_source['sourceDate'], TARGET)
        self.assertEqual(field_source['retrievedAt'], source['retrievedAt'])
        self.assertEqual(field_source['requestId'], source['requestId'])

    def test_newer_cached_market_time_replaces_old_quote_and_preserves_query_evidence(self):
        old, raw, source = old_bond(), trade('1.9', TARGET + ' 16:21:00'), evidence('new-trade')
        before = deepcopy((old, raw, source))
        selected, selected_source = provider.select_trade_observation(old, raw, source, TARGET)
        self.assertEqual(selected['dmiLatestContraRate'], '1.9')
        self.assertEqual(selected_source['requestId'], 'new-trade')
        self.assertEqual(selected_source['retrievedAt'], source['retrievedAt'])
        self.assertEqual((old, raw, source), before)

    def test_older_cached_market_time_cannot_replace_later_saved_observation(self):
        selected, source = provider.select_trade_observation(
            old_bond(), trade('1.4', TARGET + ' 15:20:00'), evidence('late-retrieval'), TARGET)
        self.assertEqual(selected['dmiLatestContraRate'], '1.76')
        self.assertEqual(source['requestId'], 'old-trade')
        self.assertEqual(source['retrievedAt'], TARGET + 'T17:00:00+08:00')

    def test_legacy_same_day_bond_without_market_time_can_receive_real_snapshot(self):
        old = old_bond()
        old.pop('tradeObservedAt')
        old['fieldSources']['yieldPct'][0].pop('observationTime')
        selected, source = provider.select_trade_observation(
            old, trade('0', TARGET + ' 16:00:00'), evidence('dated-snapshot'), TARGET)
        self.assertEqual(selected['dmiLatestContraRate'], '0')
        self.assertEqual(source['requestId'], 'dated-snapshot')

    def test_explicit_conflict_does_not_fall_back_to_previously_saved_value(self):
        selected, _ = provider.select_trade_observation(
            old_bond(), trade(), evidence(), TARGET, conflicted=True)
        self.assertIsNone(selected)

    def test_old_wrong_source_date_or_metric_does_not_supply_fallback(self):
        invalid = [dict(source='wind'), dict(yieldDate='2026-09-10'),
                   dict(yieldPriceBasis='valuation'), dict(yieldMetric='index_yield')]
        for changes in invalid:
            with self.subTest(changes=changes):
                old = {**old_bond(), **changes}
                selected, _ = provider.select_trade_observation(old, None, {}, TARGET)
                self.assertIsNone(selected)

    def test_wrong_date_or_identity_raw_observation_cannot_replace_current_bond(self):
        for raw in (trade('1.9', '2026-09-10 16:21:00'), trade('1.9', code='809337')):
            with self.subTest(raw=raw):
                selected, source = provider.select_trade_observation(old_bond(), raw, evidence(), TARGET)
                self.assertEqual(selected['dmiLatestContraRate'], '1.76')
                self.assertEqual(source['requestId'], 'old-trade')


if __name__ == '__main__':
    unittest.main()
