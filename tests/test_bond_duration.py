from datetime import date
from decimal import Decimal, localcontext
import json
import unittest

from backend.bond_duration import calculate_duration, duration_bucket, METHOD_VERSION


def detail(**overrides):
    result = dict(parCouponRate='5', couponType='附息式固定利率', couponFrqncy='年',
                  frstValueDate='2024-01-01', frstCpnDt='2025-01-01',
                  mrtyDate='2026-01-01', intrstBss='---', exerciseInfoFlag='否',
                  exerciseInfoList=[dict(exerciseType='---', exerciseDate='---')])
    result.update(overrides)
    return result


class DurationCalculationTests(unittest.TestCase):
    def assertDecimalClose(self, actual, expected, tolerance='0.00000001'):
        self.assertLessEqual(abs(Decimal(actual) - Decimal(expected)), Decimal(tolerance))

    def test_two_year_par_bond_known_analytic_duration(self):
        value = calculate_duration(detail(), '2024-01-01', '5')
        # Price = 5/1.05 + 105/1.05**2 = 100.
        macaulay = (Decimal(5) / Decimal('1.05') + 2 * Decimal(105) / Decimal('1.05') ** 2) / 100
        self.assertEqual(value['status'], 'estimated')
        self.assertDecimalClose(value['macaulayYears'], macaulay)
        self.assertDecimalClose(value['modifiedYears'], macaulay / Decimal('1.05'))
        self.assertEqual(value['bucketYears'], 3)
        self.assertDecimalClose(value['inputs']['fullPricePer100'], '100')
        self.assertEqual(value['methodVersion'], METHOD_VERSION)
        self.assertIn('repaymentSchedule', value['missingFields'])
        self.assertIn('intrstBss', value['missingFields'])
        self.assertTrue(any('未核验' in item and '本金' in item for item in value['assumptions']))
        self.assertIn('非官方', value['reason'])
        json.dumps(value, allow_nan=False)

    def test_modified_duration_matches_independent_finite_difference(self):
        values = detail(couponFrqncy='半年', parCouponRate='3.2', frstValueDate='2024-01-31',
                        frstCpnDt='2024-07-31', mrtyDate='2027-01-31')
        target, yield_pct = '2024-04-15', Decimal('2.7')
        calculated = calculate_duration(values, target, yield_pct)
        # Independent pricing formula over six semiannual payments, first fractional.
        fraction = Decimal((date(2024, 7, 31) - date(2024, 4, 15)).days) / Decimal(182)
        def price(annual_yield):
            base = 1 + annual_yield / 2
            return sum((Decimal('1.6') + (100 if period == 5 else 0)) /
                       base ** (fraction + period) for period in range(6))
        with localcontext() as context:
            context.prec = 40
            y, epsilon = yield_pct / 100, Decimal('0.000001')
            difference = -(price(y + epsilon) - price(y - epsilon)) / (2 * epsilon * price(y))
        self.assertEqual(calculated['status'], 'estimated')
        self.assertDecimalClose(calculated['modifiedYears'], difference, '0.00000002')

    def test_semiannual_coupon_and_zero_yield(self):
        values = detail(couponFrqncy='半年', frstCpnDt='2024-07-01', parCouponRate='4')
        calculated = calculate_duration(values, '2024-01-01', '0')
        # 2 at 0.5, 1 and 1.5 years; 102 at 2 years; price 108.
        expected = Decimal(210) / 108
        self.assertDecimalClose(calculated['macaulayYears'], expected)
        self.assertEqual(calculated['macaulayYears'], calculated['modifiedYears'])

    def test_zero_coupon_rate_at_positive_and_zero_yields(self):
        values = detail(parCouponRate='0')
        for y, expected in [('0', Decimal(2)), ('5', Decimal(2) / Decimal('1.05'))]:
            with self.subTest(yield_pct=y):
                calculated = calculate_duration(values, '2024-01-01', y)
                self.assertDecimalClose(calculated['macaulayYears'], '2')
                self.assertDecimalClose(calculated['modifiedYears'], expected)

    def test_month_end_semiannual_schedule_and_coupon_day(self):
        values = detail(couponFrqncy='半年', frstValueDate='2025-10-31',
                        frstCpnDt='2026-04-30', mrtyDate='2027-10-31', parCouponRate='0')
        calculated = calculate_duration(values, '2026-04-30', '0')
        self.assertEqual(calculated['status'], 'estimated')
        self.assertEqual(calculated['inputs']['nextCouponDate'], '2026-10-31')
        self.assertDecimalClose(calculated['modifiedYears'], '1.5')
        self.assertEqual(calculated['inputs']['futureCouponCount'], 3)

    def test_leap_year_annual_schedule_uses_actual_coupon_period(self):
        values = detail(frstValueDate='2024-02-29', frstCpnDt='2025-02-28',
                        mrtyDate='2028-02-29', parCouponRate='0')
        calculated = calculate_duration(values, '2027-03-01', '0')
        expected = Decimal(365) / 366
        self.assertEqual(calculated['status'], 'estimated')
        self.assertDecimalClose(calculated['modifiedYears'], expected)

    def test_fixed_day_is_not_silently_changed_to_month_end(self):
        values = detail(couponFrqncy='半年', frstValueDate='2024-10-30',
                        frstCpnDt='2025-04-30', mrtyDate='2026-04-30')
        calculated = calculate_duration(values, '2025-04-30', '5')
        self.assertEqual(calculated['inputs']['nextCouponDate'], '2025-10-30')

    def test_verified_amortization_shortens_duration_and_retains_evidence(self):
        values = detail(intrstBss='ACT/ACT', yieldCompounding='coupon_frequency',
                        earlyRepayment=True, repaymentScheduleVerified=True,
                        repaymentScheduleSource='prospectus.pdf page 18',
                        repaymentSchedule=[dict(date='2025-01-01', principalPct='50'),
                                           dict(date='2026-01-01', principalPct='50')])
        calculated = calculate_duration(values, '2024-01-01', '5')
        # 55 paid after one year; 52.5 after two. At a 5% par yield price is 100.
        expected = (Decimal(55) / Decimal('1.05') + Decimal(105) / Decimal('1.05') ** 2) / 105
        self.assertEqual(calculated['status'], 'calculated')
        self.assertDecimalClose(calculated['modifiedYears'], expected)
        self.assertLess(Decimal(calculated['modifiedYears']),
                        Decimal(calculate_duration(detail(), '2024-01-01', '5')['modifiedYears']))
        self.assertEqual(calculated['inputs']['principalModel'], 'verified_schedule')
        self.assertEqual(calculated['missingFields'], [])
        self.assertIn('自行计算', calculated['reason'])
        # After the first repayment coupons accrue only on the remaining 50.
        after_payment = calculate_duration(values, '2025-01-01', '5')
        self.assertDecimalClose(after_payment['inputs']['fullPricePer100'], '50')
        self.assertDecimalClose(after_payment['modifiedYears'], Decimal(1) / Decimal('1.05'))

    def test_complete_missing_field_list_includes_target_yield(self):
        calculated = calculate_duration({}, None, None)
        self.assertEqual(calculated['status'], 'unavailable')
        self.assertEqual(set(calculated['missingFields']), {'parCouponRate', 'couponType', 'couponFrqncy',
                         'frstValueDate', 'frstCpnDt', 'mrtyDate', 'targetDate', 'yieldPct'})

    def test_bad_numbers_are_unavailable_and_never_produce_nan(self):
        for value in ('NaN', 'sNaN', 'Infinity', '-Infinity', '--', '', True, 'abc', '1,2',
                      float('nan'), float('inf'), Decimal('NaN')):
            for field in ('parCouponRate', 'yieldPct'):
                with self.subTest(value=value, field=field):
                    values = detail(**{field: value}) if field == 'parCouponRate' else detail()
                    calculated = calculate_duration(values, '2024-01-01', value if field == 'yieldPct' else '5')
                    self.assertEqual(calculated['status'], 'unavailable')
                    self.assertIsNone(calculated['modifiedYears'])
                    self.assertIn(field, calculated['missingFields'])
                    json.dumps(calculated, allow_nan=False)

    def test_numerical_failure_cannot_leave_a_usable_partial_result(self):
        calculated = calculate_duration(detail(), '2024-01-01', '-99.99999999999999999999999')
        self.assertEqual(calculated['status'], 'unavailable')
        self.assertIsNone(calculated['modifiedYears'])
        self.assertIsNone(calculated['bucketYears'])

    def test_decimal_and_date_input_evidence_is_json_safe(self):
        values = detail(parCouponRate=Decimal('5'), frstValueDate=date(2024, 1, 1))
        calculated = calculate_duration(values, date(2024, 1, 1), Decimal('5'))
        self.assertEqual(calculated['status'], 'estimated')
        json.dumps(calculated, allow_nan=False)

    def test_yield_domain_supports_valid_negative_yield(self):
        calculated = calculate_duration(detail(parCouponRate='0'), '2024-01-01', '-1')
        self.assertDecimalClose(calculated['modifiedYears'], Decimal(2) / Decimal('.99'))
        for frequency, y in [('年', '-100'), ('年', '-101'), ('半年', '-200')]:
            values = detail(couponFrqncy=frequency, frstCpnDt='2024-07-01' if frequency == '半年' else '2025-01-01')
            self.assertEqual(calculate_duration(values, '2024-01-01', y)['status'], 'unavailable')

    def test_invalid_dates_and_irregular_coupons_are_rejected(self):
        for patch in ({'frstValueDate': '2024-02-30'}, {'mrtyDate': '2023-01-01'},
                      {'frstCpnDt': '2024-01-01'}, {'frstCpnDt': '2025-02-01'},
                      {'mrtyDate': '2026-02-01'}, {'frstValueDate': '2024-1-1'},
                      {'frstCpnDt': '2025-01-01T00:00:00'}):
            with self.subTest(patch=patch):
                self.assertEqual(calculate_duration(detail(**patch), '2024-01-01', '5')['status'], 'unavailable')
        for target in ('2023-12-31', '2026-01-01', '2026-01-02', 'invalid', '2024-01-01 00:00:00'):
            with self.subTest(target=target):
                self.assertEqual(calculate_duration(detail(), target, '5')['status'], 'unavailable')

    def test_complex_terms_unknown_amortization_and_other_conventions_rejected(self):
        for patch in ({'couponType': '浮动利率'}, {'couponFrqncy': '季'}, {'intrstBss': 'ACT/365'},
                      {'parCouponRate': '-1'}, {'earlyRepayment': True}, {'exerciseInfoFlag': '是'},
                      {'redemption': True}, {'note': '第三年起分期偿还本金'},
                      {'exerciseInfoList': [dict(exerciseType='赎回', exerciseDate='2025-01-01')]},
                      {'yieldCompounding': 'continuous'}):
            with self.subTest(patch=patch):
                self.assertEqual(calculate_duration(detail(**patch), '2024-01-01', '5')['status'], 'unavailable')

    def test_unverified_or_invalid_principal_schedules_are_rejected(self):
        valid = dict(repaymentSchedule=[dict(date='2026-01-01', principalPct='100')],
                     repaymentScheduleVerified=True, repaymentScheduleSource='document page 1')
        for patch in ({'repaymentScheduleVerified': False}, {'repaymentScheduleSource': ''},
                      {'repaymentSchedule': []}, {'repaymentSchedule': [dict(date='2025-01-01', principalPct='100')]},
                      {'repaymentSchedule': [dict(date='2026-01-01', principalPct='99')]},
                      {'repaymentSchedule': [dict(date='2025-06-01', principalPct='100')]},
                      {'repaymentSchedule': [dict(date='2026-01-01', principalPct='NaN')]},
                      {'repaymentSchedule': [dict(date='2025-01-01', principalPct='50'),
                                             dict(date='2025-01-01', principalPct='50')]}):
            with self.subTest(patch=patch):
                self.assertEqual(calculate_duration(detail(**(valid | patch)), '2024-01-01', '5')['status'], 'unavailable')


class DurationBucketTests(unittest.TestCase):
    def test_nearest_standard_bucket_and_high_midpoint_ties(self):
        for value, expected in [('0.01', 3), ('3', 3), ('3.999999999', 3), ('4', 5),
                                ('6', 7), ('8.5', 10), ('11', 10), ('12.49999999', 10),
                                ('12.5', 15), ('17.5', 20), ('25', 30), ('30', 30), ('70', 30)]:
            with self.subTest(value=value):
                self.assertEqual(duration_bucket(value), expected)

    def test_invalid_bucket_values_raise_value_error(self):
        for value in ('0', '-1', 'NaN', 'Infinity', None, True, '--'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    duration_bucket(value)


if __name__ == '__main__':
    unittest.main()
