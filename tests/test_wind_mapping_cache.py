import unittest
from unittest.mock import patch

from backend import wind_mapping as mapping


class FieldNameCacheTests(unittest.TestCase):
    def setUp(self):
        mapping.base_name.cache_clear()

    def tearDown(self):
        mapping.base_name.cache_clear()

    def test_shared_field_names_are_computed_once_per_query_date(self):
        names=['Wind代码','主证券代码','2026年9月11日的收盘价收益率',
               '2026年9月11日的票面利率_发行时','基于净价的收盘价修正久期']
        for _ in range(500):
            for name in names:
                normalized=mapping.base_name(name,'2026-09-11')
                self.assertIsInstance(normalized,tuple)
                self.assertIsInstance(normalized[0],str)
                self.assertIsInstance(normalized[1],bool)
        stats=mapping.base_name.cache_info()
        self.assertEqual(stats.misses,len(names))
        self.assertEqual(stats.hits,len(names)*499)

    def test_query_date_is_part_of_key_and_other_date_markers_stay_intact(self):
        name='2026年9月11日的收盘价收益率'
        self.assertEqual(mapping.base_name(name,'2026-09-11'),('收盘价收益率',True))
        self.assertEqual(mapping.base_name(name,'2026-09-10'),(name,False))
        self.assertEqual(mapping.base_name(name,'2026-09-11'),('收盘价收益率',True))
        self.assertEqual(mapping.base_name.cache_info().misses,2)

    def test_cache_is_bounded_for_many_dates_or_unrecognized_field_names(self):
        limit=mapping.base_name.cache_info().maxsize
        self.assertEqual(limit,8192)
        for index in range(limit+5):
            mapping.base_name(f'未映射字段{index}','2026-09-11')
        self.assertEqual(mapping.base_name.cache_info().currsize,limit)

    def test_cached_and_uncached_normalization_preserve_values_units_and_evidence(self):
        fields=[('Wind代码','809336.IB',None),('主证券代码','809336.IB',None),
                ('证券简称','26河北23',None),('债务主体名称','河北省人民政府',None),
                ('所属概念板块','地方政府一般债',None),('发行起始日期','2026-04-20',None),
                ('发行总额',882000,'万元'),('剩余期限',9.6082,'年'),
                ('2026年9月11日的收盘价收益率',176.91,'bp'),
                ('2026年9月11日的票面利率_发行时',1.89,'%'),
                ('2026年9月11日的基于净价的收盘价修正久期',8.7035,None),
                ('2026年9月10日的收盘价收益率',5,'%')]
        merge=mapping.Merge('2026-09-11')
        merge.fields['809336.IB']=[dict(name=name,value=value,unit=unit,requestId='saved',
                                       tableIndex=0,rowIndex=0,columnIndex=index)
                                   for index,(name,value,unit) in enumerate(fields)]
        cached=merge.normalize('809336.IB')
        uncached=mapping.base_name.__wrapped__
        with patch.object(mapping,'base_name',uncached):
            expected=merge.normalize('809336.IB')
        self.assertEqual(cached,expected)
        self.assertEqual(cached[0]['yieldPct'],'1.7691')
        self.assertEqual(cached[0]['_descriptive']['couponPct'],'1.89')
        self.assertEqual(cached[1]['couponPct'][0]['name'],'2026年9月11日的票面利率_发行时')
        self.assertEqual(cached[1]['yieldPct'][0]['unit'],'bp')


if __name__=='__main__':
    unittest.main()
