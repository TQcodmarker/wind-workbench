import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
import httpx
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field
from . import storage as store
from .domain import RULES, REGIONS, TERMS, COHORTS, YIELD_DEFINITION
from .analysis import respond, daily_summary
from .model_config import router as model_router
from .wind_connection import probe_wind
from .credentials import read_wind_key, save_wind_key, clear_wind_key
from .wind_mcp import WindMCP, WindError
from .wind_verification import read_report, verify as verify_wind_data
from . import lineage
from . import providers
from .providers import read_available, read_available_bond, available_dates

ENDPOINT='https://mcp.wind.com.cn/vserver_bond_data/mcp/'
credential=''
source_status='未验证'

def spawn_worker():
    subprocess.Popen([sys.executable,'-m','backend.worker'],cwd=store.ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

def spawn_sync_worker(job_id):
    # A durable process and an independent lock allow the page to close while
    # full catalog collection continues. It never invokes the Wind pipeline.
    subprocess.Popen([sys.executable,'-m','backend.akshare_sync','--job',job_id],cwd=store.ROOT,
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

@asynccontextmanager
async def lifespan(app):
    store.initialize()
    if store.MODE != 'demo':
        from .akshare_provider import initialize as initialize_akshare, recalculate_stale_datasets
        initialize_akshare()
        recalculate_stale_datasets()
        from . import akshare_sync
        akshare_sync.initialize()
        for job_id in akshare_sync.pending_jobs():
            spawn_sync_worker(job_id)
    lineage.archive_existing()
    if any(r['status'] in ['queued','running'] for r in store.runs()): spawn_worker()
    # Version-bound initial summaries are independent of valuation commits.
    for target in store.ready_dates():
        day=store.read_day(target)
        rid=day['snapshot']['publishedRunId']
        try: store.save_summary(rid,daily_summary(target,{}))
        except Exception: store.save_summary(rid,{'status':'failed','message':'摘要生成失败，可重试'})
    yield

app=FastAPI(title='地方债工作台 · AKShare / Wind MCP',lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)
app.include_router(model_router)

@app.exception_handler(RequestValidationError)
async def invalid_input(request, exc):
    # Do not echo request bodies: they can contain a Wind credential.
    return JSONResponse({'detail':'输入格式有误，请检查必填字段与长度'},status_code=422)

@app.middleware('http')
async def local_only(request:Request,call_next):
    host=request.url.hostname
    if host not in ['127.0.0.1','localhost','testserver']:
        return JSONResponse({'detail':'仅允许本机访问'},status_code=403)
    if request.method not in ['GET','HEAD','OPTIONS']:
        origin=request.headers.get('origin')
        if origin and origin not in [str(request.base_url).rstrip('/'),'http://127.0.0.1:5173','http://localhost:5173']:
            return JSONResponse({'detail':'写操作来源不受信任'},status_code=403)
        if request.headers.get('sec-fetch-site')=='cross-site':
            return JSONResponse({'detail':'不允许跨站写入'},status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Cache-Control']='no-store' if request.url.path.startswith('/api') else 'no-cache'
    return response

def upper_date():
    return max(date(2026,9,10),datetime.now(timezone(timedelta(hours=8))).date()-timedelta(days=1))

def validate_date(value):
    try: parsed=date.fromisoformat(value)
    except ValueError: raise HTTPException(400,'日期格式应为 YYYY-MM-DD')
    if parsed.isoformat()!=value or not date(2026,9,1)<=parsed<=upper_date():
        raise HTTPException(400,'评估日期必须在 2026-09-01 至前一自然日之间')
    return value

@app.get('/api/bootstrap')
def bootstrap():
    selected=providers.active_provider()
    dates=providers.ready_dates(provider=selected)
    current_rules=providers.current_rules(selected)
    saved_dates=[target for target in available_dates(provider=selected) if '2026-09-01'<=target<=upper_date().isoformat()]
    return dict(mode=selected,historyStart='2026-09-01',maxDate=upper_date().isoformat(),latestDate=dates[0] if dates else None,latestSavedDate=saved_dates[0] if saved_dates else None,availableDates=saved_dates,serverDate=datetime.now(timezone(timedelta(hours=8))).date().isoformat(),timezone='Asia/Shanghai',regions=REGIONS,terms=TERMS,schedulerEnabled=False,assistantMode='deterministic',yieldDefinition=current_rules['yieldDefinition'],rulesVersion=current_rules['version'],clausePolicy=current_rules['clausePolicy'],dataAcceptance=current_rules['dataAcceptance'],dateBasis=current_rules['dateBasis'])

@app.get('/api/calendar')
def calendar(month:str='2026-09'):
    selected=providers.active_provider()
    try: current=date.fromisoformat(month+'-01')
    except ValueError: raise HTTPException(400,'月份格式应为 YYYY-MM')
    days=[]
    while current.strftime('%Y-%m')==month:
        if date(2026,9,1)<=current<=upper_date():
            day=providers.read_day(current.isoformat(),provider=selected)
            days.append(dict(date=current.isoformat(),dataState=day['dataState'],status=day['latestAttempt']['status'] if day['latestAttempt'] else None))
        current+=timedelta(days=1)
    return days

@app.get('/api/datasets/{target}')
def dataset(target:str): return providers.read_day(validate_date(target))

@app.get('/api/datasets/{target}/available')
def available_dataset(target:str):
    # Saved evidence is useful before a nationwide run is complete. This read
    # never starts collection or publishes a full-market snapshot.
    return read_available(validate_date(target), include_evidence=False)

@app.get('/api/datasets/{target}/available/summary')
def available_summary(target:str):
    return providers.read_available_summary(validate_date(target))


@app.get('/api/datasets/{target}/available/page')
def available_page(target:str,page:int=1,page_size:int=25,cohort:str='all',tier:str='all',scope:str='all',
                   region:str='',status:str='all',q:str=''):
    try:
        return providers.read_available_page(validate_date(target),page=page,page_size=page_size,
                                             cohort=cohort,tier=tier,scope=scope,region=region,status=status,q=q)
    except ValueError as exc:
        raise HTTPException(400,str(exc))


@app.get('/api/datasets/{target}/available/{code}')
def available_bond(target:str, code:str):
    bond=read_available_bond(validate_date(target), code)
    if bond is None: raise HTTPException(404, '该日期未保存此债券的数据')
    return bond

@app.get('/api/datasets/{target}/acquisition')
def acquisition_progress(target:str):
    target=validate_date(target)
    if providers.active_provider() != 'wind': return None
    paths=list((store.ROOT/'runtime').glob(f'*-acquisition-{target}.json'))
    if not paths: return None
    for path in sorted(paths, key=lambda item:item.stat().st_mtime, reverse=True):
        state=json.loads(path.read_text(encoding='utf-8'))
        if state.get('scope')=='excel_universe' and state.get('targetDate')==target:
            return {key:state.get(key) for key in (
                'scope','targetDate','status','requestedCodes','dataCalls','maxDataCalls',
                'progress','updatedAt','stopReason','coverage')}
    return None

@app.get('/api/rules')
def rules(): return providers.current_rules()

@app.get('/api/runs')
def runs(): return providers.runs()

@app.get('/api/runs/{rid}')
def run(rid:str):
    result=store.get_run(rid)
    if not result: raise HTTPException(404,'运行记录不存在')
    return result

@app.get('/api/trace/sessions')
def trace_sessions(runId:str|None=None):
    return lineage.sessions(runId)

@app.get('/api/akshare/queries/{qid}')
def akshare_query(qid:str):
    from .akshare_provider import query_detail
    result=query_detail(qid)
    if result is None: raise HTTPException(404,'AKShare 查询证据不存在')
    return result

@app.get('/api/akshare/sync')
def full_sync_status(target:str|None=None):
    from . import akshare_sync
    return akshare_sync.status(validate_date(target) if target else None)

class FullSyncInput(BaseModel):
    targetDate:str

@app.post('/api/akshare/sync',status_code=202)
def start_full_sync(body:FullSyncInput):
    from . import akshare_sync
    target=validate_date(body.targetDate)
    if store.MODE=='demo': raise HTTPException(400,'演示服务不采集真实数据，请在真实数据服务中同步')
    try: job=akshare_sync.start(target,universe='market')
    except ValueError as exc: raise HTTPException(409,str(exc))
    if job['status'] in ('queued','running'):
        spawn_sync_worker(job['jobId'])
    return job

@app.post('/api/akshare/sync/{job_id}/pause')
def pause_full_sync(job_id:str):
    from . import akshare_sync
    try: akshare_sync.get(job_id)
    except KeyError: raise HTTPException(404,'全量同步任务不存在')
    try: job=akshare_sync.pause(job_id)
    except ValueError as exc: raise HTTPException(409,str(exc))
    return job

@app.post('/api/akshare/sync/{job_id}/resume',status_code=202)
def resume_full_sync(job_id:str):
    from . import akshare_sync
    if store.MODE=='demo': raise HTTPException(400,'演示服务不采集真实数据，请在真实数据服务中同步')
    try: akshare_sync.get(job_id)
    except KeyError: raise HTTPException(404,'全量同步任务不存在')
    try: job=akshare_sync.resume(job_id)
    except ValueError as exc: raise HTTPException(409,str(exc))
    if job['status'] in ('queued','running'):
        spawn_sync_worker(job_id)
    return job

@app.get('/api/trace/sessions/{sid}/requests')
def trace_requests(sid:str):
    return lineage.requests(sid)

@app.get('/api/trace/requests/{rid}')
def trace_request(rid:str):
    result=lineage.request_detail(rid)
    if not result:raise HTTPException(404,'来源请求不存在')
    return result

@app.get('/api/trace/sessions/{sid}/export')
def trace_export(sid:str):
    result=lineage.export_session(sid)
    if not result:raise HTTPException(404,'溯源记录不存在')
    # The generated filename is independent of user-supplied path fragments.
    return JSONResponse(result,headers={'Content-Disposition':'attachment; filename="wind-lineage.json"'})

@app.get('/api/trace/runs/{rid}/observations')
def trace_observations(rid:str,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=200),
        cohort:str|None=None,region:str|None=None,term:int|None=None,scope:str|None=None):
    if not store.get_run(rid):raise HTTPException(404,'运行记录不存在')
    return lineage.observations(rid,offset,limit,cohort,region,term,scope)

class RunInput(BaseModel):
    targetDate:str
    triggerType:str='manual_retry'
    provider:str|None=None

@app.post('/api/runs',status_code=202)
def retry(body:RunInput):
    if body.triggerType!='manual_retry': raise HTTPException(400,'只允许手动重试')
    provider=body.provider or providers.active_provider()
    if provider not in ('wind','akshare','demo'):
        raise HTTPException(400,'不支持的数据源')
    try: result=store.enqueue(validate_date(body.targetDate),provider=provider)
    except ValueError as exc: raise HTTPException(409,str(exc))
    spawn_worker()
    return result

class AnalysisInput(BaseModel):
    prompt:str=Field(default='',max_length=2000)
    action:str='query'
    context:dict=Field(default_factory=dict)
    baseDate:str|None=None

@app.post('/api/analysis')
def analysis(body:AnalysisInput):
    selected=providers.active_provider()
    if body.action not in ['query','compare','summary']: raise HTTPException(400,'不支持的分析操作')
    if body.context.get('date'): validate_date(body.context['date'])
    if body.baseDate: validate_date(body.baseDate)
    context=body.context
    if context.get('cohort',COHORTS[0]) not in COHORTS or str(context.get('tier','all')) not in ['all','1','2','3'] or context.get('scope','all') not in ['all','overall','general','special'] or context.get('term') not in [None,'',*TERMS]:
        raise HTTPException(400,'分析范围无效')
    if selected == 'akshare':
        result=respond(body.model_dump(),reader=lambda target:providers.analysis_day(target,provider=selected),dates=providers.available_dates(provider=selected))
    else:
        result=respond(body.model_dump())
    if result.get('targetDate'): validate_date(result['targetDate'])
    if result.get('baseDate'): validate_date(result['baseDate'])
    return result

@app.get('/api/summaries/{target}')
def summary(target:str):
    selected=providers.active_provider()
    if selected == 'akshare':
        target=validate_date(target)
        return daily_summary(target,{},reader=lambda day:providers.analysis_day(day,provider=selected),dates=providers.available_dates(provider=selected))
    day=store.read_day(validate_date(target))
    if not day['snapshot']: return dict(status='unavailable',message='该日期没有可用于摘要的成功结果')
    with store.connection() as db:
        row=db.execute('SELECT payload FROM summaries WHERE run_id=?',(day['snapshot']['publishedRunId'],)).fetchone()
    if not row:return dict(status='pending',message='摘要尚未生成，可手动生成')
    payload=json.loads(row['payload'])
    payload.setdefault('yieldDefinition',day['snapshot']['yieldDefinition'])
    return payload

def wind_key():
    return read_wind_key() or credential

@app.get('/api/source')
def source():
    from .akshare_provider import status as akshare_status
    catalog = store.DB.parent / 'wind-tools.json'
    report = read_report()
    verification_is_current=bool(report and report.get('yieldDefinition')==YIELD_DEFINITION and report.get('rulesVersion')==RULES['version'])
    connection_status = '握手成功' if source_status == '未验证' and report and report.get('connectionVerified') else source_status
    pipeline=next((r for r in store.runs() if r.get('source')=='wind' and r.get('traceSessionId')),None)
    pipeline_is_current=bool(pipeline and pipeline.get('rulesVersion')==RULES['version'])
    if pipeline_is_current:
        data_status='取数中' if pipeline['status'] in ['queued','running'] else ('已完成 Excel 清单取数' if pipeline.get('outcome')=='excel_universe' else '已更新已有个券样本' if pipeline.get('outcome')=='saved_sample' else '已发布真实行情') if pipeline['status']=='succeeded' else '取数失败，原始数据已保存'
    elif verification_is_current:
        data_status='核验未通过' if report.get('status')=='blocked' else '已完成字段核验'
    else:
        data_status='当前取数规则待核验'
    return dict(activeProvider=providers.active_provider(),providers=providers.PROVIDERS,akshare=akshare_status(),endpoint=ENDPOINT,configured=bool(wind_key()),connectionStatus=connection_status,yieldDefinition=YIELD_DEFINITION,
        dataStatus=data_status,pipeline=pipeline,pipelineIsCurrent=pipeline_is_current,storage='Windows DPAPI 加密 · 本机配置数据库',mode=store.MODE,verification=report,verificationIsCurrent=verification_is_current,
        rulesVersion=RULES['version'],clausePolicy=RULES['clausePolicy'],dataAcceptance=RULES['dataAcceptance'],dateBasis=RULES['dateBasis'],
        toolCount=len(json.loads(catalog.read_text(encoding='utf-8'))['tools']) if catalog.exists() else 0)

class ProviderInput(BaseModel):
    provider:str

@app.post('/api/source/provider')
def select_source(body:ProviderInput):
    try: selected=providers.select_provider(body.provider)
    except ValueError as exc: raise HTTPException(400,str(exc))
    return {'activeProvider':selected,'message':'数据源已切换；已有任务继续使用创建时的来源'}

class SourceInput(BaseModel):
    key:str=Field(max_length=4096)

@app.post('/api/source')
def save_source(body:SourceInput):
    global credential,source_status
    key=body.key.strip()
    if not key or key.lower().startswith('bearer ') or any(c.isspace() for c in key):
        raise HTTPException(400,'请输入不含空格及 Bearer 前缀的 Wind Key')
    try: save_wind_key(key)
    except RuntimeError as exc: raise HTTPException(400,str(exc))
    credential=key;source_status='未验证'
    with store.connection() as db:
        db.execute("DELETE FROM metadata WHERE key='wind_verification'")
    (store.DB.parent / 'wind-tools.json').unlink(missing_ok=True)
    return source()

@app.post('/api/source/clear')
def clear_source():
    global credential,source_status
    clear_wind_key()
    credential='';source_status='未验证'
    with store.connection() as db:
        db.execute("DELETE FROM metadata WHERE key='wind_verification'")
    (store.DB.parent / 'wind-tools.json').unlink(missing_ok=True)
    return source()

@app.post('/api/source/test')
async def test_source():
    global source_status
    key=wind_key()
    if not key: raise HTTPException(400,'请先保存 Wind Key')
    source_status = await probe_wind(key)
    return source()

@app.post('/api/source/discover')
async def discover_source():
    global source_status
    try:
        async with WindMCP(wind_key()) as mcp:
            tools = await mcp.list_tools()
            catalog = {'discoveredAt':store.now(),'protocolVersion':mcp.version,'tools':tools}
        (store.DB.parent / 'wind-tools.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf-8')
        source_status='握手成功'
        return catalog
    except WindError as exc:
        raise HTTPException(400,str(exc))

@app.post('/api/source/verify')
async def verify_source(body:RunInput):
    await verify_wind_data(validate_date(body.targetDate))
    return source()

@app.get('/{path:path}')
def frontend(path:str):
    root=(store.ROOT/'dist').resolve()
    target=(root/path).resolve()
    if target.is_relative_to(root) and target.is_file(): return FileResponse(target)
    if path.startswith('api/'): raise HTTPException(404,'接口不存在')
    if (root/'index.html').exists(): return FileResponse(root/'index.html')
    raise HTTPException(404,'请先执行 npm run build，或使用 Vite 开发服务')
