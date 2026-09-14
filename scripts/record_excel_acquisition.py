"""Summarize a stopped Excel-universe acquisition and record its terminal job.

This command performs no Wind queries and never publishes a national snapshot.
Pass only this acquisition's pilot plans; earlier enrichment sessions are reused
as data where appropriate but are excluded from this acquisition's billed calls.
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import uuid
from collections import Counter
from datetime import date
from pathlib import Path

from backend import storage as store
from backend.available_data import read_available
from backend.domain import RULES, YIELD_DEFINITION
from backend.excel_acquisition import FIELDS, TERMINAL, cache_index, checkpoint_lock, group_complete, populated
from backend.lineage import dump
from backend.wind_mapping import CODE


LABELS = dict(bondId='稳定债券身份', name='证券简称', issuer='发行人', bondType='一般/专项分类',
              issueDate='发行日期', maturityDate='到期日期', issueAmountYi='发行规模（亿元）',
              couponPct='票面利率（%）', currency='币种', yieldPct='收盘价到期收益率（%）',
              remainingYears='剩余期限（年）', duration='修正久期（年）',
              outstandingBalanceYi='债券余额（亿元）', closeNetPrice='收盘净价（元）')
FIELDS_ALL = tuple(field for group in FIELDS.values() for field in group)
STOPPED = {'finished', 'stopped', 'budget_paused', 'interrupted', 'failed'}


def _pilots(paths, target):
    reports = []
    for path in paths:
        path = Path(path).resolve()
        plan = json.loads(path.read_text(encoding='utf-8-sig'))
        result_path = path.with_name(path.stem+'-result.json')
        result = json.loads(result_path.read_text(encoding='utf-8-sig'))
        if (plan.get('targetDate') != target or result.get('targetDate') != target
                or result.get('status') not in STOPPED or not result.get('sessionId')):
            raise ValueError('试取计划/结果日期不一致、尚未停止，或缺少来源会话')
        reports.append(dict(planPath=str(path), reportPath=str(result_path), sessionId=result['sessionId'],
                            status=result['status']))
    return reports


def build_report(checkpoint_path, pilot_plans=(), supplement_checkpoints=()):
    """Read-only reconciliation. Caller must hold the checkpoint lock."""
    checkpoint_path = Path(checkpoint_path).resolve()
    raw = checkpoint_path.read_bytes()
    checkpoint = json.loads(raw.decode('utf-8-sig'))
    if checkpoint.get('scope') != 'excel_universe' or checkpoint.get('status') not in STOPPED:
        raise ValueError('只可总结已停止的 Excel 全部债券任务，不能记录仍在执行的任务')
    target = checkpoint['targetDate']
    date.fromisoformat(target)
    codes = checkpoint.get('codes', [])
    manifest = checkpoint.get('manifest', {})
    if (not codes or len(codes) != len(set(codes))
            or any(not isinstance(code, str) or not CODE.fullmatch(code) for code in codes)
            or set(manifest) != set(codes)):
        raise ValueError('checkpoint 名单或逐券 manifest 不完整')
    digest = hashlib.sha256(dump(codes).encode('utf-8')).hexdigest()
    if digest != checkpoint.get('universeHash'):
        raise ValueError('checkpoint 名单指纹不匹配')
    if any(set(manifest[code]) != set(FIELDS) for code in codes):
        raise ValueError('逐券 manifest 缺少字段组')
    processed = sum(all(manifest[code][group].get('status') in TERMINAL for group in FIELDS) for code in codes)
    if checkpoint['status'] == 'finished' and processed != len(codes):
        raise ValueError('finished checkpoint 仍有未处理字段组，不能记为名单处理完成')
    pilots = _pilots(pilot_plans, target)
    supplements = []
    for path in supplement_checkpoints:
        path = Path(path).resolve()
        supplement = json.loads(path.read_text(encoding='utf-8-sig'))
        if (supplement.get('scope') != 'excel_field_supplement' or supplement.get('targetDate') != target
                or supplement.get('status') not in STOPPED or supplement.get('universeHash') != digest
                or not supplement.get('sessionId')):
            raise ValueError('单指标补取 checkpoint 尚未停止，或日期、名单、来源会话不匹配')
        supplements.append(dict(checkpointPath=str(path), sessionId=supplement['sessionId'],
                                fields=supplement.get('fields'), status=supplement['status']))
    runner_session = checkpoint.get('sessionId')
    supplement_sessions = {supplement['sessionId'] for supplement in supplements}
    session_ids = list(dict.fromkeys([pilot['sessionId'] for pilot in pilots] +
        ([runner_session] if runner_session else []) + [supplement['sessionId'] for supplement in supplements]))
    session_details = []
    with store.connection() as db:
        for sid in session_ids:
            session = db.execute('SELECT id,target_date,created_at,run_id FROM source_sessions WHERE id=?', (sid,)).fetchone()
            if not session or session['target_date'] != target:
                raise ValueError('来源会话不存在或与目标日期不一致')
            counts = db.execute("SELECT COUNT(*) AS total,SUM(CASE WHEN method='tools/call' THEN 1 ELSE 0 END) AS data,"
                                "MAX(COALESCE(finished_at,started_at)) AS finished FROM source_requests WHERE session_id=?", (sid,)).fetchone()
            session_details.append(dict(sessionId=sid, role='runner' if sid == runner_session else 'supplement'
                                        if sid in supplement_sessions else 'pilot',
                dataCalls=counts['data'] or 0, protocolRequests=counts['total'], createdAt=session['created_at'],
                finishedAt=counts['finished'], associatedRunId=session['run_id']))
    data = read_available(target)
    available = cache_index(data)
    records, missing_counts, dispositions = [], Counter(), Counter()
    matched_entities = {}
    source_request_ids, source_session_ids = set(), set()
    complete = 0
    for code in codes:
        bond = available.get(code)
        missing = [field for field in FIELDS_ALL if not populated((bond or {}).get(field), field)]
        errors = (bond or {}).get('validationErrors', [])
        full = bool(bond) and all(group_complete(bond, group) for group in FIELDS)
        complete += full
        missing_counts.update(missing)
        if bond:
            entity = bond.get('bondId') or bond['code']
            matched_entities[entity] = bond
            source_request_ids.update(bond.get('requestIds', []))
            source_session_ids.update(bond.get('sessionIds', []))
        dispositions[(bond or {}).get('disposition', 'not_returned')] += 1
        records.append(dict(excelCode=code, matched=bool(bond), matchedCode=(bond or {}).get('code'),
            matchedAliases=(bond or {}).get('codes', []), complete=full,
            values={field: (bond or {}).get(field) for field in FIELDS_ALL},
            missingFields=missing, missingFieldLabels=[LABELS[field] for field in missing],
            validationErrors=errors, disposition=(bond or {}).get('disposition', 'not_returned'),
            reason=(bond or {}).get('reason', '该日期未取得此代码或其明确跨市场身份的债券行'),
            groups={group: manifest[code][group] for group in FIELDS},
            requestIds=(bond or {}).get('requestIds', []), sessionIds=(bond or {}).get('sessionIds', [])))
    matched = sum(record['matched'] for record in records)
    entity_dispositions = Counter(bond.get('disposition', 'unknown') for bond in matched_entities.values())
    coverage = dict(requestedCodes=len(codes), processedCodes=processed, pendingCodes=len(codes)-processed,
                    presentCodes=matched, notReturnedCodes=len(codes)-matched,
                    fullyPopulatedCodes=complete, incompleteCodes=len(codes)-complete,
                    conflictedCodes=sum(record['disposition'] == 'conflicted' for record in records),
                    invalidCodes=sum(bool(record['validationErrors']) for record in records),
                    uniqueBondEntities=len(matched_entities), eligibleEntities=entity_dispositions['eligible'],
                    excludedEntities=entity_dispositions['excluded'],
                    completeUniverse=matched == len(codes), allFieldsComplete=complete == len(codes),
                    nationalMarketComplete=False)
    counted_calls = sum(session['dataCalls'] for session in session_details)
    return dict(scope='excel_universe', targetDate=target, checkpointPath=str(checkpoint_path),
        checkpointHash=hashlib.sha256(raw).hexdigest(), checkpointStatus=checkpoint['status'],
        universeHash=digest, sourcePath=checkpoint.get('sourcePath'), sourceHash=checkpoint.get('sourceHash'),
        sheet=checkpoint.get('sheet'), generatedAt=store.now(),
        createdAt=checkpoint.get('createdAt'), startedAt=checkpoint.get('startedAt'),
        finishedAt=max([checkpoint.get('finishedAt') or checkpoint.get('updatedAt') or store.now()] +
                       [session['finishedAt'] for session in session_details if session['finishedAt']]),
        stopReason=checkpoint.get('stopReason'), runnerSessionId=runner_session,
        traceSessionIds=session_ids, acquisitionSessions=session_details, pilotPlans=pilots,
        supplementCheckpoints=supplements,
        dataCalls=counted_calls, pilotDataCalls=sum(session['dataCalls'] for session in session_details if session['role'] == 'pilot'),
        runnerDataCalls=sum(session['dataCalls'] for session in session_details if session['role'] == 'runner'),
        supplementDataCalls=sum(session['dataCalls'] for session in session_details if session['role'] == 'supplement'),
        checkpointDataCalls=checkpoint.get('dataCalls'),
        reusedSourceSessionIds=sorted(source_session_ids-set(session_ids)),
        scopeSourceRequestCount=len(source_request_ids), coverage=coverage,
        missingFieldCounts={field: missing_counts[field] for field in FIELDS_ALL},
        missingFieldLabels=LABELS, dispositionCounts=dict(dispositions),
        rulesVersion=RULES['version'], mappingVersion=data['mappingVersion'], yieldDefinition=YIELD_DEFINITION,
        publishedSnapshot=False, warnings=['Excel 名单覆盖情况不代表全国当日债券市场完整覆盖',
            'finished 表示名单处理结束，字段完整度以 fullyPopulatedCodes 为准',
            '返回空值保留为缺失，未按零填充；已使用同日期保存数据和明确的跨市场代码关系'],
        bonds=records)


def _write_outputs(report, output_dir):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = report['targetDate'].replace('-', '')+'-Excel全部债券'
    json_path = output_dir/(prefix+'取数结果.json')
    csv_path = output_dir/(prefix+'缺失字段.csv')
    data_csv = output_dir/(prefix+'全部数据.csv')
    report['outputPaths'] = dict(json=str(json_path), missingCsv=str(csv_path), dataCsv=str(data_csv))
    temporary = json_path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, json_path)
    temporary = csv_path.with_suffix('.csv.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['查询日期', 'Excel代码', '匹配Wind代码', '证券简称', '基本信息查询状态', '行情查询状态',
                         '缺失指标', '缺失字段代码', '冲突或校验信息', '记录状态', '原因', '来源会话', '原始请求ID'])
        for bond in report['bonds']:
            if bond['complete']:
                continue
            writer.writerow([report['targetDate'], bond['excelCode'], bond['matchedCode'], bond['values']['name'],
                bond['groups']['basic']['status'], bond['groups']['market']['status'],
                '；'.join(bond['missingFieldLabels']), ';'.join(bond['missingFields']),
                '；'.join(bond['validationErrors']), bond['disposition'], bond['reason'],
                ';'.join(bond['sessionIds']), ';'.join(bond['requestIds'])])
    os.replace(temporary, csv_path)
    temporary = data_csv.with_suffix('.csv.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['查询日期', 'Excel代码', '匹配Wind代码'] + [LABELS[field] for field in FIELDS_ALL] +
                        ['记录覆盖', '字段完整', '基本信息查询状态', '行情查询状态', '缺失指标', '冲突或校验信息',
                         '记录状态', '原因', '来源会话', '原始请求ID'])
        for bond in report['bonds']:
            writer.writerow([report['targetDate'], bond['excelCode'], bond['matchedCode']] +
                [bond['values'][field] for field in FIELDS_ALL] +
                ['已取得' if bond['matched'] else '未取得', '完整' if bond['complete'] else '未完整',
                 bond['groups']['basic']['status'], bond['groups']['market']['status'],
                 '；'.join(bond['missingFieldLabels']), '；'.join(bond['validationErrors']),
                 bond['disposition'], bond['reason'], ';'.join(bond['sessionIds']), ';'.join(bond['requestIds'])])
    os.replace(temporary, data_csv)


def record(checkpoint_path, pilot_plans=(), output_dir=None, supplement_checkpoints=()):
    checkpoint_path = Path(checkpoint_path).resolve()
    with checkpoint_lock(checkpoint_path):
        report = build_report(checkpoint_path, pilot_plans, supplement_checkpoints)
        coverage = report['coverage']
        succeeded = report['checkpointStatus'] == 'finished'
        status = 'succeeded' if succeeded else 'failed'
        phase = 'Excel 名单处理结束' if succeeded else 'Excel 名单取数已停止'
        message = (f"Excel 名单 {coverage['requestedCodes']} 个代码，已处理 {coverage['processedCodes']} 个；"
                   f"已有返回 {coverage['presentCodes']} 个，字段完整 {coverage['fullyPopulatedCodes']} 个，"
                   f"仍缺字段或存在冲突 {coverage['incompleteCodes']} 个。"
                   f"本轮 {report['dataCalls']} 次数据查询（试取 {report['pilotDataCalls']} 次，"
                   f"单指标补取 {report['supplementDataCalls']} 次）。"
                   '未发布全国完整快照。')
        marker = 'excel-acquisition:'+str(report['runnerSessionId'] or report['universeHash']+':'+report['targetDate'])
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT value FROM metadata WHERE key=?', (marker,)).fetchone()
            run_id = previous['value'] if previous else str(uuid.uuid4())
            for session in report['acquisitionSessions']:
                current = db.execute('SELECT target_date,run_id FROM source_sessions WHERE id=?', (session['sessionId'],)).fetchone()
                if not current or current['target_date'] != report['targetDate'] or current['run_id'] not in (None, run_id):
                    raise ValueError('来源会话已关联其他任务，禁止将历史查询计入本轮任务')
            run = dict(runId=run_id, targetDate=report['targetDate'], triggerType='manual_retry',
                status=status, outcome='excel_universe', scope='excel_universe', phase=phase,
                createdAt=report['createdAt'] or report['generatedAt'],
                startedAt=report['startedAt'] or report['createdAt'], finishedAt=report['finishedAt'], message=message,
                counts=dict(source=coverage['presentCodes'], eligible=coverage['eligibleEntities'],
                            missing=coverage['incompleteCodes'], terms=coverage['excludedEntities']),
                excelCoverage=coverage, rulesVersion=report['rulesVersion'], mappingVersion=report['mappingVersion'],
                source=store.MODE, yieldDefinition=YIELD_DEFINITION, rules=RULES,
                traceSessionId=report['runnerSessionId'] or next(iter(report['traceSessionIds']), None),
                traceSessionIds=report['traceSessionIds'], dataCalls=report['dataCalls'],
                pilotDataCalls=report['pilotDataCalls'], runnerDataCalls=report['runnerDataCalls'],
                supplementDataCalls=report['supplementDataCalls'],
                checkpointPath=str(checkpoint_path), checkpointHash=report['checkpointHash'],
                acquisitionStatus=report['checkpointStatus'], stopReason=report['stopReason'], publishedSnapshot=False)
            if previous:
                if not db.execute('SELECT 1 FROM job_runs WHERE id=?', (run_id,)).fetchone():
                    raise ValueError('已有总结标记引用的任务不存在')
                db.execute('UPDATE job_runs SET status=?,payload=? WHERE id=?', (status, dump(run), run_id))
            else:
                db.execute('INSERT INTO job_runs VALUES (?,?,?,?)', (run_id, report['targetDate'], status, dump(run)))
                db.execute('INSERT INTO metadata VALUES (?,?)', (marker, run_id))
            # Existing published snapshot and date readiness are deliberately
            # retained; only the newest attempt points to this terminal record.
            db.execute('INSERT INTO valuation_dates(date,latest_id) VALUES (?,?) ON CONFLICT(date) '
                       'DO UPDATE SET latest_id=excluded.latest_id', (report['targetDate'], run_id))
            db.executemany('UPDATE source_sessions SET run_id=? WHERE id=?',
                           [(run_id, sid) for sid in report['traceSessionIds']])
        report['runId'] = run_id
        report['run'] = run
        _write_outputs(report, output_dir or store.ROOT/'outputs')
        return report


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--pilot-plans', type=Path, nargs='*', default=[])
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--supplement-checkpoints', type=Path, nargs='*', default=[])
    args = parser.parse_args()
    result = record(args.checkpoint, args.pilot_plans, args.output_dir, args.supplement_checkpoints)
    print(json.dumps({key: result[key] for key in
        ('runId', 'targetDate', 'checkpointStatus', 'dataCalls', 'pilotDataCalls', 'runnerDataCalls',
         'supplementDataCalls', 'coverage', 'outputPaths')},
        ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
