import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend import storage as store
from backend.saved_bond_metrics import load_saved_metrics


TARGET = '2026-09-11'
CODE = '809336.IB'


def response(fields):
    columns = [dict(name=key) if isinstance(key, str) else dict(name=key[0], unit=key[1])
               for key, _ in fields]
    table = dict(columns=columns, rows=[[value for _, value in fields]])
    return {'result': {'content': [{'type': 'text', 'text': json.dumps({'data': {'data': [table]}})}]}}


class SavedBondMetricTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'saved.sqlite3'
        self.db_patch = patch.object(store, 'DB', self.path)
        self.db_patch.start()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''CREATE TABLE source_sessions (id TEXT PRIMARY KEY, target_date TEXT);
                CREATE TABLE source_requests (id TEXT PRIMARY KEY, session_id TEXT, method TEXT,
                response TEXT, started_at TEXT, status TEXT);
                CREATE TABLE snapshots (payload TEXT);
                INSERT INTO snapshots VALUES ('leave the prior projection unchanged');''')
        self.index = 0

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def save(self, fields, *, code=CODE, target=TARGET, status='succeeded'):
        self.index += 1
        rid, sid = f'r-{self.index}', f's-{self.index}'
        raw = json.dumps(response([('Wind代码', code), *fields]), ensure_ascii=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO source_sessions VALUES (?,?)', (sid, target))
            db.execute('INSERT INTO source_requests VALUES (?,?,?,?,?,?)',
                       (rid, sid, 'tools/call', raw, str(self.index).zfill(3), status))
        return rid, sid

    def test_dated_real_metrics_units_and_complete_cell_provenance_without_writes(self):
        rid, sid = self.save([(('2026年9月11日中债估值收益率', 'bp'), 181.25),
                              (('2026-09-11收盘价到期收益率', '小数'), 0.0185),
                              (('2026-09-11基于净价的收盘价修正久期', '年'), 8.7)])
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch('backend.wind_mcp.WindMCP.__init__', side_effect=AssertionError('No network')):
            metrics = load_saved_metrics(TARGET)[CODE]
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertEqual([(x['metric'], x['value'], x['unit']) for x in metrics],
                         [('chinabond_valuation', '1.8125', '%'), ('ytm', '1.8500', '%'),
                          ('modified_duration', '8.7', '年')])
        self.assertEqual(metrics[0]['priceBasis'], 'valuation')
        source = metrics[0]['fieldSources'][0]
        self.assertEqual((source['requestId'], source['sessionId'], source['value'], source['unit']),
                         (rid, sid, 181.25, 'bp'))
        self.assertEqual((source['tableIndex'], source['rowIndex'], source['columnIndex']), (0, 0, 1))
        self.assertEqual((source['sourceDate'], source['source'], source['entityGrain']), (TARGET, 'wind', 'bond'))
        self.assertEqual(metrics[0]['dateBasis'], 'field_date')

    def test_query_date_alone_is_insufficient_and_explicit_actual_date_is_accepted(self):
        self.save([('中债估值收益率', 1.8)])
        self.assertEqual(load_saved_metrics(TARGET), {})
        self.save([('中债估值收益率', 1.9), ('数据日期', TARGET)])
        items = load_saved_metrics(TARGET)[CODE]
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]['value'], items[0]['dateBasis']), ('1.9', 'reported_date'))

    def test_wrong_header_or_session_date_never_crosses_date_boundary(self):
        self.save([('2026年9月10日中债估值收益率', 1.8)])
        self.save([('2026-09-11中债估值收益率', 1.9)], target='2026-09-10')
        self.save([('2026-09-11中债估值收益率', 2)], target=None)
        self.save([('2026-09-11中债估值收益率', 2.1), ('数据日期', '2026-09-10')])
        self.assertEqual(load_saved_metrics(TARGET), {})

    def test_specific_actual_duration_time_in_separate_request_overrides_header(self):
        self.save([('2026-09-11基于净价的收盘价修正久期', 0.0055),
                   ('2026-09-11收盘价收益率', 1.5)])
        self.save([('2026-09-11基于净价的收盘价修正久期时间', '2026-08-28')])
        items = load_saved_metrics(TARGET)[CODE]
        self.assertEqual([x['metric'] for x in items], ['ytm'])

    def test_close_price_time_overrides_both_close_metrics_but_not_valuation(self):
        self.save([('2026-09-11收盘价修正久期', 8.7), ('2026-09-11到期收益率', 1.8),
                   ('2026-09-11中债估值收益率', 1.9), ('收盘价净价时间', '2026-09-10')])
        self.assertEqual([x['metric'] for x in load_saved_metrics(TARGET)[CODE]], ['chinabond_valuation'])

    def test_same_day_actual_time_remains_in_evidence(self):
        _, sid = self.save([('2026年09月11日收盘价修正久期', 8.7),
                            ('收盘价修正久期时间', '2026/09/11')])
        item = load_saved_metrics(TARGET)[CODE][0]
        self.assertEqual(item['dateBasis'], 'reported_date')
        self.assertTrue(any(s['value'] == '2026/09/11' and s['sessionId'] == sid
                            for s in item['fieldSources']))

    def test_invalid_or_ambiguous_numbers_units_and_duration_kind_are_rejected(self):
        for name, value, unit in [('中债估值收益率', None, '%'), ('中债估值收益率', '--', '%'),
                                  ('中债估值收益率', 'NaN', '%'), ('中债估值收益率', True, '%'),
                                  ('中债估值收益率', 1.8, '未知'), ('收盘价修正久期', -1, '年'),
                                  ('收盘价修正久期', 0, '年'), ('收盘价修正久期', 8, '月'),
                                  ('久期', 8, '年'), ('麦考利久期', 8, '年')]:
            self.save([((TARGET + name, unit), value)])
        self.assertEqual(load_saved_metrics(TARGET), {})

    def test_zero_and_negative_yields_remain_valid_and_conflicts_are_not_resolved(self):
        self.save([(TARGET + '中债估值收益率', 0)])
        self.save([(TARGET + '中债估值收益率', -0.01)], status='failed')
        items = load_saved_metrics(TARGET)[CODE]
        self.assertEqual([x['value'] for x in items], ['0', '-0.01'])
        self.assertNotEqual(items[0]['fieldSources'][0]['requestId'], items[1]['fieldSources'][0]['requestId'])

    def test_index_curve_reference_and_fake_proxy_are_never_individual_metrics(self):
        self.save([(TARGET + '中债4202曲线收益率', 1.8), (TARGET + '地方债指数修正久期', 8.9),
                   (TARGET + '修正久期（指数参考）', 8.9)])
        self.save([(TARGET + '中债估值收益率', 1.8)], code='CBA05801.CS')
        self.save([(TARGET + '中债估值收益率', 1.8), ('证券简称', '地方政府债指数')])
        self.save([(TARGET + '收盘价修正久期', 8.9), ('久期来源', 'index proxy')])
        self.assertEqual(load_saved_metrics(TARGET), {})

    def test_incompatible_yield_metadata_is_rejected(self):
        self.save([(TARGET + '到期收益率', 1.8), ('收益率类型', '中债估值收益率')])
        self.save([(TARGET + '中债估值收益率', 1.9), ('价格口径', '收盘价')])
        self.save([(TARGET + '中债估值收益率', 2), ('数据粒度', 'index')])
        self.assertEqual(load_saved_metrics(TARGET), {})

    def test_exchange_mapping_requires_explicit_identity_and_preserves_its_evidence(self):
        self.save([(TARGET + '收盘价修正久期', 8.7)], code='236633.SH')
        self.assertEqual(load_saved_metrics(TARGET), {})
        rid, _ = self.save([('跨市场代码', '236633.SH')])
        item = load_saved_metrics(TARGET)[CODE][0]
        self.assertEqual(item['sourceCode'], '236633.SH')
        self.assertTrue(any(s['requestId'] == rid and s['value'] == '236633.SH' for s in item['fieldSources']))
        self.save([('跨市场代码', '236633.SH')], code='809337.IB')
        self.assertEqual(load_saved_metrics(TARGET), {})

    def test_numeric_prefix_is_never_used_as_cross_market_identity(self):
        self.save([(TARGET + '收盘价修正久期', 8.7)], code='809336.SH')
        self.save([('证券简称', 'same name')])
        self.assertEqual(load_saved_metrics(TARGET), {})

    def test_missing_database_and_missing_source_schema_do_not_create_tables(self):
        missing = Path(self.temp.name) / 'does-not-exist.sqlite3'
        with patch.object(store, 'DB', missing):
            self.assertEqual(load_saved_metrics(TARGET), {})
        self.assertFalse(missing.exists())
        empty = Path(self.temp.name) / 'empty.sqlite3'
        with closing(sqlite3.connect(empty)):
            pass
        with patch.object(store, 'DB', empty):
            self.assertEqual(load_saved_metrics(TARGET), {})
        with closing(sqlite3.connect(empty)) as db:
            self.assertEqual(list(db.execute('SELECT name FROM sqlite_master')), [])


if __name__ == '__main__':
    unittest.main()
