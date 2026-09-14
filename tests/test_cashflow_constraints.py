from copy import deepcopy
from pathlib import Path
import json
import unittest

from backend.bond_duration import calculate_duration
from backend.cashflow_constraints import apply_cashflow_constraints


def bond_detail(**overrides):
    result = dict(bondCode='2571241', couponType='附息式固定利率', parCouponRate='1.95',
                  couponFrqncy='半年', frstValueDate='2025-10-31', frstCpnDt='2026-04-30',
                  mrtyDate='2035-10-31', intrstBss='---', exerciseInfoFlag='否',
                  exerciseInfoList=[dict(exerciseType='---', exerciseDate='---')])
    result.update(overrides)
    return result


class CashflowConstraintsTests(unittest.TestCase):
    def test_known_jiangxi_counterexample_overrides_negative_provider_flag(self):
        original = bond_detail()
        applied = apply_cashflow_constraints('2571241.IB', original)
        self.assertTrue(applied['earlyRepayment'])
        self.assertEqual(applied['exerciseInfoFlag'], '否')
        self.assertNotIn('earlyRepayment', original)
        evidence = applied['cashflowConstraintEvidence']
        self.assertTrue(any('R225' in value and '债券提前偿还' in value for value in evidence))
        self.assertTrue(any('2026-06-17' in value and '原数据-WIND' in value
                            and '地方债二级盯盘.xlsx' in value for value in evidence))
        calculated = calculate_duration(applied, '2026-09-11', '1.8')
        self.assertEqual(calculated['status'], 'unavailable')
        self.assertIn('repaymentSchedule', calculated['missingFields'])
        self.assertEqual(calculated['inputs']['cashflowConstraintEvidence'], evidence)

    def test_all_eight_current_trade_intersections_have_exact_cell_evidence(self):
        samples = {'2605444.IB': 'R3131', '2605392.IB': 'R3169', '2505319.IB': 'R4406',
                   '2171290.IB': 'R9242', '2105223.IB': 'R10086', '199558.IB': 'R11058',
                   '199285.IB': 'R11275', '173788.IB': 'R12603'}
        for code, cell in samples.items():
            with self.subTest(code=code):
                applied = apply_cashflow_constraints(code, {'bondCode': code.removesuffix('.IB')})
                self.assertTrue(applied['earlyRepayment'])
                self.assertTrue(any(cell in text for text in applied['cashflowConstraintEvidence']))
                self.assertNotIn('redemption', applied)

    def test_workbook_zero_is_not_certification_of_bullet_cashflow(self):
        # R2=0 in the user's workbook. It must not synthesize a false clause flag.
        original = bond_detail(bondCode='809336')
        applied = apply_cashflow_constraints('809336.IB', original)
        self.assertEqual(applied, original)
        self.assertNotIn('earlyRepayment', applied)
        self.assertNotIn('redemption', applied)
        calculated = calculate_duration(applied, '2026-09-11', '1.8')
        self.assertEqual(calculated['status'], 'estimated')
        self.assertIn('repaymentSchedule', calculated['missingFields'])

    def test_known_and_unknown_results_are_independent_deep_copies(self):
        for code in ('2571241.IB', '999999999.IB'):
            with self.subTest(code=code):
                original = bond_detail(cashflowConstraintEvidence=['已有其他证据'])
                before = deepcopy(original)
                applied = apply_cashflow_constraints(code, original)
                applied['exerciseInfoList'][0]['exerciseType'] = 'changed'
                applied['cashflowConstraintEvidence'].append('new')
                self.assertEqual(original, before)
                self.assertIsNot(original, applied)

    def test_reapplying_constraint_does_not_duplicate_evidence(self):
        once = apply_cashflow_constraints('2571241.IB', bond_detail())
        twice = apply_cashflow_constraints('2571241.IB', once)
        self.assertEqual(once, twice)
        self.assertIsNot(once, twice)

    def test_verified_repayment_plan_can_resolve_known_amortization(self):
        original = bond_detail(intrstBss='ACT/ACT', yieldCompounding='coupon_frequency',
                               repaymentScheduleVerified=True,
                               repaymentScheduleSource='已核验募集文件：本金偿还计划页',
                               repaymentSchedule=[dict(date='2030-10-31', principalPct='50'),
                                                  dict(date='2035-10-31', principalPct='50')])
        applied = apply_cashflow_constraints('2571241.IB', original)
        calculated = calculate_duration(applied, '2026-09-11', '1.8')
        self.assertTrue(applied['earlyRepayment'])
        self.assertEqual(calculated['status'], 'calculated')
        self.assertEqual(calculated['inputs']['principalModel'], 'verified_schedule')
        self.assertTrue(calculated['inputs']['cashflowConstraintEvidence'])

    def test_redemption_is_still_rejected_with_a_verified_principal_schedule(self):
        original = bond_detail(bondCode='809064', intrstBss='ACT/ACT',
                               yieldCompounding='coupon_frequency', repaymentScheduleVerified=True,
                               repaymentScheduleSource='已核验募集文件：本金偿还计划页',
                               repaymentSchedule=[dict(date='2035-10-31', principalPct='100')])
        applied = apply_cashflow_constraints('809064.IB', original)
        self.assertTrue(applied['redemption'])
        self.assertTrue(any('R3045' in text and '赎回' in text
                            for text in applied['cashflowConstraintEvidence']))
        calculated = calculate_duration(applied, '2026-09-11', '1.8')
        self.assertEqual(calculated['status'], 'unavailable')
        self.assertIn('redemption', calculated['missingFields'])

    def test_unqualified_code_is_supported_but_other_market_is_not_conflated(self):
        self.assertTrue(apply_cashflow_constraints('2571241', {})['earlyRepayment'])
        self.assertTrue(apply_cashflow_constraints('2571241.ib', {})['earlyRepayment'])
        self.assertEqual(apply_cashflow_constraints('2571241.SH', {}), {})
        self.assertEqual(apply_cashflow_constraints(None, {}), {})

    def test_bundled_extract_only_contains_positive_flags_and_retains_provenance(self):
        data = json.loads((Path(__file__).resolve().parents[1] / 'backend' / 'data' /
                           'cashflow_constraints.json').read_text(encoding='utf-8'))
        self.assertEqual(data['count'], 1594)
        self.assertEqual(len(data['constraints']), 1594)
        self.assertEqual(data['evaluationDate'], '2026-06-17')
        self.assertEqual(data['sheet'], '原数据-WIND')
        self.assertTrue(data['sourceOriginalPath'].endswith('地方债二级盯盘.xlsx'))
        self.assertEqual(len(data['sourceExtractSha256']), 64)
        kinds = []
        for entry in data['constraints']:
            for marker in entry['markers']:
                self.assertIn(marker['rawValue'], ('债券提前偿还', '赎回'))
                self.assertRegex(marker['cell'], r'^[OR]\d+$')
                kinds.append(marker['kind'])
        self.assertEqual(kinds.count('early_repayment'), 1547)
        self.assertEqual(kinds.count('redemption'), 47)


if __name__ == '__main__':
    unittest.main()
