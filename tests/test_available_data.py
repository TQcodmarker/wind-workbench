import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend import storage as store
from backend.available_data import available_dates, read_available
from backend.domain import RULES
from backend.wind_mapping import VERSION


TARGET = '2026-09-11'
CODE = '809336.IB'


def result(values):
    columns = [dict(name=name) if isinstance(name, str) else dict(name=name[0], unit=name[1])
               for name, _ in values]
    table = dict(columns=columns, rows=[[value for _, value in values]])
    return dict(jsonrpc='2.0', result=dict(content=[dict(type='text', text=json.dumps(
        dict(data=dict(data=[table])), ensure_ascii=False))]))


def complete(**overrides):
    values = {'Wind代码': CODE, '主证券代码': CODE, '证券简称': '26河北23',
              '债务主体名称': '河北省人民政府', '所属概念板块': '地方政府一般债',
              '发行起始日期': '2026-04-20', '到期日期': '2036-04-21',
              '发行总额': 88.2, '剩余期限': 9.6082, '收盘价收益率': 1.7691}
    values.update(overrides)
    return list(values.items())


class AvailableDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'saved.sqlite3'
        self.db_patch = patch.object(store, 'DB', self.path)
        self.db_patch.start()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE source_sessions(id TEXT PRIMARY KEY,target_date TEXT);
                CREATE TABLE source_requests(id TEXT PRIMARY KEY,session_id TEXT,
                    method TEXT,response TEXT,started_at TEXT,status TEXT);
                CREATE TABLE snapshots(run_id TEXT,payload TEXT);
                INSERT INTO snapshots VALUES('published-old','keep-existing-snapshot');
            ''')
        self.index = 0

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def record(self, values=None, target=TARGET, session=None, status='succeeded', raw=None):
        self.index += 1
        rid = f'request-{self.index}'
        sid = session or f'session-{self.index}'
        response = json.dumps(result(values), ensure_ascii=False) if raw is None else raw
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT OR IGNORE INTO source_sessions VALUES(?,?)', (sid, target))
            db.execute('INSERT INTO source_requests VALUES(?,?,?,?,?,?)',
                       (rid, sid, 'tools/call', response, f'2026-09-12T12:00:{self.index:02}', status))
        return rid, sid

    def test_partial_rows_from_failed_runs_remain_visible_without_wind_or_writes(self):
        self.record(complete())
        rid, sid = self.record([('Wind代码', '2305973.IB'), ('主证券代码', '2305973.IB'),
                              ('证券简称', '23上海债14'), ('债务主体名称', '上海市人民政府'),
                              ('发行起始日期', '2023-08-21'), (('债券余额', '亿'), 2.3)], status='failed')
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch('backend.wind_mcp.WindMCP.__init__', side_effect=AssertionError('No Wind calls')):
            data = read_available(TARGET)
            self.assertEqual(available_dates(), [TARGET])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertEqual(data['counts'], dict(bonds=2, eligible=1, incomplete=1, excluded=0,
                                             conflicted=0, requests=2, sessions=2))
        self.assertEqual(data['scope'], 'saved_sample')
        self.assertFalse(data['complete'])
        self.assertEqual((data['rulesVersion'], data['mappingVersion']), (RULES['version'], VERSION))
        bond = data['bonds'][1]
        self.assertEqual(bond['name'], '23上海债14')
        self.assertEqual(bond['outstandingBalanceYi'], '2.3')
        self.assertIn('收盘价到期收益率', bond['missingFields'])
        self.assertEqual((bond['requestIds'], bond['sessionIds']), ([rid], [sid]))
        cell = bond['fieldSources']['outstandingBalanceYi'][0]
        self.assertEqual((cell['name'], cell['unit'], cell['value'], cell['requestId'], cell['sessionId']),
                         ('债券余额', '亿', 2.3, rid, sid))
        self.assertTrue(all(cell['sampleCount'] == 1 for cell in data['cells']))
        self.assertEqual(data['cells'][0]['yieldPct'], '1.76910000')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT payload FROM snapshots').fetchone()[0], 'keep-existing-snapshot')

    def test_exact_session_date_isolation_and_no_date_inference_from_undated_sessions(self):
        self.record(complete(**{'收盘价收益率': 1.7}), target='2026-09-10')
        self.record(complete(**{'收盘价收益率': 1.8}), target=TARGET)
        self.record(complete(**{'收盘价收益率': 9}), target=None)
        self.assertEqual(read_available(TARGET)['bonds'][0]['yieldPct'], '1.8')
        self.assertEqual(read_available('2026-09-10')['bonds'][0]['yieldPct'], '1.7')
        self.assertEqual(read_available('2026-09-09')['counts']['bonds'], 0)
        self.assertEqual(available_dates(), [TARGET, '2026-09-10'])

    def test_cross_market_repeats_and_empty_alternatives_merge_once_with_all_evidence(self):
        self.record(complete(**{'跨市场代码': '236633.SH'}), session='same-session')
        self.record([('Wind代码', '236633.SH'), ('主证券代码', CODE),
                     (('到期收益率', 'bp'), 176.91), (('债券余额', '万元'), 882000)], session='same-session')
        self.record([('Wind代码', CODE), ('到期收益率', None)])
        data = read_available(TARGET)
        self.assertEqual(data['counts']['bonds'], 1)
        self.assertEqual(data['counts']['eligible'], 1)
        self.assertEqual((data['counts']['requests'], data['counts']['sessions']), (3, 2))
        bond = data['bonds'][0]
        self.assertEqual(bond['codes'], [CODE, '236633.SH'])
        self.assertEqual(bond['bondId'], CODE)
        self.assertEqual(bond['outstandingBalanceYi'], '88.2000')
        self.assertEqual(len(bond['fieldSources']['yieldPct']), 3)
        self.assertEqual(data['cells'][0]['sampleCount'], 1)
        self.assertEqual(data['cells'][0]['issueAmountSumYi'], '88.2')

    def test_empty_primary_code_falls_back_per_row_without_losing_other_rows(self):
        response = result(complete(**{'主证券代码': None}))
        payload = json.loads(response['result']['content'][0]['text'])
        table = payload['data']['data'][0]
        other = list(table['rows'][0])
        other[0] = other[1] = '809337.IB'
        invalid = list(table['rows'][0])
        invalid[0] = '无效代码'
        table['rows'] += [invalid, other]
        response['result']['content'][0]['text'] = json.dumps(payload, ensure_ascii=False)
        rid, _ = self.record(raw=json.dumps(response, ensure_ascii=False))
        data = read_available(TARGET)
        self.assertEqual(data['counts']['bonds'], 2)
        self.assertEqual(data['counts']['eligible'], 1)
        self.assertEqual(data['counts']['incomplete'], 1)
        fallback = next(bond for bond in data['bonds'] if bond['code'] == CODE)
        self.assertEqual(fallback['yieldPct'], '1.7691')
        self.assertIsNone(fallback['bondId'])
        self.assertIsNone(fallback['fieldSources']['bondId'][0]['value'])
        self.assertEqual(fallback['fieldSources']['yieldPct'][0]['rowIndex'], 0)
        other = next(bond for bond in data['bonds'] if bond['code'] == '809337.IB')
        self.assertEqual(other['fieldSources']['yieldPct'][0]['rowIndex'], 2)
        self.assertTrue(any(rid in warning and '第 2 行无有效债券代码' in warning
                            for warning in data['warnings']))
        self.assertEqual(available_dates(), [TARGET])

    def test_invalid_primary_code_falls_back_to_valid_wind_code_for_display(self):
        self.record(complete(**{'主证券代码': '未提供'}))
        bond = read_available(TARGET)['bonds'][0]
        self.assertEqual(bond['code'], CODE)
        self.assertEqual(bond['yieldPct'], '1.7691')
        self.assertEqual(bond['disposition'], 'incomplete')
        self.assertEqual(bond['fieldSources']['bondId'][0]['value'], '未提供')

    def test_reciprocal_cross_market_links_without_primary_code_share_one_identity(self):
        first = [(key, value) for key, value in complete(**{'跨市场代码': '236633.SH'})
                 if key != '主证券代码']
        rid1, _ = self.record(first)
        rid2, _ = self.record([('Wind代码', '236633.SH'), ('跨市场代码', CODE),
                              ('收盘价收益率', 1.7691)])
        data = read_available(TARGET)
        self.assertEqual(data['counts']['bonds'], 1)
        self.assertEqual(data['counts']['eligible'], 1)
        bond = data['bonds'][0]
        self.assertEqual(bond['bondId'], CODE)
        self.assertEqual(bond['identityBasis'], 'explicit_cross_market_links')
        self.assertEqual(bond['validationErrors'], [])
        self.assertEqual([(cell['name'], cell['value'], cell['requestId'])
                          for cell in bond['fieldSources']['bondId']],
                         [('跨市场代码', '236633.SH', rid1), ('跨市场代码', CODE, rid2)])
        self.assertEqual(data['cells'][0]['sampleCount'], 1)
        self.assertEqual(data['cells'][0]['issueAmountSumYi'], '88.2')

    def test_cross_market_canonical_identity_keeps_real_market_value_conflicts(self):
        first = [(key, value) for key, value in complete(**{'跨市场代码': '236633.SH'})
                 if key != '主证券代码']
        self.record(first)
        self.record([('Wind代码', '236633.SH'), ('跨市场代码', CODE), ('收盘价收益率', 1.9)])
        data = read_available(TARGET)
        self.assertEqual(data['bonds'][0]['disposition'], 'conflicted')
        self.assertEqual(data['bonds'][0]['bondId'], CODE)
        self.assertTrue(any('yieldPct' in error for error in data['bonds'][0]['validationErrors']))
        self.assertEqual(data['cells'], [])

    def test_yield_conflict_excludes_aggregation_but_keeps_other_fields_and_sources(self):
        self.record(complete(**{'修正久期': 8.7035}))
        self.record([('Wind代码', CODE), ('收盘价收益率', 1.9)])
        data = read_available(TARGET)
        bond = data['bonds'][0]
        self.assertEqual(bond['disposition'], 'conflicted')
        self.assertIsNone(bond['yieldPct'])
        self.assertEqual(bond['duration'], '8.7035')
        self.assertEqual(bond['issueAmountYi'], '88.2')
        self.assertEqual(len(bond['fieldSources']['yieldPct']), 2)
        self.assertEqual(data['counts']['conflicted'], 1)
        self.assertEqual(data['cells'], [])

    def test_optional_field_conflict_is_visible_and_not_silently_chosen(self):
        self.record(complete(**{'修正久期': 8.7}))
        self.record([('Wind代码', CODE), ('修正久期', 9)])
        data = read_available(TARGET)
        bond = data['bonds'][0]
        self.assertEqual(bond['disposition'], 'conflicted')
        self.assertIsNone(bond['duration'])
        self.assertEqual(bond['yieldPct'], '1.7691')
        self.assertTrue(any('duration' in error for error in bond['validationErrors']))
        self.assertEqual(data['cells'], [])

    def test_zero_values_are_preserved_and_zero_yield_is_eligible(self):
        self.record(complete(**{'收盘价收益率': 0, '修正久期': 0, '票面利率': 0,
                                '债券余额': 0, '收盘价净价': 0}))
        data = read_available(TARGET)
        bond = data['bonds'][0]
        self.assertEqual(bond['disposition'], 'eligible')
        for field in ('yieldPct', 'duration', 'couponPct', 'outstandingBalanceYi', 'closeNetPrice'):
            self.assertEqual(bond[field], '0')
        self.assertEqual(bond['missingFields'], [])
        self.assertEqual(data['cells'][0]['yieldPct'], '0E-8')

    def test_zero_issuance_is_excluded_not_missing(self):
        self.record(complete(**{'发行总额': 0}))
        data = read_available(TARGET)
        self.assertEqual(data['bonds'][0]['disposition'], 'excluded')
        self.assertEqual(data['bonds'][0]['missingFields'], [])
        self.assertEqual(data['counts']['excluded'], 1)
        self.assertEqual(data['cells'], [])

    def test_optional_missing_fields_do_not_block_aggregate_and_clauses_are_absent(self):
        self.record(complete(**{'提前偿还': True, '发行人赎回': True, '特殊条款': '发行人赎回'}))
        data = read_available(TARGET)
        bond = data['bonds'][0]
        self.assertEqual(bond['disposition'], 'eligible')
        self.assertIsNone(bond['duration'])
        self.assertEqual(bond['missingFields'], [])
        encoded = json.dumps(data, ensure_ascii=False)
        for field in ('提前偿还', '发行人赎回', '特殊条款', 'earlyRepayment', 'redemption'):
            self.assertNotIn(field, encoded)

    def test_conflicting_primary_identity_is_flagged_without_dropping_the_bond(self):
        self.record(complete())
        self.record([('Wind代码', CODE), ('主证券代码', '809337.IB')])
        data = read_available(TARGET)
        self.assertEqual(data['counts']['bonds'], 1)
        self.assertEqual(data['bonds'][0]['disposition'], 'conflicted')
        self.assertEqual(data['bonds'][0]['name'], '26河北23')
        self.assertEqual(data['cells'], [])

    def test_dated_basic_aliases_merge_cross_market_identity_and_semantic_bond_type(self):
        values=[(f'2026年9月11日的{key}' if key in ('主证券代码','所属概念板块') else key,value)
                for key,value in complete()]
        values += [('2026年9月11日的跨市场代码','236633.SH'),
                   ('2026年9月11日的地方债类型','一般债')]
        first,_=self.record(values)
        second,_=self.record([('2026年9月11日的Wind代码','236633.SH'),
                              ('2026年9月11日的主证券代码',CODE),
                              ('收盘价收益率',1.7691)])
        data=read_available(TARGET)
        self.assertEqual(data['counts']['bonds'],1)
        self.assertEqual(data['counts']['eligible'],1)
        bond=data['bonds'][0]
        self.assertEqual((bond['code'],bond['bondId'],bond['bondType']),(CODE,CODE,'general'))
        self.assertEqual(bond['codes'],[CODE,'236633.SH'])
        self.assertEqual(bond['validationErrors'],[])
        self.assertEqual(bond['requestIds'],[first,second])
        self.assertEqual([cell['name'] for cell in bond['fieldSources']['bondId']],
                         ['2026年9月11日的主证券代码']*2)
        self.assertEqual([cell['value'] for cell in bond['fieldSources']['bondType']],
                         ['地方政府一般债','一般债'])

    def test_dated_reciprocal_cross_market_links_and_other_date_identity_stay_distinct(self):
        first=[(key,value) for key,value in complete() if key!='主证券代码']
        first.append(('2026年9月11日的跨市场代码','236633.SH'))
        self.record(first)
        self.record([('Wind代码','236633.SH'),('2026年9月11日的跨市场代码',CODE)])
        self.record([('Wind代码','809337.IB'),('2026年9月10日的主证券代码',CODE),
                     ('2026年9月10日的跨市场代码','236633.SH')])
        data=read_available(TARGET)
        self.assertEqual(data['counts']['bonds'],2)
        self.assertEqual(data['counts']['eligible'],1)
        bond=next(bond for bond in data['bonds'] if bond['code']==CODE)
        self.assertEqual(bond['identityBasis'],'explicit_cross_market_links')
        self.assertEqual(bond['validationErrors'],[])
        other=next(bond for bond in data['bonds'] if bond['code']=='809337.IB')
        self.assertEqual(other['codes'],['809337.IB'])
        self.assertIsNone(other['bondId'])

    def test_dated_conflicting_primary_identity_and_classification_remain_conflicts(self):
        self.record(complete())
        self.record([('Wind代码',CODE),('2026年9月11日的主证券代码',CODE),
                     ('2026年9月11日的地方债类型','一般债'),
                     ('2026年9月11日的所属概念板块','地方政府专项债')])
        self.record([('Wind代码',CODE),('2026年9月11日的主证券代码','809337.IB')])
        data=read_available(TARGET)
        self.assertEqual(data['counts']['bonds'],1)
        bond=data['bonds'][0]
        self.assertEqual(bond['disposition'],'conflicted')
        self.assertIn('bondType 多次返回值冲突',bond['validationErrors'])
        self.assertIn('bondId 多次返回值冲突',bond['validationErrors'])
        self.assertEqual(data['cells'],[])

    def test_bad_response_does_not_hide_other_saved_bonds(self):
        self.record(complete())
        rid, _ = self.record(raw='not-json')
        self.record(raw=json.dumps({'result': {'content': [{'type': 'text', 'text': '没找到数据'}]}}))
        data = read_available(TARGET)
        self.assertEqual(data['counts']['bonds'], 1)
        self.assertEqual(data['counts']['requests'], 1)
        self.assertTrue(any(rid in warning for warning in data['warnings']))

    def test_missing_database_is_not_created_by_reads(self):
        missing = Path(self.temp.name)/'absent.sqlite3'
        with patch.object(store, 'DB', missing):
            self.assertEqual(read_available(TARGET)['bonds'], [])
            self.assertEqual(available_dates(), [])
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
