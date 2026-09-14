import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from backend import storage as store,lineage
from backend.api import app
from backend.wind_mcp import WindMCP,WindError
from backend.wind_pipeline import Pipeline,FIELD_GROUPS
from backend.wind_mapping import Merge


def table(columns,rows):
    return {'columns':[dict(name=c) if isinstance(c,str) else c for c in columns],'rows':rows}


def result(tables):
    return {'content':[{'type':'text','text':json.dumps({'data':{'data':tables},'error':None},ensure_ascii=False)}]}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patches=[patch.object(store,'DB',Path(self.temp.name)/'wind.sqlite3'),patch.object(store,'MODE','wind'),patch('backend.wind_pipeline.read_wind_key',return_value='TEST_KEY')]
        for p in self.patches:p.start()
        store.initialize(seed=False)
        self.incomplete=False;self.null_yield=False;self.fail_stage=False

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def handler(self,request):
        body=json.loads(request.content);method=body['method']
        if method=='initialize':data={'protocolVersion':'2024-11-05'}
        elif method=='notifications/initialized':return httpx.Response(202)
        elif method=='tools/list':data={'tools':[{'name':n} for n in ['get_bond_basicinfo','get_bond_issuer_info','get_bond_market_data']]}
        else:
            q=body['params']['arguments']['question'];codes=re.findall(r'\d{6}\.SH',q)
            if '由' in q:
                if '上海市人民政府' in q:
                    data=result([table(['Wind代码','主证券代码','发行起始日期','债务主体名称'],
                        [['233890.SH','233890.SH','2024-05-15','上海市人民政府'],['233889.SH','233889.SH','2024-05-15','上海市人民政府']]),table(['债券总数'],[[3 if self.incomplete else 2]])])
                else:data=result([table(['债券总数'],[[0]])])
            elif '发行总额' in q:
                data=result([table(['Wind代码','主证券代码','发行起始日期',{'name':'发行总额','unit':'亿'},'交易币种','所属概念板块'],
                    [[c,c,'2024-05-15',10 if c=='233890.SH' else 30,'CNY','地方政府专项债'] for c in codes])])
            elif '发行主体名称' in q:data=result([table(['Wind代码','债务主体名称'],[[c,'上海市人民政府'] for c in codes])])
            elif '收盘价修正久期' in q:
                if self.fail_stage:return httpx.Response(503,text='provider failed')
                data=result([table(['Wind代码','基于净价的收盘价修正久期'],[[c,9] for c in codes])])
            elif '收盘价到期收益率' in q:data=result([table(['Wind代码',{'name':'2026年9月10日收盘价收益率','unit':'%'}],
                [[c,None if self.null_yield else 1 if c=='233890.SH' else 2] for c in codes])])
            elif '剩余期限' in q:data=result([table(['Wind代码',{'name':'剩余期限_下一行权日','unit':'年'}],[[c,10] for c in codes])])
            else:data=result([table(['Wind代码',{'name':'2026年9月10日修正久期','unit':'年'}],[[c,9] for c in codes])])
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':data})

    def execute(self):
        run=store.enqueue('2026-09-10')
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as client:
                factory=lambda key,recorder:WindMCP(key,client=client,recorder=recorder)
                pipeline=Pipeline(run['runId'],client_factory=factory)
                await pipeline.run()
        asyncio.run(go())
        return run

    def test_complete_multitool_join_publish_and_export_reproduces_weighting(self):
        run=self.execute();rid=run['runId']
        day=store.read_day('2026-09-10')
        cell=next(c for c in day['snapshot']['cells'] if c['regionId']=='shanghai' and c['termYears']==10 and c['cohort']=='before_20250808' and c['bondScope']=='special')
        self.assertEqual(cell['yieldPct'],'1.75000000')
        rows=lineage.observations(rid)['items']
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r['disposition']=='included' for r in rows))
        self.assertTrue(all(r['payload']['yieldMetric']=='ytm' and r['payload']['yieldPriceBasis']=='close' and r['payload']['yieldDate']=='2026-09-10' for r in rows))
        self.assertTrue(all('earlyRepayment' not in r['payload'] and 'redemption' not in r['payload'] for r in rows))
        source=rows[0]['field_sources']['yieldPct'][0]
        request=lineage.request_detail(source['requestId'])
        original=json.loads(request['response']['result']['content'][0]['text'])['data']['data'][source['tableIndex']]['rows'][source['rowIndex']][source['columnIndex']]
        self.assertEqual(str(original),rows[0]['payload']['yieldPct'])
        exported=lineage.export_session(lineage.sessions(rid)[0]['id'])
        self.assertEqual(len(exported['cells']),1554)
        self.assertEqual(len(exported['observations']),2)
        self.assertTrue(any(a['kind']=='implementation' and 'domain.py' in a['payload']['files'] for a in exported['artifacts']))
        self.assertNotIn('TEST_KEY',json.dumps(exported))
        self.assertTrue(all(r['response_sha256'] for r in exported['requests'] if r['method']!='notifications/initialized'))
        self.assertFalse(any('提前偿还' in str(r['request']) or '赎回条款' in str(r['request']) for r in exported['requests']))
        client=TestClient(app)
        self.assertEqual(client.get(f'/api/trace/runs/{rid}/observations?region=shanghai&term=10').json()['total'],2)
        self.assertEqual(client.get(f'/api/trace/runs/{rid}/observations?offset=-1').status_code,422)

    def test_summary_versions_survive_replacement_and_are_exported(self):
        rid=self.execute()['runId']
        store.save_summary(rid,{'text':'first'})
        store.save_summary(rid,{'text':'second'})
        exported=lineage.export_session(lineage.sessions(rid)[0]['id'])
        self.assertEqual([r['payload']['text'] for r in exported['summaryVersions']],['first','second'])

    def test_interrupted_request_preserves_received_fragments(self):
        rid=store.enqueue('2026-09-10')['runId']
        recorder=lineage.Recorder('interruption',run_id=rid)
        request=recorder.begin('tools/call',{'params':{'name':'get_bond_basicinfo'}})
        recorder.part(request,'data: partial response')
        lineage.interrupt_run(rid)
        detail=lineage.request_detail(request)
        self.assertEqual(detail['status'],'failed')
        self.assertEqual(detail['parts'],['data: partial response'])
        self.assertIsNotNone(detail['finished_at'])

    def test_incomplete_partition_retains_raw_and_partial_observations_without_publish(self):
        self.incomplete=True
        with self.assertRaises(WindError):self.execute()
        run=store.runs()[0]
        self.assertIsNone(store.read_day('2026-09-10')['snapshot'])
        self.assertEqual(lineage.observations(run['runId'])['total'],2)
        session=lineage.sessions(run['runId'])[0]
        exported=lineage.export_session(session['id'])
        self.assertTrue(any(a['kind']=='pipeline-failure' for a in exported['artifacts']))
        self.assertGreater(len(exported['requests']),10)

    def test_retry_preserves_previous_snapshot_and_both_sessions(self):
        first=self.execute();self.null_yield=True
        with self.assertRaises(WindError):self.execute()
        self.assertEqual(store.read_day('2026-09-10')['snapshot']['publishedRunId'],first['runId'])
        self.assertEqual(len(lineage.sessions()),2)
        failed=store.runs()[0]
        rows=lineage.observations(failed['runId'])['items']
        self.assertTrue(all(r['disposition']=='excluded' for r in rows))
        self.assertTrue(all('到期收益率' in r['reason'] for r in rows))

    def test_http_error_keeps_body_and_other_tool_data(self):
        self.fail_stage=True
        with self.assertRaises(WindError):self.execute()
        session=lineage.sessions()[0]
        request=next(r for r in lineage.requests(session['id']) if r['http_status']==503)
        self.assertEqual(request['status'],'failed')
        self.assertIn('provider failed',''.join(lineage.request_detail(request['id'])['parts']))
        self.assertGreater(lineage.observations(store.runs()[0]['runId'])['total'],0)

    def test_mapping_uses_returned_term_omits_clauses_and_keeps_balance_separate_from_weight(self):
        m=Merge('2026-09-10')
        m.add(result([table(['Wind代码','特殊条款',{'name':'剩余期限_下一行权日','unit':'年'},{'name':'债券余额','unit':'亿元'}],[['233890.SH','',10,20]])]),'request')
        row,_=m.normalize('233890.SH')
        self.assertEqual(row['remainingYears'],'10')
        self.assertNotIn('earlyRepayment',row)
        self.assertNotIn('redemption',row)
        self.assertIsNone(row['issueAmountYi'])
        self.assertEqual(row['_descriptive']['outstandingBalanceYi'],'20')

    def test_future_field_groups_do_not_request_clauses_or_extra_yield_proof(self):
        self.assertEqual(len(FIELD_GROUPS),7)
        questions=' '.join(fields for _,_,fields in FIELD_GROUPS)
        for text in ['提前偿还','赎回','实际数据日期','收益率类型','价格口径']:
            self.assertNotIn(text,questions)

    def test_observed_local_bond_type_preserves_general_subtype_and_rejects_conflict(self):
        for raw,expected in [('一般债-特殊再融资一般债','general'),('专项债-再融资专项债','special'),('一般债;专项债',None),('未知',None)]:
            with self.subTest(raw=raw):
                m=Merge('2026-09-10')
                m.add(result([table(['Wind代码','地方债类型'],[['809312.IB',raw]])]),'request')
                row,sources=m.normalize('809312.IB')
                self.assertEqual(row['bondType'],expected)
                self.assertEqual(sources['bondType'][0]['value'],raw)

    def test_recorded_response_redacts_credential_and_preserves_failed_rpc(self):
        recorder=lineage.Recorder('test')
        async def go():
            handler=lambda req:httpx.Response(200,json={'id':1,'error':{'code':-1,'message':'secret TEST_KEY'}})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(WindError):
                    async with WindMCP('TEST_KEY',client=client,recorder=recorder):pass
        asyncio.run(go())
        exported=json.dumps(lineage.export_session(recorder.id))
        self.assertNotIn('TEST_KEY',exported)
        self.assertIn('[REDACTED]',exported)


if __name__=='__main__':unittest.main()
