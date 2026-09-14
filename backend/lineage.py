"""Durable source evidence, normalized rows, and calculation decisions."""
import hashlib
import json
import uuid
from . import storage as store


def dump(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),default=str)


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS source_sessions (
      id TEXT PRIMARY KEY, run_id TEXT, target_date TEXT, purpose TEXT NOT NULL,
      created_at TEXT NOT NULL, config TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS source_requests (
      id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES source_sessions(id),
      stage TEXT NOT NULL, method TEXT NOT NULL, tool TEXT, request TEXT NOT NULL,
      started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
      http_status INTEGER, response TEXT, error TEXT, response_sha256 TEXT);
    CREATE INDEX IF NOT EXISTS source_request_session ON source_requests(session_id,started_at);
    CREATE TABLE IF NOT EXISTS source_response_parts (
      request_id TEXT NOT NULL REFERENCES source_requests(id), seq INTEGER NOT NULL,
      content TEXT NOT NULL, PRIMARY KEY(request_id,seq));
    CREATE TABLE IF NOT EXISTS source_artifacts (
      id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES source_sessions(id),
      kind TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS bond_observations (
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES job_runs(id),
      code TEXT NOT NULL, bond_id TEXT, region_id TEXT, payload TEXT NOT NULL,
      field_sources TEXT NOT NULL, mapping_version TEXT NOT NULL,
      disposition TEXT NOT NULL, reason TEXT NOT NULL, cohort TEXT, term INTEGER, scope TEXT,
      duplicate_of TEXT);
    CREATE INDEX IF NOT EXISTS observations_run ON bond_observations(run_id);
    CREATE INDEX IF NOT EXISTS observations_cell ON bond_observations(run_id,cohort,region_id,term,scope);
    CREATE TABLE IF NOT EXISTS source_imports (path TEXT NOT NULL, sha256 TEXT NOT NULL, artifact_id TEXT NOT NULL, PRIMARY KEY(path,sha256));
    CREATE TABLE IF NOT EXISTS summary_versions (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
    ''')


class Recorder:
    def __init__(self, purpose, target=None, run_id=None, config=None):
        self.id = str(uuid.uuid4())
        self.stage = '连接'
        with store.connection() as db:
            initialize(db)
            db.execute('INSERT INTO source_sessions VALUES (?,?,?,?,?,?)',
                (self.id,run_id,target,purpose,store.now(),dump(config or {})))

    def begin(self, method, body):
        rid = str(uuid.uuid4())
        with store.connection() as db:
            db.execute('INSERT INTO source_requests (id,session_id,stage,method,tool,request,started_at,status) VALUES (?,?,?,?,?,?,?,?)',
                (rid,self.id,self.stage,method,body.get('params',{}).get('name'),dump(body),store.now(),'running'))
        return rid

    def part(self, rid, content):
        with store.connection() as db:
            seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM source_response_parts WHERE request_id=?',(rid,)).fetchone()[0]
            db.execute('INSERT INTO source_response_parts VALUES (?,?,?)',(rid,seq,content))

    def finish(self, rid, response, error=None, http_status=None):
        raw = dump(response) if response is not None else None
        with store.connection() as db:
            db.execute('UPDATE source_requests SET finished_at=?,status=?,http_status=?,response=?,error=?,response_sha256=? WHERE id=?',
                (store.now(),'failed' if error else 'succeeded',http_status,raw,error,
                 hashlib.sha256(raw.encode()).hexdigest() if raw is not None else None,rid))

    def artifact(self, kind, payload):
        raw=dump(payload); aid=str(uuid.uuid4())
        with store.connection() as db:
            db.execute('INSERT INTO source_artifacts VALUES (?,?,?,?,?,?)',
                (aid,self.id,kind,store.now(),raw,hashlib.sha256(raw.encode()).hexdigest()))
        return aid


def save_observation(run_id, row, field_sources, mapping_version, decision):
    oid=str(uuid.uuid4())
    with store.connection() as db:
        db.execute('INSERT INTO bond_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (oid,run_id,row.get('code',''),row.get('bondId'),row.get('regionId'),dump(row),dump(field_sources),mapping_version,
             decision['disposition'],decision['reason'],decision.get('cohort'),decision.get('term'),row.get('bondType'),decision.get('duplicateOf')))
    return oid


def sessions(run_id=None):
    with store.connection() as db:
        rows=db.execute('SELECT s.*, (SELECT COUNT(*) FROM source_requests r WHERE r.session_id=s.id) AS request_count FROM source_sessions s '+
            ('WHERE run_id=? ' if run_id else '')+'ORDER BY created_at DESC', (run_id,) if run_id else ()).fetchall()
        return [dict(row,config=json.loads(row['config'])) for row in rows]


def update_decision(oid,decision):
    with store.connection() as db:
        db.execute('UPDATE bond_observations SET disposition=?,reason=?,cohort=?,term=?,duplicate_of=? WHERE id=?',
             (decision['disposition'],decision['reason'],decision.get('cohort'),decision.get('term'),decision.get('duplicateOf'),oid))


def interrupt_run(run_id):
    """Close abandoned requests without losing response fragments already committed."""
    with store.connection() as db:
        db.execute("UPDATE source_requests SET status='failed',finished_at=?,error=? WHERE status='running' AND session_id IN (SELECT id FROM source_sessions WHERE run_id=?)",
            (store.now(),'执行进程中断；已接收的响应片段保留',run_id))


def requests(session_id):
    with store.connection() as db:
        return [dict(r) for r in db.execute('SELECT id,stage,method,tool,started_at,finished_at,status,http_status,error,response_sha256 FROM source_requests WHERE session_id=? ORDER BY rowid',(session_id,))]


def request_detail(rid):
    with store.connection() as db:
        row=db.execute('SELECT * FROM source_requests WHERE id=?',(rid,)).fetchone()
        if not row:return None
        return dict(row,request=json.loads(row['request']),response=json.loads(row['response']) if row['response'] else None,
            parts=[r['content'] for r in db.execute('SELECT content FROM source_response_parts WHERE request_id=? ORDER BY seq',(rid,))])


def observations(run_id, offset=0, limit=100, cohort=None, region=None, term=None, scope=None):
    where=['run_id=?']; args=[run_id]
    for key,value in [('cohort',cohort),('region_id',region),('term',term),('scope',scope if scope not in ('all',None) else None)]:
        if value is not None:where.append(key+'=?');args.append(value)
    sql=' AND '.join(where)
    with store.connection() as db:
        total=db.execute('SELECT COUNT(*) FROM bond_observations WHERE '+sql,args).fetchone()[0]
        rows=db.execute('SELECT * FROM bond_observations WHERE '+sql+' ORDER BY rowid LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
    return {'total':total,'offset':offset,'items':[dict(r,payload=json.loads(r['payload']),field_sources=json.loads(r['field_sources'])) for r in rows]}


def export_session(sid):
    with store.connection() as db:
        session=db.execute('SELECT * FROM source_sessions WHERE id=?',(sid,)).fetchone()
        if not session:return None
        artifacts=[dict(r,payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM source_artifacts WHERE session_id=? ORDER BY rowid',(sid,))]
    result={'session':dict(session,config=json.loads(session['config'])), 'requests':[request_detail(r['id']) for r in requests(sid)],'artifacts':artifacts}
    if session['run_id']:
        result['run']=store.get_run(session['run_id'])
        # Export all observations, not just the first page shown in the UI.
        result['observations']=observations(session['run_id'],limit=2147483647)['items']
        with store.connection() as db:
            result['cells']=[json.loads(r['payload']) for r in db.execute('SELECT payload FROM aggregate_results WHERE run_id=?',(session['run_id'],))]
            result['summaryVersions']=[dict(r,payload=json.loads(r['payload'])) for r in db.execute('SELECT * FROM summary_versions WHERE run_id=? ORDER BY rowid',(session['run_id'],))]
    return result


def archive_existing():
    """Preserve legacy evidence as imported artifacts, without inventing request metadata."""
    paths=[*store.DB.parent.glob('wind-probe-*.json'),store.DB.parent/'wind-tools.json',store.DB.parent/'wind-verification-raw.json']
    recorder=None
    for path in paths:
        if not path.is_file():continue
        raw=path.read_text(encoding='utf-8');digest=hashlib.sha256(raw.encode()).hexdigest()
        with store.connection() as db:
            exists=db.execute('SELECT 1 FROM source_imports WHERE path=? AND sha256=?',(path.name,digest)).fetchone()
        if exists:continue
        if recorder is None:recorder=Recorder('旧版证据归档（原请求时间未完整记录）')
        aid=recorder.artifact('legacy-import',{'filename':path.name,'originalText':raw,'sourceHash':digest})
        with store.connection() as db:
            db.execute('INSERT OR IGNORE INTO source_imports VALUES (?,?,?)',(path.name,digest,aid))
