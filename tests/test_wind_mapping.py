import json
import unittest
from backend.wind_mapping import Merge,VERSION


TARGET='2026-09-10'
CODE='809336.IB'


def response(name='收盘价到期收益率',value=1.7691,unit='%',metadata=None):
    metadata={} if metadata is None else metadata
    columns=[{'name':'Wind代码'},{'name':name,'unit':unit}]+[{'name':key} for key in metadata]
    table={'columns':columns,'rows':[[CODE,value,*metadata.values()]]}
    return {'content':[{'type':'text','text':json.dumps({'data':{'data':[table]}})}]}


def normalize(**kwargs):
    merge=Merge(TARGET)
    merge.add(response(**kwargs),'yield-request')
    return merge.normalize(CODE)


class YieldMappingTests(unittest.TestCase):
    def test_explicit_closing_ytm_preserves_value_date_and_cell_evidence(self):
        row,sources=normalize(metadata={'收益率数据日期':20260910})
        self.assertEqual(row['yieldPct'],'1.7691')
        self.assertEqual(row['yieldDate'],TARGET)
        self.assertEqual(row['valuationDate'],TARGET)
        self.assertEqual((row['yieldMetric'],row['yieldPriceBasis']),('ytm','close'))
        self.assertEqual(row['valueStatus'],'valid')
        self.assertEqual(row['_descriptive']['reportedYieldDates'],[20260910])
        self.assertEqual(sources['reportedYieldDates'][0]['value'],20260910)
        self.assertEqual(sources['yieldDate'],[])
        self.assertEqual(sources['yieldPct'][0]['requestId'],'yield-request')
        self.assertEqual(row['_descriptive']['yieldSourceField'],'收盘价到期收益率')
        self.assertEqual(VERSION,'wind-fields-v7-closing-yield-alias')

    def test_issue_coupon_preserves_its_original_basis_and_never_becomes_yield(self):
        row,sources=normalize(name='票面利率_发行时',value=1.8)
        self.assertEqual(row['_descriptive']['couponPct'],'1.8')
        self.assertEqual(sources['couponPct'][0]['name'],'票面利率_发行时')
        self.assertIsNone(row['yieldPct'])

    def test_observed_yield_aliases_use_requested_closing_ytm_without_extra_proof(self):
        for name in ['到期收益率','收盘价收益率','收盘收益率']:
            with self.subTest(name=name):
                row,_=normalize(name=name)
                self.assertEqual(row['yieldPct'],'1.7691')
                self.assertEqual(row['valueStatus'],'valid')
                self.assertEqual(row['yieldDate'],TARGET)

    def test_parenthesized_closing_basis_stays_part_of_field_name(self):
        for name in ['到期收益率(收盘价)','到期收益率（收盘价）','（2026-09-10）收盘价到期收益率']:
            with self.subTest(name=name):
                row,_=normalize(name=name,metadata={'收益率数据日期':TARGET})
                self.assertEqual(row['valueStatus'],'valid')

    def test_missing_unit_does_not_discard_returned_yield_and_source_unit_is_kept(self):
        row,sources=normalize(name='收盘价收益率',unit=None)
        self.assertEqual(row['valueStatus'],'valid')
        self.assertIsNone(sources['yieldPct'][0]['unit'])
        self.assertEqual(row['_descriptive']['sourceUnits']['yieldPct'],[None])

    def test_chinabond_exercise_and_coupon_are_never_aliases(self):
        for name in ['中债估值收益率','中债估价收益率','到行权收益率','行权收益率','票面利率','成交到期收益率']:
            with self.subTest(name=name):
                row,_=normalize(name=name,metadata={'收益率数据日期':TARGET,'收益率价格口径':'收盘价','收益率类型':'到期收益率'})
                self.assertIsNone(row['yieldPct'])
                self.assertEqual(row['valueStatus'],'missing')

    def test_query_date_is_used_without_requiring_a_source_date(self):
        for metadata in [{},{'中债估值日期':TARGET}]:
            with self.subTest(metadata=metadata):
                row,_=normalize(name='2026年9月10日收盘价到期收益率',metadata=metadata)
                self.assertEqual(row['yieldPct'],'1.7691')
                self.assertEqual(row['yieldDate'],TARGET)
                self.assertEqual(row['valueStatus'],'valid')

    def test_source_date_is_retained_separately_from_query_date(self):
        row,_=normalize(name='2026年9月10日收盘价到期收益率',metadata={'收益率数据日期':'2026-09-09'})
        self.assertEqual(row['yieldDate'],TARGET)
        self.assertEqual(row['_descriptive']['reportedYieldDates'],['2026-09-09'])
        self.assertEqual(row['valueStatus'],'valid')

    def test_extra_metadata_request_is_not_needed_to_accept_numeric_value(self):
        for name,first,second in [
            ('收盘价到期收益率',{}, {'收益率数据日期':TARGET}),
            ('到期收益率',{'收益率数据日期':TARGET},{'收益率价格口径':'收盘价'}),
            ('收盘价收益率',{'收益率数据日期':TARGET},{'收益率类型':'到期收益率'}),
        ]:
            with self.subTest(name=name):
                merge=Merge(TARGET)
                merge.add(response(name=name,metadata=first),'yield-request')
                merge.add(response(name='证券简称',value='26河北23',metadata=second),'unrelated-request')
                row,_=merge.normalize(CODE)
                self.assertEqual(row['valueStatus'],'valid')

    def test_null_nonfinite_and_explicit_unrelated_metadata_are_rejected(self):
        cases=[{'value':None},{'value':'NaN'},{'value':True},
               {'metadata':{'收益率数据日期':TARGET,'收益率价格口径':'成交价'}},
               {'metadata':{'收益率数据日期':TARGET,'收益率类型':'到行权收益率'}}]
        for case in cases:
            with self.subTest(case=case):
                row,_=normalize(**dict({'metadata':{'收益率数据日期':TARGET}},**case))
                self.assertEqual(row['valueStatus'],'missing')

    def test_valid_zero_is_not_a_missing_placeholder(self):
        row,_=normalize(value=0,metadata={'收益率数据日期':TARGET})
        self.assertEqual(row['yieldPct'],'0')
        self.assertEqual(row['valueStatus'],'valid')

    def test_conflicting_nonempty_returns_do_not_silently_choose_one(self):
        merge=Merge(TARGET)
        merge.add(response(value=1.7,metadata={'收益率数据日期':TARGET}),'first')
        merge.add(response(value=1.8,metadata={'收益率数据日期':TARGET}),'second')
        row,_=merge.normalize(CODE)
        self.assertIsNone(row['yieldPct'])
        self.assertTrue(any('冲突' in error for error in row['_validationErrors']))

    def test_equivalent_decimal_precision_is_not_a_value_conflict(self):
        merge=Merge(TARGET)
        merge.add(response(value='1.7000',metadata={'收益率数据日期':TARGET}),'first')
        merge.add(response(value=1.7,metadata={'收益率数据日期':TARGET}),'second')
        row,_=merge.normalize(CODE)
        self.assertEqual(row['yieldPct'],'1.7000')
        self.assertEqual(row['valueStatus'],'valid')

    def test_recorded_single_bond_values_are_usable_without_clauses(self):
        # Bounded copy of the four saved MCP tables for 809336.IB / 2026-09-11:
        # runtime/single-bond-ytm-809336-20260911.json. No network or database.
        fixtures=[
            (['Wind代码','证券简称',('剩余期限_下一行权日','年'),('2026年9月11日的收盘价收益率','%'),
              '2026年9月11日的收盘价收益率.债券价格类型','交易币种',('2026年9月11日的债券余额','亿'),
              '交易币种_2','2026年9月11日基于净价的收盘价修正久期',('2026年9月11日的收盘价净价','元'),
              '2026年9月11日的收盘价净价.债券价格类型','交易币种_2'],
             [CODE,'26河北23',9.6082,1.7691,'收益率','CNY',88.2,'CNY',8.7035,101.0634,'净价','CNY']),
            (['Wind代码','证券简称',('发行总额','亿元'),'交易币种','WIND代码','主证券代码','跨市场代码',
              '债务主体名称','债务主体中文简称','所属概念板块','发行起始日期','到期日期',('2026年9月11日票面利率','%'),
              '特殊条款','发行人赎回权下一行权日','发行人赎回权下一行权日.选择权类型'],
             [CODE,'26河北23',88.2,'CNY',CODE,CODE,'236633.SH','河北省人民政府','',
              '利率债;地方政府再融资债;再融资一般债;地方政府一般债;借新还旧债券;全部公募债',
              '2026-04-20','2036-04-21',1.89,'','','发行人赎回权']),
            (['Wind代码','证券简称','2026年9月11日债务主体名称','债务主体中文简称',
              '2026年9月11日省级行政区划','2026年9月11日省级行政区划.行政区划级别'],
             [CODE,'26河北23','河北省人民政府','','河北省','省级']),
            (['Wind代码','证券简称','2026年9月11日中债估价修正久期',('2026年9月11日的实际剩余期限','年'),
              ('2026年9月11日中债4202曲线收益率','%')],[CODE,'26河北23',None,9.6082,None]),
        ]
        merge=Merge('2026-09-11')
        for index,(columns,values) in enumerate(fixtures):
            table={'columns':[{'name':c} if isinstance(c,str) else {'name':c[0],'unit':c[1]} for c in columns],'rows':[values]}
            merge.add({'content':[{'type':'text','text':json.dumps({'data':{'data':[table]}})}]},f'saved-{index}',[CODE])
        row,sources=merge.normalize(CODE)
        for field,value in {'yieldPct':'1.7691','yieldDate':'2026-09-11','remainingYears':'9.6082',
                            'issueAmountYi':'88.2','regionId':'hebei','bondType':'general','valueStatus':'valid'}.items():
            self.assertEqual(row[field],value)
        for field,value in {'duration':'8.7035','couponPct':'1.89','outstandingBalanceYi':'88.2','closeNetPrice':'101.0634'}.items():
            self.assertEqual(row['_descriptive'][field],value)
        self.assertEqual(row['_validationErrors'],[])
        self.assertNotIn('earlyRepayment',row)
        self.assertNotIn('redemption',row)
        self.assertNotIn('earlyRepayment',sources)
        self.assertNotIn('redemption',sources)
        self.assertEqual(row['_descriptive']['sourceUnits']['outstandingBalanceYi'],['亿'])
        self.assertEqual(sources['duration'][0]['name'],'2026年9月11日基于净价的收盘价修正久期')
        self.assertTrue(any(c['name']=='特殊条款' for c in merge.fields[CODE]))

    def test_numeric_precision_and_optional_unit_metadata_do_not_block_issue_amount(self):
        merge=Merge(TARGET)
        merge.add(response(name='发行总额',value='88.2000',unit='亿'),'one')
        merge.add(response(name='发行总额',value=88.2,unit=None),'two')
        row,sources=merge.normalize(CODE)
        self.assertEqual(row['issueAmountYi'],'88.2000')
        self.assertEqual(row['_validationErrors'],[])
        self.assertEqual([c['unit'] for c in sources['issueAmountYi']],['亿',None])

    def test_issue_amount_and_balance_normalize_explicit_units_before_conflict_check(self):
        for field,name,descriptive in [('issueAmountYi','发行总额',False),('outstandingBalanceYi','债券余额',True)]:
            with self.subTest(field=field):
                merge=Merge(TARGET)
                for index,(value,unit) in enumerate([(88.2,'亿元'),(882000,'万元'),(8820000000,'元')]):
                    merge.add(response(name=name,value=value,unit=unit),f'amount-{index}')
                row,sources=merge.normalize(CODE)
                value=(row['_descriptive'] if descriptive else row)[field]
                self.assertEqual(value,'88.2')
                self.assertEqual(row['_validationErrors'],[])
                self.assertEqual([cell['value'] for cell in sources[field]],[88.2,882000,8820000000])
                self.assertEqual(row['_descriptive']['sourceUnits'][field],['亿元','万元','元'])

    def test_basis_points_and_decimal_proportion_normalize_to_percent(self):
        merge=Merge(TARGET)
        for index,(value,unit) in enumerate([(1.7691,'%'),(176.91,'BP'),(0.017691,'比例')]):
            merge.add(response(value=value,unit=unit),f'yield-{index}')
        row,sources=merge.normalize(CODE)
        self.assertEqual(row['yieldPct'],'1.7691')
        self.assertEqual(row['valueStatus'],'valid')
        self.assertEqual([cell['unit'] for cell in sources['yieldPct']],['%','BP','比例'])

    def test_explicit_day_unit_is_not_guessed_as_years(self):
        merge=Merge(TARGET)
        merge.add(response(name='剩余期限',value=3500,unit='天'),'days')
        row,sources=merge.normalize(CODE)
        self.assertIsNone(row['remainingYears'])
        self.assertTrue(any('单位' in error for error in row['_validationErrors']))
        self.assertEqual(sources['remainingYears'][0]['value'],3500)

    def test_bond_type_aliases_compare_classification_and_preserve_original_tags(self):
        for label,tags,expected in [
            ('专项债-交通基础设施专项债','地方政府新增债;新增专项债;地方政府专项债;全部公募债','special'),
            ('一般债','地方政府置换债;地方政府一般债;全部公募债','general'),
        ]:
            with self.subTest(label=label):
                merge=Merge(TARGET)
                merge.add(response(name='2026年9月10日的地方债类型',value=label,unit=None,
                                   metadata={'2026年9月10日的所属概念板块':tags}),'basic')
                row,sources=merge.normalize(CODE)
                self.assertEqual(row['bondType'],expected)
                self.assertEqual(row['_validationErrors'],[])
                self.assertEqual([cell['value'] for cell in sources['bondType']],[label,tags])

    def test_bond_type_real_conflicts_remain_errors_and_unknown_tags_are_not_inferred(self):
        for first,second,conflicted in [
            ('一般债','地方政府专项债',True),
            ('地方政府一般债;地方政府专项债','全部公募债',True),
            ('专项债相关产品','企业债;非地方政府一般债',False),
        ]:
            with self.subTest(first=first):
                row,_=normalize(name='地方债类型',value=first,unit=None,
                                metadata={'所属概念板块':second})
                self.assertIsNone(row['bondType'])
                self.assertEqual('bondType 多次返回值冲突' in row['_validationErrors'],conflicted)

    def test_primary_identity_precedes_date_preference_and_dated_aliases_resolve_expected(self):
        for primary_name,cross_name in [
            ('主证券代码','2026年9月10日的跨市场代码'),
            ('2026年9月10日的主证券代码','2026年9月10日的跨市场代码'),
        ]:
            with self.subTest(primary_name=primary_name):
                table={'columns':[{'name':'2026年9月10日的Wind代码'},
                                  {'name':primary_name},{'name':cross_name}],
                       'rows':[['236633.SH',CODE,'236633.SH']]}
                raw={'content':[{'type':'text','text':json.dumps({'data':{'data':[table]}})}]}
                merge=Merge(TARGET)
                actual,_=merge.add(raw,'dated-primary',expected=[CODE],primary=True)
                self.assertEqual(actual,{CODE})
                self.assertEqual(merge.aliases['236633.SH'],CODE)
                row,sources=merge.normalize(CODE)
                self.assertEqual(row['bondId'],CODE)
                self.assertEqual(row['_validationErrors'],[])
                self.assertEqual(sources['bondId'][0]['name'],primary_name)
                secondary=Merge(TARGET)
                secondary.add(raw,'secondary',expected=[CODE])
                self.assertIn(CODE,secondary.fields)


if __name__=='__main__':unittest.main()
