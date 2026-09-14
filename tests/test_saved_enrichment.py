import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from backend import storage as store
from backend.api import app
from backend.lineage import Recorder
from scripts.record_saved_enrichment import record


class SavedEnrichmentTests(unittest.TestCase):
    def test_completion_is_atomic_idempotent_and_not_a_national_publication(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(store,'MODE','wind'), patch.object(store,'DB',Path(folder)/'test.sqlite3'):
            store.initialize(seed=False)
            rec=Recorder('补取测试','2026-09-11')
            rid=rec.begin('tools/call',{'params':{'name':'get_bond_market_data'}})
            fields={'Wind代码':'809336.IB','主证券代码':'809336.IB','债务主体名称':'河北省人民政府','所属概念板块':'地方政府一般债','发行起始日期':'2026-04-20','发行总额':88.2,'收盘价收益率':1.7691,'实际剩余期限':9.6082}
            table={'columns':[{'name':name} for name in fields],'rows':[list(fields.values())]}
            rec.finish(rid,{'result':{'content':[{'type':'text','text':json.dumps({'data':{'data':[table]}})}]}})
            path=Path(folder)/'plan.json'
            path.with_name('plan-result.json').write_text(json.dumps({'sessionId':rec.id,'targetDate':'2026-09-11','status':'finished','startedAt':store.now(),'finishedAt':store.now()}),encoding='utf-8')
            with patch('backend.api.spawn_worker',side_effect=AssertionError('No collection')):
                result=record([path])
                self.assertEqual(record([path])['runId'],result['runId'])
                self.assertEqual(result['outcome'],'saved_sample')
                self.assertEqual(result['counts']['eligible'],1)
                self.assertIsNone(store.read_day('2026-09-11')['snapshot'])
                self.assertEqual(store.ready_dates(),[])
                with patch('backend.api.wind_key',return_value=''):
                    source=TestClient(app).get('/api/source').json()
                self.assertEqual(source['dataStatus'],'已更新已有个券样本')
            with store.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM job_runs').fetchone()[0],1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM job_runs WHERE status IN ('queued','running')").fetchone()[0],0)
                self.assertEqual(db.execute('SELECT run_id FROM source_sessions WHERE id=?',(rec.id,)).fetchone()[0],result['runId'])


if __name__=='__main__':
    unittest.main()
