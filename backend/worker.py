"""Durable queue runner. A file lock serializes all independent workers."""
import argparse
import os
import time
from datetime import datetime, timedelta, timezone, date
from . import storage as store
from .domain import DemoReader, calculate
from .wind_mcp import WindError

def drain():
    store.DB.parent.mkdir(parents=True, exist_ok=True)
    with open(str(store.DB)+'.lock','a+b') as lock:
        lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        if os.name=='nt':
            import msvcrt
            while True:
                try:
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1); break
                except OSError: time.sleep(.3)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX)
        # Only this process can own execution now. Previous running entries were interrupted.
        with store.connection() as db:
            interrupted=[r['id'] for r in db.execute("SELECT id FROM job_runs WHERE status='running'")]
        for rid in interrupted:
            from .lineage import interrupt_run
            interrupt_run(rid)
            store.fail(rid,'上次执行进程中断，可重新运行')
        while True:
            with store.connection() as db:
                entry=db.execute("SELECT id FROM job_runs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if not entry: break
            run=store.get_run(entry['id']); rid=run['runId']; target=run['targetDate']
            try:
                try:store.ensure_current_run(run)
                except ValueError as exc:
                    store.fail(rid,str(exc));continue
                store.update_run(rid,status='running',phase='获取名单',startedAt=store.now())
                time.sleep(.25)
                store.update_run(rid,phase='获取数据')
                if run.get('source') == 'akshare':
                    from .akshare_provider import collect
                    result = collect(run)
                    store.update_run(rid,status='succeeded',outcome='saved_sample',phase='已完成',finishedAt=store.now(),counts=result.get('counts'),provenance=result.get('provenance'),message=result.get('message','AKShare 样本已保存'))
                    continue
                elif run.get('source') == 'wind':
                    import asyncio
                    from .wind_pipeline import Pipeline
                    awaitable=Pipeline(rid).run()
                    asyncio.run(awaitable)
                elif run.get('source') == 'demo':
                    raw=DemoReader().fetch(target)
                    if raw is None:
                        store.publish_no_data(rid); continue
                    store.update_run(rid,phase='筛选计算')
                    cells,counts=calculate(raw,target)
                    store.update_run(rid,phase='保存结果')
                    store.publish(rid,cells,counts)
                else:
                    raise ValueError('未知的任务数据源')
            except WindError as exc:
                store.fail(rid,str(exc))
                continue
            except Exception as exc:
                message=(f'AKShare 更新失败，保留已有数据：{str(exc)[:300]}' if run.get('source')=='akshare' else '数据处理失败；已有成功快照保留，请检查本地运行环境')
                store.fail(rid,message)
                continue
            # Summary failure must never undo successful market publication.
            try:
                from .analysis import daily_summary
                store.save_summary(rid,daily_summary(target,{}))
            except Exception:
                store.save_summary(rid,dict(status='failed',message='摘要生成失败，可重试',publishedRunId=rid))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--daily',action='store_true')
    args=parser.parse_args()
    store.initialize()
    if args.daily:
        from . import providers
        selected=providers.active_provider()
        end=datetime.now(timezone(timedelta(hours=8))).date()-timedelta(days=1)
        store.enqueue(end.isoformat(),'daily',provider=selected)
        current=date(2026,9,1)
        while current < end:
            if not providers.read_day(current.isoformat())['snapshot'] and current.isoformat() not in providers.available_dates():
                store.enqueue(current.isoformat(),'backfill',provider=selected)
            current+=timedelta(days=1)
    drain()
