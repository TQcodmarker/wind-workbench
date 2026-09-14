import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend import available_data as available
from backend import storage as store


TARGET = '2026-09-11'
CODE = '809336.IB'


def response(yield_value=1.7691):
    values = {'Wind代码': CODE, '主证券代码': CODE, '跨市场代码': '236633.SH',
              '证券简称': '26河北23', '债务主体名称': '河北省人民政府',
              '所属概念板块': '地方政府一般债', '发行起始日期': '2026-04-20',
              '发行总额': 88.2, '剩余期限': 9.6082, '收盘价收益率': yield_value}
    table = dict(columns=[dict(name=name) for name in values], rows=[list(values.values())])
    payload = dict(data=dict(data=[table]))
    return json.dumps(dict(result=dict(content=[dict(type='text', text=json.dumps(
        payload, ensure_ascii=False))])), ensure_ascii=False)


class AvailableCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'saved.sqlite3'
        self.db_patch = patch.object(store, 'DB', self.path)
        self.db_patch.start()
        available.clear_available_cache()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE source_sessions(id TEXT PRIMARY KEY,target_date TEXT);
                CREATE TABLE source_requests(id TEXT PRIMARY KEY,session_id TEXT,
                    method TEXT,response TEXT,started_at TEXT,finished_at TEXT,
                    status TEXT,response_sha256 TEXT);
                INSERT INTO source_sessions VALUES('session-1','2026-09-11');
                INSERT INTO source_requests VALUES('request-1','session-1','tools/call',
                    NULL,'2026-09-12T12:00:00',NULL,'running',NULL);
            ''')

    def tearDown(self):
        available.clear_available_cache()
        self.db_patch.stop()
        self.temp.cleanup()

    def finish(self, value=1.7691, with_digest=True):
        raw = response(value)
        digest = hashlib.sha256(raw.encode()).hexdigest() if with_digest else None
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE source_requests SET response=?,response_sha256=?,status='success',"
                       "finished_at='2026-09-12T12:01:00' WHERE id='request-1'", (raw, digest))

    def test_default_keeps_evidence_light_list_and_alias_detail_are_detached(self):
        self.finish()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch('backend.wind_mcp.WindMCP.__init__', side_effect=AssertionError('No Wind calls')):
            full = available.read_available(TARGET)
            light = available.read_available(TARGET, include_evidence=False)
            bond = available.read_available_bond(TARGET, '236633.SH')
        self.assertTrue(full['bonds'][0]['fieldSources']['yieldPct'])
        self.assertEqual(light['bonds'][0]['fieldSources'], {})
        self.assertEqual(bond, full['bonds'][0])
        self.assertEqual(light['counts'], full['counts'])
        self.assertEqual(light['cells'], full['cells'])
        self.assertEqual(light['bonds'][0]['requestIds'], ['request-1'])
        self.assertLess(len(json.dumps(light)), len(json.dumps(full))/2)
        light['counts']['bonds'] = 999
        light['bonds'][0]['codes'].append('broken')
        full['bonds'][0]['fieldSources']['yieldPct'][0]['value'] = 999
        bond['name'] = 'changed'
        unchanged = available.read_available(TARGET)
        self.assertEqual(unchanged['counts']['bonds'], 1)
        self.assertNotIn('broken', unchanged['bonds'][0]['codes'])
        self.assertEqual(unchanged['bonds'][0]['fieldSources']['yieldPct'][0]['value'], 1.7691)
        self.assertEqual(available.read_available_bond(TARGET, CODE)['name'], '26河北23')
        self.assertIsNone(available.read_available_bond(TARGET, 'missing'))
        self.assertIsNone(available.read_available_bond('2026-09-10', CODE))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_warm_list_detail_and_dates_do_not_reparse_responses(self):
        self.finish()
        with patch.object(available, '_response_rows', wraps=available._response_rows) as parse:
            available.read_available(TARGET, include_evidence=False)
            available.available_dates()
            calls = parse.call_count
            for _ in range(3):
                available.read_available(TARGET, include_evidence=False)
                available.read_available_bond(TARGET, CODE)
                self.assertEqual(available.available_dates(), [TARGET])
            self.assertEqual(parse.call_count, calls)

    def test_finishing_same_request_id_invalidates_empty_projection_and_date_cache(self):
        self.assertEqual(available.read_available(TARGET)['bonds'], [])
        self.assertEqual(available.available_dates(), [])
        self.finish(0)
        self.assertEqual(available.read_available(TARGET, include_evidence=False)['bonds'][0]['yieldPct'], '0')
        self.assertEqual(available.available_dates(), [TARGET])
        # Neither row count, id nor timestamps change on this correction.
        self.finish(1.9)
        self.assertEqual(available.read_available_bond(TARGET, CODE)['yieldPct'], '1.9')

    def test_legacy_response_without_digest_still_invalidates_when_body_changes(self):
        self.finish(1.8, with_digest=False)
        self.assertEqual(available.read_available_bond(TARGET, CODE)['yieldPct'], '1.8')
        self.finish(1.9, with_digest=False)
        self.assertEqual(available.read_available_bond(TARGET, CODE)['yieldPct'], '1.9')

    def test_policy_changes_even_with_same_version_and_explicit_clear_rebuild(self):
        self.finish()
        with patch.object(available, '_build_available', wraps=available._build_available) as build:
            available.read_available(TARGET, include_evidence=False)
            self.assertEqual(build.call_count, 1)
            changed_rules = dict(available.RULES, formula='Changed formula under same version')
            with patch.object(available, 'RULES', changed_rules):
                available.read_available(TARGET, include_evidence=False)
                self.assertEqual(build.call_count, 2)
                available.read_available(TARGET, include_evidence=False)
                self.assertEqual(build.call_count, 2)
                available.clear_available_cache()
                available.read_available(TARGET, include_evidence=False)
                self.assertEqual(build.call_count, 3)

    def test_switching_database_paths_cannot_reuse_another_database_sample(self):
        self.finish()
        self.assertEqual(available.read_available(TARGET)['counts']['bonds'], 1)
        missing = Path(self.temp.name)/'different.sqlite3'
        with patch.object(store, 'DB', missing):
            self.assertEqual(available.read_available(TARGET, include_evidence=False)['counts']['bonds'], 0)
            self.assertEqual(available.available_dates(), [])
        self.assertFalse(missing.exists())
        self.assertEqual(available.read_available_bond(TARGET, CODE)['yieldPct'], '1.7691')


if __name__ == '__main__':
    unittest.main()
