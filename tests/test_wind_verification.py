import unittest
from backend.wind_verification import inspect_valuation


TARGET='2026-09-10'


def table(name='收盘价到期收益率',value=1.7,actual=TARGET):
    return {'columns':[{'name':'Wind代码'},{'name':name,'unit':'%'},{'name':'收益率数据日期'}],
            'rows':[['809336.IB',value,actual]]}


class YieldVerificationTests(unittest.TestCase):
    def test_verified_sample_reports_returned_closing_ytm_and_current_policy(self):
        report=inspect_valuation([table()],TARGET)
        self.assertEqual((report['received'],report['nonNullYields'],report['verifiedDateYields']),(1,1,1))
        self.assertEqual(report['yieldDefinition']['metric'],'ytm')
        self.assertEqual(report['yieldDefinition']['priceBasis'],'close')
        self.assertEqual(report['dateBasis'],'query_date')
        self.assertEqual(report['dataAcceptance'],'mcp_returned_values')
        self.assertEqual(report['rulesVersion'],'rules-v3-mcp-values-no-clauses')

    def test_missing_stale_or_only_heading_date_does_not_block_query_date_grouping(self):
        for actual in [None,'2026-09-09']:
            with self.subTest(actual=actual):
                report=inspect_valuation([table(name='2026年9月10日收盘价到期收益率',actual=actual)],TARGET)
                self.assertEqual(report['nonNullYields'],1)
                self.assertEqual(report['verifiedDateYields'],1)

    def test_null_chinabond_or_coupon_yields_do_not_pass(self):
        for sample in [table(value=None),table(name='中债估值收益率'),table(name='票面利率')]:
            with self.subTest(sample=sample):
                report=inspect_valuation([sample],TARGET)
                self.assertEqual(report['nonNullYields'],0)
                self.assertEqual(report['verifiedDateYields'],0)

    def test_observed_yield_aliases_do_not_require_extra_metadata(self):
        for name in ['到期收益率','收盘价收益率']:
            with self.subTest(name=name):
                report=inspect_valuation([table(name=name,actual=None)],TARGET)
                self.assertEqual(report['nonNullYields'],1)
                self.assertEqual(report['verifiedDateYields'],1)

    def test_duplicate_conflicting_observations_are_not_first_row_wins(self):
        report=inspect_valuation([table(value=1.7),table(value=2.1)],TARGET)
        self.assertEqual(report['received'],1)
        self.assertEqual(report['verifiedDateYields'],0)
        self.assertEqual(report['rejectedYields'],1)

    def test_date_on_another_table_is_not_required_for_accepted_value(self):
        date_table={'columns':[{'name':'Wind代码'},{'name':'收益率数据日期'}],'rows':[['809336.IB',TARGET]]}
        report=inspect_valuation([table(actual=None),date_table],TARGET)
        self.assertEqual(report['verifiedDateYields'],1)


if __name__=='__main__':unittest.main()
