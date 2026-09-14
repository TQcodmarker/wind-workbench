"""The full-sync SDK adapter uses verified identity and preserves pause evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import akshare
import pandas as pd
import requests
import importlib

from backend import storage as store
from backend.akshare_provider import QueryRecorder, initialize, query_detail


class QueryRecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=patch.object(store,'DB',Path(self.temp.name)/'recorder.sqlite3')
        self.db.start()
        store.initialize(seed=False)
        initialize(seed=False)
        self.row={'债券简称':'26河北23','债券代码':'809336','债券类型':'地方政府债','查询代码':'verified-query-code'}
        self.module=importlib.import_module('akshare.bond.bond_info_cm')
        self.original_lookup=self.module.bond_info_cm

    def tearDown(self):
        self.db.stop()
        self.temp.cleanup()

    def fake_detail(self,symbol):
        lookup=self.module.bond_info_cm(bond_name=symbol)
        self.assertEqual(lookup.iloc[0]['查询代码'],'verified-query-code')
        requests.post('https://www.chinamoney.com.cn/ags/ms/cm-u-bond-md/BondDetailInfo',data={'bondDefinedCode':lookup.iloc[0]['查询代码']})
        return pd.DataFrame([{'name':'bondCode','value':'809336'}])

    @staticmethod
    def response(*args,**kwargs):
        result=requests.Response()
        result.status_code=200
        result._content=b'{"data":{"bondBaseInfo":{"bondCode":"809336"}}}'
        return result

    def test_verified_catalog_identity_avoids_repeated_lookup_and_records_adapter(self):
        observed=[]
        recorder=QueryRecorder({'runId':'test-full-sync','targetDate':'2026-09-11'},timeout_seconds=60,on_response=observed.append)
        with patch.object(akshare,'bond_info_detail_cm',self.fake_detail),patch.object(requests.Session,'send',self.response):
            rows,entry=recorder.query('bond_info_detail_cm',{'symbol':'26河北23'},['name','value'],compatibility=True,resolved_lookup=[self.row])
        self.assertEqual(rows[0]['value'],'809336')
        self.assertEqual(len(observed),1)
        self.assertIn('bondDefinedCode=verified-query-code',observed[0]['requestBody'])
        self.assertEqual(query_detail(entry['requestId'])['resolvedLookup'],[self.row])
        self.assertIs(self.module.bond_info_cm,self.original_lookup)

    def test_pause_hook_restores_sdk_and_saves_received_response(self):
        def pause(response):
            raise InterruptedError('requested pause')
        recorder=QueryRecorder({'runId':'test-paused','targetDate':'2026-09-11'},on_response=pause)
        with patch.object(akshare,'bond_info_detail_cm',self.fake_detail),patch.object(requests.Session,'send',self.response):
            with self.assertRaises(InterruptedError):
                recorder.query('bond_info_detail_cm',{'symbol':'26河北23'},['name','value'],compatibility=True,resolved_lookup=[self.row])
        self.assertIs(self.module.bond_info_cm,self.original_lookup)
        entry=query_detail(recorder.queries[0]['requestId'])
        self.assertEqual(entry['status'],'failed')
        self.assertEqual(entry['responses'][0]['body']['data']['bondBaseInfo']['bondCode'],'809336')

    def test_wrong_name_or_missing_identity_is_rejected_before_any_request(self):
        recorder=QueryRecorder({'runId':'test-invalid','targetDate':'2026-09-11'})
        for changed in ({'债券简称':'another'},{'查询代码':''},{'债券代码':'bad'},{'债券类型':'公司债'}):
            with self.subTest(changed=changed),patch.object(requests.Session,'send',side_effect=AssertionError('No network allowed')):
                with self.assertRaises(ValueError):
                    recorder.query('bond_info_detail_cm',{'symbol':'26河北23'},['name','value'],compatibility=True,resolved_lookup=[dict(self.row,**changed)])


if __name__=='__main__':
    unittest.main()
