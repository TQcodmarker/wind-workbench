import json
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend.domain import calculate, DemoReader, COHORTS, YIELD_DEFINITION, LEGACY_YIELD_DEFINITION, RULES, PREVIOUS_RULES
from backend import storage as store
from backend.analysis import query, daily_summary, respond, diff
from backend.api import app

def bond(**changes):
    return dict(dict(bondId='A',code='A.IB',regionId='shanghai',bondType='general',issueDate='2025-08-07',valuationDate='2026-09-10',yieldDate='2026-09-10',yieldMetric='ytm',yieldPriceBasis='close',valueStatus='valid',yieldPct='1.75',issueAmountYi='10',remainingYears='10'),**changes)

def cell(cells,region='shanghai',scope='all',cohort=COHORTS[0],term=10):
    return next(c for c in cells if (c['regionId'],c['bondScope'],c['cohort'],c['termYears'])==(region,scope,cohort,term))

class CalculationTests(unittest.TestCase):
    def test_issue_weighting_not_mean_of_means(self):
        cells,counts=calculate([bond(),bond(bondId='B',code='B',bondType='special',yieldPct='2.50',issueAmountYi='30')],'2026-09-10')
        self.assertEqual(len(cells),1554)
        self.assertEqual(cell(cells)['yieldPct'],'2.31250000')
        self.assertEqual(cell(cells)['sampleCount'],2)
        self.assertEqual(counts['eligible'],2)

    def test_rounding_boundaries_and_cutoff(self):
        rows=[bond(bondId=str(i),code=str(i),remainingYears=term,issueDate='2025-08-08') for i,term in enumerate(['2.5','3.4999','3.5','10.4','10.5','11','24.9','29.5','30.5'])]
        cells,counts=calculate(rows,'2026-09-10')
        self.assertEqual(counts['eligible'],4)
        self.assertEqual(cell(cells,cohort=COHORTS[1],term=3)['sampleCount'],2)
        self.assertEqual(cell(cells,cohort=COHORTS[0])['sampleCount'],0)

    def test_zero_missing_errors_dates_and_identity(self):
        edits=[{'yieldPct':'0'},{'yieldPct':None},{'yieldPct':''},{'yieldPct':'NaN'},{'yieldPct':'Infinity'},{'issueAmountYi':'0'},{'issueAmountYi':'-1'},{'valuationDate':'2026-09-09'},{'valueStatus':'error'}]
        rows=[bond(bondId=str(i),code=str(i),**v) for i,v in enumerate(edits)]
        rows.append(dict(rows[0],code='OTHER-LISTING'))
        cells,counts=calculate(rows,'2026-09-10')
        self.assertEqual(counts['eligible'],1)
        self.assertEqual(counts['duplicates'],1)
        self.assertEqual(cell(cells)['yieldPct'],'0E-8')
        self.assertIsNone(cell(cells,region='beijing')['yieldPct'])
        with self.assertRaises(ValueError): calculate([bond(),bond(yieldPct='2')],'2026-09-10')

    def test_clause_fields_are_not_required_or_used_for_filtering(self):
        rows=[bond(), bond(bondId='B',code='B',earlyRepayment=None,redemption=None),
              bond(bondId='C',code='C',earlyRepayment=True,redemption=True)]
        cells,counts=calculate(rows,'2026-09-10')
        self.assertEqual(counts['eligible'],3)
        self.assertEqual(counts['missing'],0)
        self.assertNotIn('clauses',counts)
        self.assertEqual(cell(cells)['sampleCount'],3)
        self.assertEqual(cell(cells)['yieldPct'],'1.75000000')

    def test_ignored_legacy_clauses_do_not_create_duplicate_conflicts(self):
        cells,counts=calculate([bond(earlyRepayment=True),bond(code='OTHER',redemption=None)],'2026-09-10')
        self.assertEqual(counts['eligible'],1)
        self.assertEqual(counts['duplicates'],1)

    def test_xpcc_is_independent(self):
        cells,_=calculate([bond(regionId='xpcc'),bond(bondId='B',code='B',regionId='xinjiang')],'2026-09-10')
        self.assertEqual(cell(cells,'xpcc')['sampleCount'],1)
        self.assertEqual(cell(cells,'xinjiang')['sampleCount'],1)

    def test_yield_basis_and_actual_date_cannot_be_mixed(self):
        rows=[bond(bondId=str(i),code=str(i),**change) for i,change in enumerate([
            {}, {'yieldMetric':'chinabond_valuation'}, {'yieldPriceBasis':'last_trade'},
            {'yieldPriceBasis':None}, {'yieldDate':'2026-09-09'}])]
        cells,counts=calculate(rows,'2026-09-10')
        self.assertEqual(counts['eligible'],1)
        self.assertEqual(counts['missing'],4)
        self.assertEqual(cell(cells)['yieldPct'],'1.75000000')

class StorageAndApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.original=store.DB
        self.mode_patch=patch.object(store,'MODE','demo')
        self.mode_patch.start()
        store.DB=Path(self.temp.name)/'test.sqlite3'
        store.initialize(seed=False)
    def tearDown(self):
        store.DB=self.original
        self.mode_patch.stop()
        self.temp.cleanup()
    def publish(self,target='2026-09-10',y='1.82'):
        run=store.enqueue(target)
        cells,counts=calculate([bond(valuationDate=target,yieldDate=target,yieldPct=y)],target)
        store.publish(run['runId'],cells,counts)
        return run
    def test_atomic_publish_preserves_snapshot_on_failure_and_no_data(self):
        first=self.publish()
        failed=store.enqueue('2026-09-10')
        with self.assertRaises(ValueError): store.publish(failed['runId'],[],{})
        self.assertEqual(store.read_day('2026-09-10')['snapshot']['publishedRunId'],first['runId'])
        store.fail(failed['runId'],'test failure')
        self.assertEqual(store.read_day('2026-09-10')['dataState'],'ready')
        later=store.enqueue('2026-09-10');store.publish_no_data(later['runId'])
        day=store.read_day('2026-09-10')
        self.assertEqual(day['snapshot']['publishedRunId'],first['runId'])
        self.assertEqual(day['latestAttempt']['outcome'],'no_data')
    def test_queue_dedup_and_other_dates_retained(self):
        first=store.enqueue('2026-09-10')
        self.assertEqual(first['runId'],store.enqueue('2026-09-10')['runId'])
        store.fail(first['runId'],'test')
        self.publish('2026-09-09')
        self.publish('2026-09-10')
        self.assertEqual(len(store.ready_dates()),2)
    def test_sqlite_insert_failure_rolls_back_entire_day(self):
        first=self.publish()
        second=store.enqueue('2026-09-10')
        with store.connection() as db:
            db.execute("CREATE TRIGGER reject_publish BEFORE INSERT ON aggregate_results WHEN NEW.term=10 BEGIN SELECT RAISE(ABORT,'test disk failure'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            store.publish(second['runId'],*calculate([bond()],'2026-09-10'))
        self.assertEqual(store.read_day('2026-09-10')['snapshot']['publishedRunId'],first['runId'])
        with store.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots WHERE run_id=?',(second['runId'],)).fetchone()[0],0)
    def test_bp_zero_null_and_versions(self):
        self.publish('2026-09-09','1.75');current=self.publish()
        ctx={'cohort':COHORTS[0],'region':'上海','term':10,'scope':'general'}
        result=query('2026-09-10',ctx,'2026-09-09')
        self.assertEqual(Decimal(result['rows'][0]['changeBP']),Decimal('7'))
        self.assertEqual(result['publishedRunId'],current['runId'])
        self.assertEqual(diff('0','1.75',100),'-175.00')
        self.assertIsNone(diff(None,'0',100))
        empty=query('2026-09-10',dict(ctx,region='北京'),'2026-09-09')
        self.assertIsNone(empty['rows'][0]['changeBP'])
        self.assertEqual(empty['rows'][0]['sampleChange'],0)
    def test_missing_date_and_summary_base(self):
        self.publish('2026-09-01')
        result=daily_summary('2026-09-01',{})
        self.assertIsNone(result['baseDate'])
        self.assertEqual(len(result['rows']),1554)
        missing=query('2026-09-03',{},'2026-09-01')
        self.assertFalse(missing['rows'])
        self.assertEqual(missing['status'],'该日期尚未处理')
        self.publish('2026-09-10')
        self.assertEqual(daily_summary('2026-09-10',{})['baseDate'],'2026-09-01')
        self.assertEqual(daily_summary('2026-09-10',{'baseDate':'2026-09-02'})['baseDate'],'2026-09-02')
    def test_natural_query_filters_and_overall(self):
        raw=DemoReader().fetch('2026-09-10');run=store.enqueue('2026-09-10');store.publish(run['runId'],*calculate(raw,'2026-09-10'))
        result=respond({'prompt':'查看2026年9月10日、2025年8月8日前发行、第三档地区专项债的10年估值','context':{'date':'2026-09-10','scope':'all','term':10,'region':'上海'}})
        self.assertEqual(len(result['rows']),18)
        self.assertEqual(result['targetDate'],'2026-09-10')
        self.assertTrue(all(r['termYears']==10 and r['bondScope']=='special' for r in result['rows']))
        result=respond({'prompt':'上海整体10年估值','context':{'date':'2026-09-10','term':10}})
        self.assertEqual(len(result['rows']),1)
        self.assertEqual(result['rows'][0]['bondScope'],'all')
        self.assertEqual(respond({'prompt':'比较两天变化','context':{'date':'2026-09-10'}})['mode'],'clarify')
    def test_api_validation_origin_and_no_key_echo(self):
        client=TestClient(app)
        self.assertEqual(client.get('/api/datasets/not-date').status_code,400)
        self.assertEqual(client.get('/api/runs/unknown').status_code,404)
        self.assertEqual(client.post('/api/runs',json={'targetDate':'2026-09-10'},headers={'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(client.get('/api/bootstrap',headers={'Host':'evil.example'}).status_code,403)
        secret='DEMO_TEST_KEY_NOT_A_SECRET'
        saved=client.post('/api/source',json={'key':secret})
        self.assertEqual(saved.status_code,200)
        self.assertNotIn(secret,saved.text)
        self.assertNotIn(secret,client.get('/api/source').text)
        client.post('/api/source/clear')
        with patch('backend.api.spawn_worker'):
            response=client.post('/api/runs',json={'targetDate':'2026-09-10'})
            self.assertEqual(response.status_code,202)
    def test_worker_real_execution(self):
        from backend.worker import drain
        run=store.enqueue('2026-09-04')
        drain()
        self.assertEqual(store.get_run(run['runId'])['status'],'succeeded')
        self.assertEqual(len(store.read_day('2026-09-04')['snapshot']['cells']),1554)
    def test_summary_failure_does_not_undo_valuation(self):
        from backend.worker import drain
        run=store.enqueue('2026-09-04')
        with patch('backend.analysis.daily_summary',side_effect=RuntimeError('AI unavailable')):
            drain()
        self.assertEqual(store.get_run(run['runId'])['status'],'succeeded')
        self.assertEqual(store.read_day('2026-09-04')['dataState'],'ready')

    def make_legacy(self,run):
        with store.connection() as db:
            payload=json.loads(db.execute('SELECT payload FROM job_runs WHERE id=?',(run['runId'],)).fetchone()['payload'])
            payload.pop('yieldDefinition',None);payload.pop('rules',None)
            payload['rulesVersion']='rules-v1'
            db.execute('UPDATE job_runs SET payload=? WHERE id=?',(json.dumps(payload),run['runId']))
            db.execute("UPDATE snapshots SET rules_version='rules-v1' WHERE run_id=?",(run['runId'],))

    def test_legacy_snapshot_keeps_original_label_and_comparisons_are_blocked(self):
        old=self.publish('2026-09-09','1.75');self.make_legacy(old)
        new=self.publish()
        before=store.read_day('2026-09-09')['snapshot']
        after=store.read_day('2026-09-10')['snapshot']
        self.assertEqual(before['yieldDefinition'],LEGACY_YIELD_DEFINITION)
        self.assertEqual(store.read_day('2026-09-09')['yieldDefinition'],LEGACY_YIELD_DEFINITION)
        self.assertIn('中债估值收益率',before['rules']['formula'])
        self.assertEqual(after['yieldDefinition'],YIELD_DEFINITION)
        self.assertEqual(after['rules'],RULES)
        ctx={'region':'上海','term':10,'scope':'general'}
        comparison=query('2026-09-10',ctx,'2026-09-09')
        self.assertTrue(comparison['comparisonBlocked'])
        self.assertEqual(comparison['rows'],[])
        explicit=daily_summary('2026-09-10',{'baseDate':'2026-09-09'})
        self.assertTrue(explicit['comparisonBlocked'])
        self.assertIn('收益率口径',explicit['message'])
        auto=daily_summary('2026-09-10',{})
        self.assertIsNone(auto['baseDate'])
        self.assertEqual(len(auto['rows']),1554)
        self.assertIn('同口径',auto['message'])

    def test_old_queued_run_does_not_execute_with_new_rules(self):
        from backend.worker import drain
        queued=store.enqueue('2026-09-04')
        store.update_run(queued['runId'],rulesVersion='rules-v1',yieldDefinition=LEGACY_YIELD_DEFINITION)
        with patch('backend.worker.DemoReader.fetch') as fetch:
            drain()
            fetch.assert_not_called()
        self.assertEqual(store.get_run(queued['runId'])['status'],'failed')
        self.assertIn('旧计算规则',store.get_run(queued['runId'])['message'])

    def test_previous_ytm_snapshot_retains_its_rules_and_cannot_be_compared(self):
        old=self.publish('2026-09-09','1.75')
        with store.connection() as db:
            payload=store.get_run(old['runId'])
            payload['rulesVersion']=PREVIOUS_RULES['version']
            payload.pop('rules')
            store.write_run(db,payload)
            db.execute('UPDATE snapshots SET rules_version=? WHERE run_id=?',(PREVIOUS_RULES['version'],old['runId']))
        self.publish()
        before=store.read_day('2026-09-09')['snapshot']
        self.assertEqual(before['rules'],PREVIOUS_RULES)
        self.assertNotIn('clausePolicy',before['rules'])
        after=store.read_day('2026-09-10')['snapshot']
        self.assertEqual(after['rules']['clausePolicy'],'not_collected_or_filtered')
        self.assertTrue(query('2026-09-10',{},'2026-09-09')['comparisonBlocked'])
        self.assertIsNone(daily_summary('2026-09-10',{})['baseDate'])

    def test_previous_ytm_queue_cannot_execute_under_new_clause_policy(self):
        from backend.worker import drain
        queued=store.enqueue('2026-09-04')
        store.update_run(queued['runId'],rulesVersion=PREVIOUS_RULES['version'],rules=PREVIOUS_RULES)
        with patch('backend.worker.DemoReader.fetch') as fetch:
            drain()
            fetch.assert_not_called()
        self.assertEqual(store.get_run(queued['runId'])['status'],'failed')

    def test_api_exposes_policy_and_marks_previous_ytm_report_historical(self):
        client=TestClient(app)
        boot=client.get('/api/bootstrap').json()
        self.assertEqual(boot['rulesVersion'],RULES['version'])
        self.assertEqual(boot['dateBasis'],'query_date')
        report={'yieldDefinition':YIELD_DEFINITION,'connectionVerified':True,'rulesVersion':PREVIOUS_RULES['version']}
        with patch('backend.api.read_report',return_value=report),patch('backend.api.wind_key',return_value=''):
            source=client.get('/api/source').json()
        self.assertFalse(source['verificationIsCurrent'])
        self.assertEqual(source['clausePolicy'],'not_collected_or_filtered')
        self.assertEqual(source['dataAcceptance'],'mcp_returned_values')
        report['rulesVersion']=RULES['version']
        with patch('backend.api.read_report',return_value=report),patch('backend.api.wind_key',return_value=''):
            self.assertTrue(client.get('/api/source').json()['verificationIsCurrent'])

    def test_old_pipeline_does_not_publish_current_source_status(self):
        old=store.enqueue('2026-09-09')
        store.update_run(old['runId'],status='succeeded',source='wind',traceSessionId='old-session',
                         rulesVersion=PREVIOUS_RULES['version'],rules=PREVIOUS_RULES)
        report={'yieldDefinition':YIELD_DEFINITION,'rulesVersion':RULES['version'],
                'connectionVerified':True,'status':'blocked'}
        with patch('backend.api.read_report',return_value=report),patch('backend.api.wind_key',return_value=''):
            source=TestClient(app).get('/api/source').json()
        self.assertTrue(source['verificationIsCurrent'])
        self.assertFalse(source['pipelineIsCurrent'])
        self.assertEqual(source['dataStatus'],'核验未通过')
        self.assertEqual(source['pipeline']['runId'],old['runId'])

    def test_publication_rejects_old_metric_under_new_run(self):
        run=store.enqueue('2026-09-10')
        cells,counts=calculate([bond()],'2026-09-10')
        cells[0]['yieldMetric']='chinabond_valuation'
        with self.assertRaisesRegex(ValueError,'收益率口径'):
            store.publish(run['runId'],cells,counts)
        self.assertIsNone(store.read_day('2026-09-10')['snapshot'])

if __name__=='__main__': unittest.main()
