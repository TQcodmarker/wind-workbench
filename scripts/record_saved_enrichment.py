"""Record completed saved-universe acquisition without queuing a national run."""
import json
import uuid
from pathlib import Path

from backend import storage as store
from backend.available_data import read_available
from backend.domain import RULES, YIELD_DEFINITION
from backend.lineage import requests


def record(plan_paths):
    reports = [json.loads(path.with_name(path.stem+'-result.json').read_text(encoding='utf-8')) for path in plan_paths]
    target = reports[0]['targetDate']
    if any(r['targetDate'] != target or r['status']=='running' for r in reports):
        raise ValueError('补取报告日期不一致或尚未完成')
    sessions = [r['sessionId'] for r in reports]
    data_calls = sum(sum(req['method']=='tools/call' for req in requests(sid)) for sid in sessions)
    data = read_available(target)
    count = data['counts']
    if count['incomplete'] or count['conflicted']:
        raise ValueError('本轮仍存在缺失或冲突，不能记录为补齐完成')
    start = min(r['startedAt'] for r in reports)
    end = max(r['finishedAt'] for r in reports)
    marker = 'saved-enrichment:'+sessions[-1]
    run_id = str(uuid.uuid4())
    message = f"已补齐已有 {count['bonds']} 只债券的数据，{count['eligible']} 只纳入固定期限汇总；本轮 {data_calls} 次数据查询。未发布全国完整快照。"
    run = dict(runId=run_id,targetDate=target,triggerType='manual_retry',status='succeeded',outcome='saved_sample',
               phase='已有债券补取完成',createdAt=start,startedAt=start,finishedAt=end,message=message,
               counts=dict(source=count['bonds'],eligible=count['eligible'],missing=0,terms=count['excluded']),
               rulesVersion=RULES['version'],mappingVersion=data['mappingVersion'],source=store.MODE,
               yieldDefinition=YIELD_DEFINITION,rules=RULES,traceSessionId=sessions[-1],traceSessionIds=sessions,
               scope='saved_sample',dataCalls=data_calls,publishedSnapshot=False)
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT value FROM metadata WHERE key=?',(marker,)).fetchone()
        if previous:
            return json.loads(db.execute('SELECT payload FROM job_runs WHERE id=?',(previous['value'],)).fetchone()[0])
        for sid in sessions:
            row=db.execute('SELECT target_date,run_id FROM source_sessions WHERE id=?',(sid,)).fetchone()
            if not row or row['target_date']!=target or row['run_id']:
                raise ValueError('来源会话不属于当前未关联的补取计划')
        db.execute('INSERT INTO job_runs VALUES (?,?,?,?)',(run_id,target,'succeeded',json.dumps(run,ensure_ascii=False)))
        db.execute('INSERT INTO valuation_dates(date,latest_id) VALUES (?,?) ON CONFLICT(date) DO UPDATE SET latest_id=excluded.latest_id',(target,run_id))
        db.executemany('UPDATE source_sessions SET run_id=? WHERE id=?',[(run_id,sid) for sid in sessions])
        db.execute('INSERT INTO metadata VALUES (?,?)',(marker,run_id))
    return run


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    result=record([Path(value) for value in sys.argv[1:]])
    print(json.dumps(result,ensure_ascii=False,indent=2))
