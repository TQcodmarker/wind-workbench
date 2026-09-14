from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
import unittest

from backend.bond_duration import calculate_duration
from backend.selection_policy import select_duration, select_yield


CODE = '2600001.IB'
TARGET = '2026-09-11'


def observation(metric, value, **changes):
    row = dict(code=CODE, date=TARGET, metric=metric, value=value,
               unit='年' if metric == 'modified_duration' else '%',
               entityGrain='bond', source='individual-source',
               fieldSources=[dict(sourceFunction='individual_endpoint', requestId='request-1')])
    row.update(changes)
    return row


def calculation(**changes):
    result = dict(status='estimated', modifiedYears='11.25', bucketYears=10,
                  inputs=dict(targetDate=TARGET), reason='按固定利率现金流估算，非官方久期',
                  fieldSources=[dict(sourceFunction='bond_info_detail_cm', requestId='static-1')])
    result.update(changes)
    return result


def index(**changes):
    result = dict(value='8.9583', date=TARGET, source='中债-地方政府债指数',
                  metric='index_duration', entityGrain='index',
                  fieldSources=[dict(sourceFunction='bond_index_general_cbond', requestId='index-1')])
    result.update(changes)
    return result


class YieldSelectionTests(unittest.TestCase):
    def test_individual_valuation_precedes_trade_and_retains_both_values(self):
        selected = select_yield(CODE, TARGET, [observation('chinabond_valuation', '2.14')],
                                observation('ytm', '2.18'))
        self.assertEqual(selected['value'], '2.14')
        self.assertEqual(selected['valuationValue'], '2.14')
        self.assertEqual(selected['tradeValue'], '2.18')
        self.assertEqual(selected['kind'], 'chinabond_valuation')
        self.assertEqual(selected['yieldMetric'], 'chinabond_valuation')
        self.assertEqual(selected['yieldPriceBasis'], 'valuation')
        self.assertEqual(selected['sourceDate'], TARGET)
        self.assertEqual(selected['fieldSources'][0]['requestId'], 'request-1')

    def test_zero_and_negative_valuation_are_valid(self):
        for value in ['0', '-0.1']:
            with self.subTest(value=value):
                result = select_yield(CODE, TARGET, [observation('chinabond_valuation', value)],
                                      observation('ytm', '3'))
                self.assertEqual(result['value'], value)
                self.assertEqual(result['kind'], 'chinabond_valuation')

    def test_ytm_fallback_preserves_candidate_price_basis(self):
        result = select_yield(CODE, TARGET, [], observation('ytm', '2.2', priceBasis='closing_price'))
        self.assertEqual(result['yieldPriceBasis'], 'closing_price')
        self.assertEqual(result['yieldMetric'], 'ytm')
        self.assertEqual(result['valuationValue'], None)
        self.assertEqual(select_yield(CODE, TARGET, [], observation('ytm', '0'))['yieldPriceBasis'],
                         'latest_trade')

    def test_wrong_bond_date_units_and_nonfinite_values_do_not_hide_valid_fallback(self):
        overrides = [dict(code='2600002.IB'), dict(date='2026-09-10'), dict(unit='小数'),
                     dict(value='NaN'), dict(value=float('inf')), dict(value=True),
                     dict(value='--'), dict(date=TARGET + 'T00:00:00')]
        for invalid in overrides:
            with self.subTest(invalid=invalid):
                row = observation('chinabond_valuation', '2')
                row.update(invalid)
                result = select_yield(CODE, TARGET, [row], observation('ytm', '2.3'))
                self.assertEqual(result['kind'], 'ytm')
                self.assertEqual(result['value'], '2.3')

    def test_same_metric_conflict_rejects_valuation_and_falls_back(self):
        rows = [observation('chinabond_valuation', '2.2'), observation('chinabond_valuation', '2.3')]
        for ordered in [rows, list(reversed(rows))]:
            result = select_yield(CODE, TARGET, ordered, observation('ytm', '2.4'))
            self.assertEqual(result['kind'], 'ytm')
            self.assertEqual(result['valuationValue'], None)
            self.assertIn('冲突', result['reason'])

    def test_ytm_conflict_without_valuation_is_unavailable(self):
        result = select_yield(CODE, TARGET, [observation('ytm', '2.2')], observation('ytm', '2.3'))
        self.assertEqual(result['kind'], 'unavailable')
        self.assertIsNone(result['value'])

    def test_latest_trade_precedes_different_close_yield_without_false_conflict(self):
        close = observation('ytm', '2.2', priceBasis='close')
        trade = observation('ytm', '2.3', priceBasis='latest_trade')
        for rows, candidate in [([close], trade), ([trade, close], None), ([close, trade], None)]:
            with self.subTest(rows=rows):
                result = select_yield(CODE, TARGET, rows, candidate)
                self.assertEqual(result['kind'], 'ytm')
                self.assertEqual(result['value'], '2.3')
                self.assertEqual(result['yieldPriceBasis'], 'latest_trade')

    def test_latest_trade_conflict_may_fall_back_to_close_basis(self):
        rows = [observation('ytm', '2.2', priceBasis='latest_trade'),
                observation('ytm', '2.1', priceBasis='close')]
        result = select_yield(CODE, TARGET, rows, observation('ytm', '2.3', priceBasis='latest_trade'))
        self.assertEqual(result['value'], '2.1')
        self.assertEqual(result['yieldPriceBasis'], 'close')
        self.assertIn('最新成交到期收益率存在冲突', result['reason'])

    def test_unknown_price_basis_is_not_silently_treated_as_trade_or_close(self):
        result = select_yield(CODE, TARGET, [observation('ytm', '2.2', priceBasis='unrecognized')])
        self.assertEqual(result['kind'], 'unavailable')

    def test_equivalent_numeric_values_merge_provenance_without_mutating_inputs(self):
        rows = [observation('chinabond_valuation', '2.20'),
                observation('chinabond_valuation', Decimal('2.2'), source='second-source',
                            fieldSources=[dict(sourceFunction='second_endpoint', requestId='request-2')])]
        original = deepcopy(rows)
        selected = select_yield(CODE, TARGET, rows)
        self.assertEqual(Decimal(selected['value']), Decimal('2.2'))
        self.assertEqual(len(selected['fieldSources']), 2)
        selected['fieldSources'][0]['requestId'] = 'changed'
        self.assertEqual(rows, original)

    def test_index_curve_and_explicit_proxy_yields_are_rejected(self):
        changes = [dict(isProxy=True), dict(entityGrain='index'), dict(entityGrain='curve'),
                   dict(evidenceQuality='proxy'), dict(sourceFunction='bond_china_yield'),
                   dict(sourceFunction='bond_china_close_return'),
                   dict(fieldSources=[dict(sourceFunction='bond_index_general_cbond')]),
                   dict(fieldSources=[dict(isProxy=True)])]
        for invalid in changes:
            with self.subTest(invalid=invalid):
                for metric in ['chinabond_valuation', 'ytm']:
                    selected = select_yield(CODE, TARGET, [observation(metric, '2.2', **invalid)])
                    self.assertEqual(selected['kind'], 'unavailable')
                    self.assertIsNone(selected['value'])

    def test_invalid_target_and_unrelated_metrics_are_unavailable(self):
        self.assertEqual(select_yield(CODE, '2026-02-30', [], None)['kind'], 'unavailable')
        self.assertEqual(select_yield(None, TARGET, [observation('ytm', '2', code=None)])['kind'], 'unavailable')
        self.assertEqual(select_yield(CODE, TARGET, [index(), observation('modified_duration', '8')])['kind'],
                         'unavailable')


class DurationSelectionTests(unittest.TestCase):
    def test_direct_duration_wins_and_buckets_nearest_with_midpoint_up(self):
        for value, bucket in [('11', 10), ('12.5', 15), ('0.1', 3), ('50', 30)]:
            with self.subTest(value=value):
                selected = select_duration(CODE, TARGET, [observation('modified_duration', value)],
                                           calculation(), index())
                self.assertEqual(selected['kind'], 'direct')
                self.assertEqual(selected['bucketYears'], bucket)
                self.assertEqual(selected['individualValue'], value)

    def test_usable_individual_calculation_wins_over_index_and_keeps_pre_rounding_bucket(self):
        for status in ['estimated', 'calculated']:
            selected = select_duration(CODE, TARGET, [],
                                       calculation(status=status, modifiedYears='12.50000000', bucketYears=10),
                                       index())
            self.assertEqual(selected['kind'], status)
            self.assertEqual(selected['bucketYears'], 10)
            self.assertEqual(selected['individualValue'], '12.50000000')
            self.assertEqual(selected['fieldSources'][0]['requestId'], 'static-1')

    def test_actual_calculator_result_is_accepted_without_fabricated_observation(self):
        detail = dict(parCouponRate='3', couponType='附息式固定利率', couponFrqncy='年',
                      frstValueDate='2024-01-01', frstCpnDt='2025-01-01', mrtyDate='2036-01-01')
        calculated = calculate_duration(detail, TARGET, '2.2')
        selected = select_duration(CODE, TARGET, [], calculated, index())
        self.assertEqual(selected['kind'], 'estimated')
        self.assertEqual(selected['value'], calculated['modifiedYears'])
        self.assertEqual(selected['bucketYears'], calculated['bucketYears'])

    def test_direct_invalid_values_dates_codes_and_units_fall_back_to_calculation(self):
        overrides = [dict(value='-1'), dict(value='0'), dict(value='NaN'), dict(value=float('inf')),
                     dict(value=False), dict(code='another.IB'), dict(date='2026-09-10'), dict(unit='%')]
        for invalid in overrides:
            with self.subTest(invalid=invalid):
                row = observation('modified_duration', '8')
                row.update(invalid)
                self.assertEqual(select_duration(CODE, TARGET, [row], calculation(), index())['kind'], 'estimated')

    def test_proxy_cannot_become_individual_direct_duration(self):
        rows = [observation('modified_duration', '9', entityGrain='index'),
                observation('modified_duration', '9', isProxy=True),
                observation('modified_duration', '9', fieldSources=index()['fieldSources'])]
        for row in rows:
            with self.subTest(row=row):
                selected = select_duration(CODE, TARGET, [row], None, index())
                self.assertEqual(selected['kind'], 'index_reference')
                self.assertIsNone(selected['individualValue'])

    def test_direct_conflict_falls_back_to_individual_calculation(self):
        selected = select_duration(CODE, TARGET,
                                   [observation('modified_duration', '8'), observation('modified_duration', '9')],
                                   calculation(), index())
        self.assertEqual(selected['kind'], 'estimated')
        self.assertIn('冲突', selected['reason'])

    def test_missing_principal_schedule_may_use_authorized_index_reference(self):
        failed = dict(status='unavailable', inputs=dict(targetDate=TARGET),
                      reason='已知存在提前或分期还本，但缺少可靠还本计划')
        selected = select_duration(CODE, TARGET, [], failed, index())
        self.assertEqual(selected['kind'], 'index_reference')
        self.assertEqual(selected['value'], '8.9583')
        self.assertEqual(selected['bucketYears'], 10)
        self.assertIsNone(selected['individualValue'])
        self.assertIn('还本计划', selected['reason'])
        self.assertIn('不代表个券实际', selected['reason'])
        self.assertEqual(selected['fieldSources'][0]['requestId'], 'index-1')

    def test_wrong_date_code_or_invalid_calculation_falls_back_to_index(self):
        changes = [dict(inputs=dict(targetDate='2026-09-10')),
                   dict(inputs=dict(targetDate=TARGET, code='another.IB')),
                   dict(inputs={}), dict(modifiedYears='NaN'), dict(modifiedYears='0'), dict(status='unavailable')]
        for invalid in changes:
            with self.subTest(invalid=invalid):
                self.assertEqual(select_duration(CODE, TARGET, [], calculation(**invalid), index())['kind'],
                                 'index_reference')

    def test_index_requires_same_date_positive_duration_and_endpoint_evidence(self):
        changes = [dict(date='2026-09-10'), dict(value='0'), dict(value='-1'), dict(value='Infinity'),
                   dict(value=True), dict(entityGrain='bond'), dict(metric='index_yield'),
                   dict(fieldSources=[]), dict(fieldSources=[dict(sourceFunction='unrelated_endpoint')])]
        for invalid in changes:
            with self.subTest(invalid=invalid):
                selected = select_duration(CODE, TARGET, [], None, index(**invalid))
                self.assertEqual(selected['kind'], 'unavailable')
                self.assertIsNone(selected['bucketYears'])

    def test_index_output_copies_evidence_and_is_json_serializable(self):
        candidate = index()
        saved = deepcopy(candidate)
        selected = select_duration(CODE, date(2026, 9, 11), [], None, candidate)
        json.dumps(selected, allow_nan=False)
        selected['fieldSources'][0]['requestId'] = 'changed'
        self.assertEqual(candidate, saved)


if __name__ == '__main__':
    unittest.main()
