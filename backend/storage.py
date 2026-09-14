import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .domain import REGIONS, RULES, YIELD_DEFINITION, historical_rules, DemoReader, calculate

ROOT = Path(__file__).resolve().parent.parent
MODE = os.environ.get('WIND_DATA_MODE', 'wind')
if MODE not in ('wind', 'demo'):
    raise RuntimeError('WIND_DATA_MODE 必须是 wind 或 demo')
DB = Path(os.environ.get('WIND_DB', str(ROOT/'runtime'/f'wind-{MODE}.sqlite3')))

def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')

@contextmanager
def connection():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def initialize(seed=True):
    with connection() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS job_runs (id TEXT PRIMARY KEY,target_date TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS active_date ON job_runs(target_date) WHERE status IN ('queued','running');
        CREATE TABLE IF NOT EXISTS valuation_dates (date TEXT PRIMARY KEY,state TEXT NOT NULL DEFAULT 'pending',published_id TEXT REFERENCES job_runs(id),latest_id TEXT REFERENCES job_runs(id));
        CREATE TABLE IF NOT EXISTS snapshots (run_id TEXT PRIMARY KEY REFERENCES job_runs(id),published_at TEXT NOT NULL,rules_version TEXT NOT NULL,mapping_version TEXT NOT NULL,regions TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS aggregate_results (run_id TEXT REFERENCES snapshots(run_id),cohort TEXT,region_id TEXT,term INTEGER,scope TEXT,payload TEXT NOT NULL,PRIMARY KEY(run_id,cohort,region_id,term,scope));
        CREATE TABLE IF NOT EXISTS summaries (run_id TEXT PRIMARY KEY REFERENCES snapshots(run_id),payload TEXT NOT NULL);
        ''')
        from .lineage import initialize as initialize_lineage
        initialize_lineage(db)
        seeded = db.execute("SELECT value FROM metadata WHERE key='seeded'").fetchone()
        origin = db.execute("SELECT value FROM metadata WHERE key='data_mode'").fetchone()
        existing = db.execute('SELECT COUNT(*) FROM job_runs').fetchone()[0]
        if (origin and origin['value'] != MODE) or (not origin and existing and MODE != 'demo'):
            raise RuntimeError('数据库来源与运行模式不匹配；请使用独立的真实行情库，不能复用演示库')
        db.execute("INSERT OR IGNORE INTO metadata VALUES ('data_mode',?)", (MODE,))
    if seed and MODE == 'demo' and not seeded:
        for n in range(1,11):
            target=f'2026-09-{n:02}'
            if n==3: continue
            run=enqueue(target,'demo_seed')
            if n==4:
                fail(run['runId'],'演示首次取数失败，可重试该日期')
            else:
                raw=DemoReader().fetch(target)
                if raw is None: publish_no_data(run['runId'])
                else: publish(run['runId'],*calculate(raw,target))
            if n==8:
                failed=enqueue(target,'demo_seed')
                fail(failed['runId'],'演示连接超时，保留上次成功结果')
        with connection() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('seeded','1')")

def enqueue(target, trigger='manual_retry', provider=None):
    # Keep the internal legacy default for Wind acquisition scripts. API and
    # scheduler callers pass the selected source explicitly.
    provider = provider or MODE
    if provider not in ('akshare', MODE) or (MODE == 'demo' and provider != 'demo'):
        raise ValueError('任务数据源与数据库运行模式不兼容')
    if provider == 'akshare':
        from .akshare_provider import RULES as run_rules
    else:
        run_rules = RULES
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute("SELECT payload FROM job_runs WHERE target_date=? AND status IN ('queued','running')",(target,)).fetchone()
        if existing:
            active = json.loads(existing['payload'])
            if active.get('source', 'demo') != provider:
                raise ValueError('该日期的另一数据源任务仍在执行，请完成后再更新；已排队任务保留原来源')
            return active
        rid=str(uuid.uuid4())
        run=dict(runId=rid,targetDate=target,triggerType=trigger,status='queued',outcome=None,phase='等待执行',createdAt=now(),startedAt=None,finishedAt=None,message='任务已进入持久化队列',counts=None,rulesVersion=run_rules['version'],mappingVersion=run_rules['mappingVersion'],source=provider,yieldDefinition=run_rules['yieldDefinition'],rules=run_rules)
        db.execute('INSERT INTO job_runs VALUES (?,?,?,?)',(rid,target,'queued',json.dumps(run,ensure_ascii=False)))
        if provider != 'akshare':
            db.execute('INSERT INTO valuation_dates(date,latest_id) VALUES (?,?) ON CONFLICT(date) DO UPDATE SET latest_id=excluded.latest_id',(target,rid))
        return run

def get_run(rid):
    with connection() as db:
        row=db.execute('SELECT payload FROM job_runs WHERE id=?',(rid,)).fetchone()
        return json.loads(row['payload']) if row else None

def write_run(db,run):
    db.execute('UPDATE job_runs SET status=?,payload=? WHERE id=?',(run['status'],json.dumps(run,ensure_ascii=False),run['runId']))

def update_run(rid,**changes):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        run=json.loads(db.execute('SELECT payload FROM job_runs WHERE id=?',(rid,)).fetchone()['payload'])
        run.update(changes)
        write_run(db,run)
        return run

def fail(rid,message):
    update_run(rid,status='failed',phase='执行失败',finishedAt=now(),message=message)

def ensure_current_run(run):
    if run.get('source') == 'akshare':
        from .akshare_provider import RULES as expected
    else:
        expected = RULES
    if run.get('rulesVersion')!=expected['version'] or run.get('yieldDefinition')!=expected['yieldDefinition'] or run.get('rules')!=expected:
        raise ValueError('任务使用旧计算规则，请按当前规则重新创建任务')

def validate_cells(cells):
    from .domain import COHORTS, TERMS, SCOPES, number
    expected={(c,r['id'],t,s) for c in COHORTS for r in REGIONS for t in TERMS for s in SCOPES}
    actual={(c['cohort'],c['regionId'],c['termYears'],c['bondScope']) for c in cells}
    if len(cells)!=1554 or actual!=expected: raise ValueError('Incomplete daily snapshot')
    for cell in cells:
        if cell.get('yieldMetric')!=YIELD_DEFINITION['metric'] or cell.get('yieldPriceBasis')!=YIELD_DEFINITION['priceBasis']:
            raise ValueError('汇总收益率口径与当前规则不匹配')
        if cell['sampleCount']==0:
            if cell['yieldPct'] is not None or number(cell['issueAmountSumYi'])!=0 or number(cell['weightedYieldSum'])!=0:
                raise ValueError('Invalid empty group')
        elif cell['sampleCount']<0 or cell['yieldPct'] is None or number(cell['issueAmountSumYi'])<=0:
            raise ValueError('Invalid aggregate')

def publish(rid,cells,counts,provenance=None):
    validate_cells(cells)
    if MODE == 'wind' and (not provenance or provenance.get('source') != 'wind' or provenance.get('complete') is not True):
        raise ValueError('真实行情必须具备完整取数来源记录')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        run=json.loads(db.execute('SELECT payload FROM job_runs WHERE id=?',(rid,)).fetchone()['payload'])
        ensure_current_run(run)
        if run.get('source', 'demo') != MODE:
            raise ValueError('任务来源与当前模式不匹配')
        if provenance:
            if provenance.get('evaluationDate') != run['targetDate']:
                raise ValueError('取数来源日期与任务日期不匹配')
            run['provenance'] = provenance
        stamp=now()
        db.execute('INSERT INTO snapshots VALUES (?,?,?,?,?)',(rid,stamp,RULES['version'],RULES['mappingVersion'],json.dumps(REGIONS,ensure_ascii=False)))
        db.executemany('INSERT INTO aggregate_results VALUES (?,?,?,?,?,?)',[(rid,c['cohort'],c['regionId'],c['termYears'],c['bondScope'],json.dumps(c)) for c in cells])
        run.update(status='succeeded',outcome='data',phase='已完成',finishedAt=stamp,counts=counts,message='已保存完整汇总' if counts['eligible'] else '已完成计算，无符合口径的债券')
        write_run(db,run)
        db.execute("UPDATE valuation_dates SET state='ready',published_id=? WHERE date=?",(rid,run['targetDate']))

def publish_no_data(rid,provenance=None):
    if MODE == 'wind' and (not provenance or provenance.get('source') != 'wind' or provenance.get('complete') is not True):
        raise ValueError('无行情状态必须由完整 Wind 取数结果确认')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        run=json.loads(db.execute('SELECT payload FROM job_runs WHERE id=?',(rid,)).fetchone()['payload'])
        ensure_current_run(run)
        if run.get('source', 'demo') != MODE:
            raise ValueError('任务来源与当前模式不匹配')
        if provenance:
            if provenance.get('evaluationDate') != run['targetDate']:
                raise ValueError('取数来源日期与任务日期不匹配')
            run['provenance'] = provenance
        run.update(status='succeeded',outcome='no_data',phase='已完成',finishedAt=now(),message='数据源确认当日无独立收益率数据，未填充前日数据')
        write_run(db,run)
        db.execute("UPDATE valuation_dates SET state=CASE WHEN published_id IS NULL THEN 'no_data' ELSE 'ready' END WHERE date=?",(run['targetDate'],))

def read_day(target):
    with connection() as db:
        db.execute('BEGIN')
        day=db.execute('SELECT * FROM valuation_dates WHERE date=?',(target,)).fetchone()
        result=dict(evaluationDate=target,source=MODE,yieldDefinition=YIELD_DEFINITION,dataState=day['state'] if day else 'pending',snapshot=None,latestAttempt=None)
        if day and day['latest_id']:
            result['latestAttempt']=json.loads(db.execute('SELECT payload FROM job_runs WHERE id=?',(day['latest_id'],)).fetchone()['payload'])
        if day and day['published_id']:
            snap=db.execute('SELECT * FROM snapshots WHERE run_id=?',(day['published_id'],)).fetchone()
            cells=[json.loads(row['payload']) for row in db.execute('SELECT payload FROM aggregate_results WHERE run_id=? ORDER BY rowid',(day['published_id'],))]
            run=json.loads(db.execute('SELECT payload FROM job_runs WHERE id=?',(day['published_id'],)).fetchone()['payload'])
            saved_rules=historical_rules(run,snap['rules_version'])
            result['yieldDefinition']=saved_rules['yieldDefinition']
            result['snapshot']=dict(source=run.get('source','demo'),provenance=run.get('provenance'),publishedRunId=day['published_id'],publishedAt=snap['published_at'],rulesVersion=snap['rules_version'],mappingVersion=snap['mapping_version'],rules=saved_rules,yieldDefinition=saved_rules['yieldDefinition'],regions=json.loads(snap['regions']),cells=cells,yieldUnit='percent',issueAmountUnit='yi_cny')
        return result

def runs():
    with connection() as db:
        return [json.loads(r['payload']) for r in db.execute('SELECT payload FROM job_runs ORDER BY rowid DESC LIMIT 300')]

def ready_dates():
    with connection() as db:
        return [r['date'] for r in db.execute("SELECT date FROM valuation_dates WHERE state='ready' ORDER BY date DESC")]

def save_summary(rid,payload):
    with connection() as db:
        db.execute('INSERT INTO summary_versions VALUES (?,?,?,?)',(str(uuid.uuid4()),rid,now(),json.dumps(payload,ensure_ascii=False)))
        db.execute('INSERT OR REPLACE INTO summaries VALUES (?,?)',(rid,json.dumps(payload,ensure_ascii=False)))
