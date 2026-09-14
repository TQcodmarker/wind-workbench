import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from backend import storage as store
from backend.credentials import read_wind_key, save_wind_key, clear_wind_key
from backend.domain import DemoReader, calculate
from backend.wind_mcp import WindMCP, WindError
from backend.wind_verification import inspect_universe, inspect_valuation, tables_from


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_notification_paginated_discovery_and_sse_call(self):
        seen = []
        def handle(request):
            body = json.loads(request.content)
            seen.append(body['method'])
            method = body['method']
            if method == 'initialize':
                result = {'protocolVersion':'2024-11-05'}
            else:
                self.assertEqual(request.headers['mcp-session-id'], 'session-test')
                self.assertEqual(request.headers['mcp-protocol-version'], '2024-11-05')
                if method == 'notifications/initialized':
                    self.assertNotIn('id', body)
                    return httpx.Response(202)
                if method == 'tools/list':
                    result = {'tools':[{'name':'read-bonds','inputSchema':{}}], 'nextCursor':'page2'} if not body['params'] else {'tools':[]}
                else:
                    self.assertEqual(body['params'], {'name':'read-bonds','arguments':{'date':'2026-09-10'}})
                    event = {'jsonrpc':'2.0','id':body['id'],'result':{'content':[{'type':'text','text':'[]'}]}}
                    return httpx.Response(200,headers={'content-type':'text/event-stream'},text=': keepalive\n\ndata: '+json.dumps(event)+'\n\n')
            return httpx.Response(200,headers={'mcp-session-id':'session-test'},json={'jsonrpc':'2.0','id':body['id'],'result':result})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            async with WindMCP('test-key', client) as mcp:
                self.assertEqual(len(await mcp.list_tools()), 1)
                self.assertEqual((await mcp.call('read-bonds', {'date':'2026-09-10'}))['content'][0]['text'], '[]')
                with self.assertRaises(WindError):
                    await mcp.call('guessed-tool', {})
        self.assertEqual(seen, ['initialize','notifications/initialized','tools/list','tools/list','tools/call'])

    async def test_errors_do_not_expose_provider_body_or_key(self):
        for status, body in [(401, {'secret':'test-key'}), (200, {'id':1,'error':{'code':-32603,'message':'test-key'}})]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))) as client:
                with self.assertRaises(WindError) as error:
                    async with WindMCP('test-key', client):
                        pass
                self.assertNotIn('test-key',str(error.exception))

    async def test_repeated_cursor_is_failure_not_partial_catalog(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,json={'id':json.loads(request.content)['id'],'result':{'tools':[],'nextCursor':'same'}}))) as client:
            mcp = WindMCP('test-key',client)
            with self.assertRaisesRegex(WindError,'分页游标重复'):
                await mcp.list_tools()


class RealModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(store,'DB',Path(self.temp.name)/'wind.sqlite3')
        self.mode_patch = patch.object(store,'MODE','wind')
        self.db_patch.start(); self.mode_patch.start()
        store.initialize()

    def tearDown(self):
        self.db_patch.stop(); self.mode_patch.stop(); self.temp.cleanup()

    def test_default_real_library_is_empty_and_never_seeds(self):
        self.assertEqual(store.ready_dates(), [])
        self.assertEqual(store.runs(), [])
        self.assertIsNone(store.read_day('2026-09-10')['snapshot'])

    def test_real_worker_cannot_fall_back_to_demo(self):
        from backend.worker import drain
        run = store.enqueue('2026-09-10')
        with patch('backend.wind_verification.read_wind_key',return_value=''), patch.object(DemoReader,'fetch',side_effect=AssertionError('Must not use demo')) as demo:
            drain()
        demo.assert_not_called()
        self.assertEqual(store.get_run(run['runId'])['status'], 'failed')
        self.assertIsNone(store.read_day('2026-09-10')['snapshot'])
        with self.assertRaises(ValueError):
            store.publish_no_data(run['runId'])

    def test_real_publish_requires_source_and_matching_date(self):
        run = store.enqueue('2026-09-10')
        cells, counts = calculate([], '2026-09-10')
        for provenance in (None, {'source':'demo','complete':True}, {'source':'wind','complete':False}, {'source':'wind','complete':True,'evaluationDate':'2026-09-09'}):
            with self.assertRaises(ValueError):
                store.publish(run['runId'],cells,counts,provenance)
        self.assertIsNone(store.read_day('2026-09-10')['snapshot'])

    def test_mode_cannot_change_on_same_database(self):
        with patch.object(store,'MODE','demo'), self.assertRaises(RuntimeError):
            store.initialize()

    @unittest.skipUnless(os.name=='nt','DPAPI requires Windows')
    def test_database_credentials_survive_new_reader_without_plaintext(self):
        with patch.dict(os.environ,{},clear=True):
            save_wind_key('TEST_CREDENTIAL_NOT_REAL')
            self.assertEqual(read_wind_key(), 'TEST_CREDENTIAL_NOT_REAL')
            data = (Path(self.temp.name)/'wind-config.sqlite3').read_bytes()
            self.assertNotIn(b'TEST_CREDENTIAL_NOT_REAL',data)
            self.assertFalse((Path(self.temp.name)/'wind-key.dpapi').exists())
            clear_wind_key()
            self.assertEqual(read_wind_key(),'')


class VerificationTests(unittest.TestCase):
    def test_truncated_duplicate_tables_do_not_count_as_complete(self):
        table = {'columns':[{'name':'Wind代码'}], 'rows':[['233890.SH'],['233889.SH']]}
        total = {'columns':[{'name':'上海债券总条数'}], 'rows':[[625]]}
        result = inspect_universe([table,table,total])
        self.assertEqual(result['received'],2)
        self.assertEqual(result['reportedTotal'],625)
        self.assertFalse(result['complete'])

    def test_nonempty_yields_use_query_date_and_zero_is_valid(self):
        columns = [{'name':'Wind代码'}, {'name':'2026年9月10日收盘价到期收益率','unit':'%'}, {'name':'实际收益率日期'}]
        table = {'columns':columns,'rows':[['A',None,'2026-09-10'],['B',0,'2026-09-10'],['C',1.5,'2026-09-09'],['D',1.8,None]]}
        result = inspect_valuation([table], '2026-09-10')
        self.assertEqual(result['nonNullYields'],3)
        self.assertEqual(result['verifiedDateYields'],3)
        self.assertEqual(result['dateBasis'],'query_date')

    def test_no_data_text_and_malformed_table_are_not_a_snapshot(self):
        self.assertEqual(tables_from({'content':[{'type':'text','text':'没找到数据'}]}),[])
        malformed = {'data':{'data':[{'columns':[{'name':'Wind代码'}],'rows':[['A','B']]}]},'error':None}
        with self.assertRaises(WindError):
            tables_from({'content':[{'type':'text','text':json.dumps(malformed)}]})


if __name__ == '__main__':
    unittest.main()
