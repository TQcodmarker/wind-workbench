"""Dated AKShare observations, isolated from Wind snapshots and credentials.

Individual yields prefer saved ChinaBond valuations, then dated bond YTM.
Index duration can provide an explicitly labelled grouping fallback; index and
curve yields remain references and never become individual-bond yields.
AKShare is imported only by the live collector, so Wind remains usable without it.
"""
from collections import defaultdict
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import importlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import RLock
import time
import uuid
from urllib.parse import urlsplit

from . import storage as store
from .domain import COHORTS, REGIONS, TERMS, number
from .bond_duration import calculate_duration
from .cashflow_constraints import apply_cashflow_constraints
from .selection_policy import select_yield, select_duration
from .saved_bond_metrics import load_saved_metrics

ROOT = Path(__file__).resolve().parents[1]
VERIFIED = ROOT / 'research' / 'akshare-dictionary-normalized-20260912'
DOC = 'https://akshare.akfamily.xyz/data/bond/bond.html'
MAX_DETAIL_QUERIES = 20
YIELD_DEFINITION = dict(metric='valuation_then_ytm', priceBasis='per_bond_priority',
                        label='采用收益率（中债估值优先）', source='逐券已保存来源')
RULES = dict(version='rules-akshare-v3-priority-yield-duration-reference', mappingVersion='akshare-local-v3',
             cutoff='2025-08-08', terms=TERMS, regions=REGIONS, historyStart='2026-09-01',
             rounding='nearest_duration_bucket_ties_up', weight='发行规模（亿元）',
             formula='Σ（逐券采用收益率 × 发行规模）÷ Σ发行规模',
             yieldDefinition=YIELD_DEFINITION, clausePolicy='cashflow_constraints_on_estimates_with_index_fallback',
             yieldPriority=['chinabond_valuation', 'ytm'],
             durationPriority=['direct', 'calculated', 'estimated', 'index_reference'],
             dataAcceptance='dated_individual_valuation_then_ytm; explicit_index_duration_fallback',
             dateBasis='source_observation_date', remainingTermBasis='Actual/365',
             groupingBasis='modified_duration', groupingLabel='修正久期档位',
             durationPolicy='个券直接久期优先，其次个券计算或估算久期，均缺失时使用同日地方债指数久期参考并标注',
             durationBuckets='就近归入标准久期档；等距归较大档，低于最小档/高于最大档归端点档',
             scope='saved_sample')
_QUERY_LOCK = RLock()
_DATASET_LOCK = RLock()
_CODE = re.compile(r'^\d{6,9}(?:\.IB)?$')
_LOCAL_NAME = re.compile(r'^\d{2}(?:北京|天津|河北|山西|内蒙古|辽宁|吉林|黑龙江|上海|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|广西|海南|重庆|四川|贵州|云南|西藏|陕西|甘肃|青海|宁夏|新疆|兵团|深圳|厦门|宁波|大连|青岛)(?:债)?\d+$')


def _dump(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


@contextmanager
def dataset_lock():
    """Serialize same-source materialization across independent acquisition workers.

    Only the final read/merge/write holds this lock; network I/O stays outside it.
    """
    store.DB.parent.mkdir(parents=True, exist_ok=True)
    with _DATASET_LOCK, open(str(store.DB)+'.akshare-dataset.lock', 'a+b') as lock:
        lock.seek(0)
        lock.write(b'0')
        lock.flush()
        lock.seek(0)
        if os.name=='nt':
            import msvcrt
            while True:
                try:
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                    break
                except OSError:
                    time.sleep(.1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name=='nt':
                msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(lock,fcntl.LOCK_UN)


def _read_rows(query, args=()):
    if not store.DB.exists():
        return []
    with closing(sqlite3.connect(store.DB.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in db.execute(query, args)]
        except sqlite3.OperationalError as exc:
            if 'no such table' in str(exc):
                return []
            raise


def initialize(seed=True):
    with store.connection() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS akshare_datasets (
                target_date TEXT PRIMARY KEY, collected_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS akshare_details (
                code TEXT PRIMARY KEY, collected_at TEXT NOT NULL, payload TEXT NOT NULL, evidence TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS akshare_queries (
                id TEXT PRIMARY KEY, run_id TEXT, target_date TEXT NOT NULL,
                collected_at TEXT NOT NULL, function_name TEXT NOT NULL, payload TEXT NOT NULL);
        ''')
    if seed and not _read_rows('SELECT target_date FROM akshare_datasets LIMIT 1'):
        _import_verified()


def _numeric(value):
    try:
        return str(number(value))
    except ValueError:
        return None


def _date(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _code(value):
    value = str(value or '')
    return value if value.endswith('.IB') and _CODE.fullmatch(value) else (
        value+'.IB' if _CODE.fullmatch(value) else None)


def _region(issuer):
    # Cities and the corps precede provinces, avoiding e.g. 新疆兵团 -> 新疆省.
    issuer = str(issuer)
    if '新疆' in issuer and '兵团' in issuer:
        return 'xpcc'
    matches = [region for region in REGIONS if region['name'] in str(issuer)]
    city_ids = {'shenzhen', 'xiamen', 'ningbo', 'dalian', 'qingdao'}
    return max(matches, key=lambda region: (region['id'] in city_ids, len(region['name'])))['id'] if matches else None


def _detail_cache():
    return {row['code']: (json.loads(row['payload']), json.loads(row['evidence']))
            for row in _read_rows('SELECT * FROM akshare_details')}


def _save_detail(code, detail, evidence):
    if _code(detail.get('bondCode')) != code or detail.get('bondType') != '地方政府债':
        raise ValueError('公开详情身份或债券类型与请求不符')
    with store.connection() as db:
        db.execute('INSERT OR REPLACE INTO akshare_details VALUES (?,?,?,?)',
                   (code, evidence.get('retrievedAt') or store.now(), _dump(detail), _dump(evidence)))


def _field_source(name, value, unit, evidence, source_date=None):
    return dict(name=name, value=value, unit=unit,
                requestId=evidence.get('requestId', 'akshare-verified'),
                sessionId=evidence.get('runId', 'akshare-verified'),
                source='akshare', sourceFunction=evidence.get('function'),
                sourceDate=source_date, retrievedAt=evidence.get('retrievedAt'),
                executionMode=evidence.get('executionMode', 'documented_sdk'),
                reusedSameDate=evidence.get('reusedSameDate', False),
                evidence=evidence.get('evidence', []))


def _interpolate(points, remaining):
    if remaining is None:
        return None
    term = number(remaining)
    for index, (x, y) in enumerate(points):
        x, y = number(x), number(y)
        if term == x:
            return str(y)
        if term < x:
            if index == 0:
                return None
            x0, y0 = map(number, points[index-1])
            return str(y0 + (y-y0)*(term-x0)/(x-x0))
    return None


def normalize_bond(detail, target, trade=None, benchmarks=None, detail_evidence=None,
                   trade_evidence=None, benchmark_evidence=None, metric_observations=None):
    """Normalize a bond with explicit per-metric source priority and provenance."""
    date.fromisoformat(target)
    benchmarks = benchmarks or {}
    evidence = detail_evidence or {}
    trade_evidence = trade_evidence or {}
    benchmark_evidence = benchmark_evidence or {}
    code = _code(detail.get('bondCode'))
    if code is None or detail.get('bondType') != '地方政府债':
        raise ValueError('详情缺少可验证的地方政府债身份')
    issued, maturity = _date(detail.get('issueDate')), _date(detail.get('mrtyDate'))
    days = (date.fromisoformat(maturity)-date.fromisoformat(target)).days if maturity else None
    remaining = str(Decimal(max(days, 0))/Decimal(365)) if days is not None else None
    matured = days is not None and days <= 0
    future = bool(issued and issued > target)
    region = _region(detail.get('entyFullName'))
    full_name = str(detail.get('bondFullName') or '')
    bond_type = 'special' if '专项债' in full_name else ('general' if '一般债' in full_name else None)
    observed_at = _observation_time(trade.get('showDate')) if trade else None
    trade_valid = bool(trade and _code(trade.get('bondcode')) == code and observed_at and
                       observed_at.date().isoformat() == target and not matured and not future)
    trade_yield = _numeric(trade.get('dmiLatestContraRate')) if trade_valid else None
    traded_price = _numeric(trade.get('dmiLatestRate')) if trade_valid else None
    amount, coupon = _numeric(detail.get('issueAmnt')), _numeric(detail.get('parCouponRate'))
    trade_fields = ([_field_source('dmiLatestContraRate / 最新收益率', trade_yield, '%', trade_evidence, target)]
                    if trade_yield is not None else [])
    if trade_fields:
        trade_fields[0]['observationTime'] = observed_at.isoformat()
    observations = metric_observations or []
    yield_selection = select_yield(code, target, observations,
        dict(code=code, date=target, metric='ytm', value=trade_yield, unit='%',
             source='AKShare / 中国货币网', priceBasis='latest_trade', fieldSources=trade_fields)
        if trade_yield is not None else None)
    if matured or future:
        yield_selection = select_yield(code, target, [], None)
    yield_pct = yield_selection['value']
    duration_calculation = calculate_duration(apply_cashflow_constraints(code, detail), target, yield_pct)
    sources = {}
    source_map = {'code': ('bondCode', code, '代码'), 'name': ('bondName', detail.get('bondName'), None),
                  'issuer': ('entyFullName', detail.get('entyFullName'), None),
                  'regionId': ('entyFullName', region, None),
                  'bondType': ('bondFullName', bond_type, None),
                  'issueDate': ('issueDate', issued, '日期'),
                  'maturityDate': ('mrtyDate', maturity, '日期'),
                  'remainingYears': ('mrtyDate / Actual365', remaining, '年'),
                  'issueAmountYi': ('issueAmnt', amount, '亿元'),
                  'couponPct': ('parCouponRate', coupon, '%'),
                  'couponType': ('couponType', detail.get('couponType'), None),
                  'couponFrequency': ('couponFrqncy', detail.get('couponFrqncy'), None),
                  'interestStartDate': ('frstValueDate', _date(detail.get('frstValueDate')), '日期'),
                  'firstCouponDate': ('frstCpnDt', _date(detail.get('frstCpnDt')), '日期')}
    for field, (source_name, value, unit) in source_map.items():
        if value is not None:
            sources[field] = [_field_source(source_name, value, unit, evidence,
                                           target if field == 'remainingYears' else None)]
    if trade_fields:
        sources['tradeYieldPct'] = trade_fields
    if yield_pct is not None:
        sources['yieldPct'] = deepcopy(yield_selection['fieldSources'])
    if traded_price is not None:
        sources['tradeNetPrice'] = [_field_source('dmiLatestRate / 成交净价', traded_price, '元', trade_evidence, target)]
    calculated_duration = duration_calculation['modifiedYears']
    if calculated_duration is not None:
        calculation_sources = [dict(_field_source('修正久期计算输入：静态现金流要素', calculated_duration, '年', evidence, target),
                                    calculationMethod=duration_calculation['methodVersion'],
                                    calculationStatus=duration_calculation['status'])]
        calculation_sources.extend(dict(item, name='修正久期计算输入：'+item['name'],
                                        value=calculated_duration, unit='年',
                                        calculationMethod=duration_calculation['methodVersion'],
                                        calculationStatus=duration_calculation['status'])
                                   for item in sources.get('yieldPct', []))
        duration_calculation['sourceRequestIds'] = list(dict.fromkeys(item['requestId'] for item in calculation_sources))
        duration_calculation['fieldSources'] = calculation_sources
    reference = dict(indexYieldPct=None, indexDurationYears=None, curveYieldPct=None,
                     adjustedYieldReferencePct=None, spreadReferenceBp=None,
                     adjustedSpreadReferenceBp=None, durationSizeReference=None,
                     issueSizeReferenceYi=amount, yieldReferenceKind=None)
    substitutions = []

    def substitute(field, label, actual_metric, value, unit, function, source_date, grain):
        if value is not None:
            substitutions.append(dict(field=field, label=label, actualMetric=actual_metric,
                                      value=value, unit=unit, isProxy=True, status='obtained_substitute',
                                      statusLabel='已获取（替代）', sourceFunction=function,
                                      sourceDate=source_date, entityGrain=grain))

    substitute('remainingYears', '剩余期限', '剩余期限（Actual/365）', remaining, '年',
               'bond_info_detail_cm', target, 'bond_day')
    substitute('outstandingBalanceYi', '未偿余额', '发行规模参考；不代表未偿余额', amount, '亿元',
               'bond_info_detail_cm', issued, 'bond_issue_event')
    if not matured and not future:
        reference['indexYieldPct'] = _numeric(benchmarks.get('indexYieldPct'))
        reference['indexDurationYears'] = _numeric(benchmarks.get('indexDurationYears'))
        reference['curveYieldPct'] = _interpolate(benchmarks.get('curveNodes', []), remaining)
        ref_yield = yield_pct if yield_pct is not None else reference['indexYieldPct']
        reference['yieldReferenceKind'] = yield_selection['kind'] if yield_pct is not None else (
            'index_reference' if ref_yield is not None else None)
        if ref_yield is not None and coupon is not None:
            reference['adjustedYieldReferencePct'] = str(number(ref_yield)+Decimal('.25')*number(coupon))
        if ref_yield is not None and reference['curveYieldPct'] is not None:
            reference['spreadReferenceBp'] = str((number(ref_yield)-number(reference['curveYieldPct']))*100)
            if reference['adjustedYieldReferencePct'] is not None:
                reference['adjustedSpreadReferenceBp'] = str((number(reference['adjustedYieldReferencePct'])-Decimal('1.25')*number(reference['curveYieldPct']))*100)
        if amount is not None and reference['indexDurationYears'] is not None:
            reference['durationSizeReference'] = str(number(amount)*number(reference['indexDurationYears']))
        substitute('indexDurationYears', '地方债指数久期参考', '地方政府债指数平均市值法久期参考',
                   reference['indexDurationYears'], '年', 'bond_index_general_cbond', target, 'index_day')
        substitute('curveYieldPct', '地方债曲线', 'CFETS地方政府债AAA曲线插值收益率',
                   reference['curveYieldPct'], '%', 'bond_china_close_return', target, 'curve_tenor_day')
        if yield_selection['kind'] != 'chinabond_valuation':
            substitute('valuationYieldPct', '中债估值收益率', '个券到期收益率（兜底）' if yield_pct is not None else '地方政府债指数平均到期收益率参考',
                       ref_yield, '%', (yield_selection.get('source') or 'bond_spot_deal') if yield_pct is not None else 'bond_index_general_cbond',
                       target, 'bond_market_day' if yield_pct is not None else 'index_day')
        for field, label, metric, unit in (
            ('adjustedYieldReferencePct', '原表免税收益/综合收益', '按原表公式 J+0.25×票息计算的调整收益参考', '%'),
            ('spreadReferenceBp', '原表非免税曲线偏离', '采用个券收益率相对CFETS曲线差值' if yield_pct is not None else '指数参考收益率相对CFETS曲线差值', 'BP'),
            ('adjustedSpreadReferenceBp', '原表免税曲线偏离', '按原表调整公式计算的参考差值', 'BP'),
            ('durationSizeReference', '原表久期×余额', '地方债指数久期×发行规模；仅作研究参考', '年×亿元'),
        ):
            substitute(field, label, metric, reference[field], unit, 'calculated_from_public_references',
                       target, 'research_reference')
        for field in ('indexYieldPct', 'indexDurationYears', 'curveYieldPct'):
            if reference[field] is not None:
                sources[field] = [_field_source(field, reference[field], '年' if field == 'indexDurationYears' else '%',
                                                benchmark_evidence.get(field, {}), benchmark_evidence.get(field, {}).get('sourceDate') or target)]
    index_evidence = benchmark_evidence.get('indexDurationYears', {})
    index_candidate = dict(value=reference['indexDurationYears'],
                           date=index_evidence.get('sourceDate') or benchmarks.get('indexDurationDate'),
                           source='AKShare / 中债地方政府债指数', entityGrain='index', metric='index_duration',
                           fieldSources=sources.get('indexDurationYears', []))
    duration_selection = select_duration(code, target, [] if matured or future else observations,
                                         duration_calculation, index_candidate if not matured and not future else None)
    duration, term = duration_selection['value'], duration_selection['bucketYears']
    if duration is not None:
        sources['duration'] = deepcopy(duration_selection['fieldSources'])
    if yield_selection['kind'] == 'chinabond_valuation':
        sources['valuationYieldPct'] = deepcopy(sources['yieldPct'])
    missing = [label for value, label in ((region, '发行地区'), (bond_type, '一般/专项分类'),
               (issued, '发行日期'), (remaining, '剩余期限'), (amount, '发行规模'),
               (yield_pct, '目标日个券收益率（中债估值或到期收益率）'), (duration, '可用久期（个券或指数参考）')) if value is None]
    if matured or future:
        disposition, reason = 'excluded', '已到期，不适用当日行情' if matured else '尚未发行'
    elif missing:
        disposition, reason = 'incomplete', ('仅有指数收益率参考，不参与个券收益率汇总' if yield_pct is None and reference['indexYieldPct'] is not None else '缺失或无效：'+'、'.join(missing))
        if yield_pct is not None and duration is None:
            reason += '；'+duration_selection['reason']
    elif number(amount) <= 0:
        disposition, reason = 'excluded', '发行规模不大于 0'
    elif term not in TERMS:
        disposition, reason = 'excluded', '无法归入修正久期档位'
    else:
        disposition, reason = 'eligible', (f"采用{yield_selection['label']}，按{duration_selection['label']}归入 {term} 年档，按发行规模加权汇总")
    request_ids = list(dict.fromkeys(item['requestId'] for values in sources.values() for item in values))
    session_ids = list(dict.fromkeys(item['sessionId'] for values in sources.values() for item in values))
    return dict(code=code, bondId=code, name=detail.get('bondName'), issuer=detail.get('entyFullName'),
                regionId=region, bondType=bond_type, issueDate=issued, maturityDate=maturity,
                remainingYears=remaining, termYears=term, issueAmountYi=amount, couponPct=coupon,
                yieldPct=yield_pct, yieldDate=target if yield_pct is not None else None,
                yieldSelection=yield_selection, valuationYieldPct=yield_selection.get('valuationValue'),
                tradeYieldPct=trade_yield,
                tradeObservedAt=observed_at.isoformat() if trade_valid else None,
                yieldMetric=yield_selection['yieldMetric'], yieldPriceBasis=yield_selection['yieldPriceBasis'],
                duration=duration, durationCalculation=duration_calculation,
                durationSelection=duration_selection, individualDuration=duration_selection.get('individualValue'),
                outstandingBalanceYi=None, closeNetPrice=None, tradeNetPrice=traded_price,
                currency=None if detail.get('bondCcy') in (None, '---', '') else detail.get('bondCcy'),
                cohort=COHORTS[0 if issued < '2025-08-08' else 1] if issued else None,
                disposition=disposition, reason=reason, missingFields=missing, validationErrors=[],
                source='akshare', fieldSources=sources, sourceUnits={'issueAmountYi': '亿元'},
                codes=[code], requestIds=request_ids, sessionIds=session_ids,
                substitutions=substitutions, referenceFields=reference,
                earlyRepayment=None, redemption=None)


def aggregate(bonds):
    groups = defaultdict(lambda: [0, Decimal(0), Decimal(0), 0, 0, 0, 0, 0])
    seen = set()
    for bond in bonds:
        if bond['code'] in seen:
            raise ValueError('AKShare 样本存在重复债券身份')
        seen.add(bond['code'])
        if bond['disposition'] != 'eligible':
            continue
        selected_yield = bond.get('yieldSelection') or {}
        if (selected_yield.get('kind') not in ('chinabond_valuation', 'ytm')
                or bond.get('yieldPct') is None or selected_yield.get('value') != bond['yieldPct']
                or selected_yield.get('sourceDate') != bond.get('yieldDate')
                or bond.get('yieldMetric') != selected_yield.get('yieldMetric')
                or bond.get('yieldPriceBasis') != selected_yield.get('yieldPriceBasis')):
            raise ValueError('AKShare 汇总禁止使用指数收益率参考')
        selection = bond.get('durationSelection') or {}
        if (selection.get('kind') not in ('direct', 'estimated', 'calculated', 'index_reference')
                or bond.get('duration') is None
                or bond.get('duration') != selection.get('value')
                or bond.get('termYears') != selection.get('bucketYears')
                or selection.get('sourceDate') != bond.get('yieldDate')
                or bond.get('termYears') not in TERMS):
            raise ValueError('AKShare 汇总必须使用有来源的同日久期及其就近档位')
        for scope in ('all', bond['bondType']):
            values = groups[(bond['cohort'], bond['regionId'], bond['termYears'], scope)]
            values[0] += 1
            values[1] += number(bond['issueAmountYi'])
            values[2] += number(bond['yieldPct'])*number(bond['issueAmountYi'])
            values[3] += selection['kind'] == 'estimated'
            values[4] += selection['kind'] == 'direct'
            values[5] += selection['kind'] == 'index_reference'
            values[6] += selected_yield['kind'] == 'chinabond_valuation'
            values[7] += selected_yield['kind'] == 'ytm'
    return [dict(cohort=key[0], regionId=key[1], termYears=key[2], bondScope=key[3],
                 cellState='value', yieldPct=str((weighted/amount).quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP)),
                 sampleCount=count, issueAmountSumYi=str(amount), weightedYieldSum=str(weighted),
                 yieldMetric=YIELD_DEFINITION['metric'], yieldPriceBasis=YIELD_DEFINITION['priceBasis'],
                 groupingBasis='modified_duration', estimatedDurationCount=estimated,
                 directDurationCount=direct, indexDurationCount=index,
                 valuationYieldCount=valuation, ytmYieldCount=ytm)
            for key, (count, amount, weighted, estimated, direct, index, valuation, ytm) in sorted(groups.items())]


def _dataset(target, bonds, benchmarks=None, provenance=None, warnings=None):
    counts = dict(bonds=len(bonds), eligible=0, incomplete=0, excluded=0, conflicted=0,
                  requests=len({rid for bond in bonds for rid in bond['requestIds']}),
                  sessions=len({sid for bond in bonds for sid in bond['sessionIds']}),
                  substitutions=sum(len(bond['substitutions']) for bond in bonds),
                  referenceOnly=sum(bond['yieldPct'] is None and bond['referenceFields'].get('indexYieldPct') is not None for bond in bonds))
    for bond in bonds:
        counts[bond['disposition']] += 1
    counts.update(durationEstimated=sum((bond.get('durationSelection') or {}).get('kind') == 'estimated' for bond in bonds),
                  durationCalculated=sum((bond.get('durationSelection') or {}).get('kind') == 'calculated' for bond in bonds),
                  durationDirect=sum((bond.get('durationSelection') or {}).get('kind') == 'direct' for bond in bonds),
                  durationIndexReference=sum((bond.get('durationSelection') or {}).get('kind') == 'index_reference' for bond in bonds),
                  eligibleDurationIndexReference=sum(bond['disposition'] == 'eligible' and (bond.get('durationSelection') or {}).get('kind') == 'index_reference' for bond in bonds),
                  yieldValuation=sum((bond.get('yieldSelection') or {}).get('kind') == 'chinabond_valuation' for bond in bonds),
                  yieldYtm=sum((bond.get('yieldSelection') or {}).get('kind') == 'ytm' for bond in bonds),
                  durationUnavailable=sum(bond.get('duration') is None for bond in bonds))
    obsolete_warnings = ('发行规模作为未偿余额的规模参考；指数久期和指数收益率仅作基准参考',
                         '公开成交收益率与 Wind 收盘价到期收益率、中债估值收益率属于不同口径',
                         '按个券修正久期就近分档；估算采用注明的付息与还本假设')
    return dict(evaluationDate=target, source='akshare', scope='saved_sample', complete=False,
                rulesVersion=RULES['version'], mappingVersion=RULES['mappingVersion'],
                yieldDefinition=YIELD_DEFINITION, counts=counts,
                groupingBasis='modified_duration', rules=deepcopy(RULES),
                bonds=sorted(bonds, key=lambda bond: ({'eligible': 0, 'incomplete': 1, 'excluded': 2, 'conflicted': 3}[bond['disposition']], bond['code'])),
                cells=aggregate(bonds), benchmarks=benchmarks or {}, provenance=provenance or {},
                warnings=list(dict.fromkeys([
                    'AKShare 已保存样本，覆盖范围未经全市场完整性确认。',
                    '收益率逐券优先采用同日中债估值收益率，缺失时采用同日个券到期收益率；汇总可能包含两种口径，按实际来源分别计数。',
                    '久期优先个券直接值，其次个券计算或估算值；缺失时按用户规则采用同日地方债指数久期参考参与分档，并单独统计参考样本。',
                    '指数久期是共同参考，不代表该券实际修正久期或风险；指数收益率及曲线不替代个券收益率。',
                    '发行规模用于加权；个券估算久期采用已列明的现金流假设，已知复杂现金流且缺少安排时不进行个券估算。',
                    *(warning for warning in warnings or [] if not warning.startswith(obsolete_warnings))])))


def _save_dataset(dataset):
    from .akshare_read_model import save_dataset
    save_dataset(dataset)


def _import_verified():
    """Import dated public evidence bundled with this workspace, never Wind rows."""
    required = [VERIFIED/name for name in ('sample-values.json', 'normalized-observations.json', 'benchmark-indicators.json')]
    if not all(path.exists() for path in required):
        return
    samples, observations, indicators = [json.loads(path.read_text(encoding='utf-8')) for path in required]
    target = indicators['as_of_date']
    benchmarks = dict(indexYieldPct=_numeric(indicators['index']['average_yield_pct']),
                      indexDurationYears=_numeric(indicators['index']['average_duration']),
                      curveNodes=[[_numeric(row['期限']), _numeric(row['到期收益率'])] for row in indicators['curve']['rows'] if _date(row['日期']) == target])
    bonds = []
    saved_metrics = load_saved_metrics(target)
    for sample in samples:
        field_rows = [row for row in observations if row['target_entity_id'] == sample['code']]
        identity = next(row for row in field_rows if row['target_column'] == 'A')
        # Rebase the old absolute evidence path for a moved checkout.
        evidence_path = Path(identity['evidence'][0])
        if not evidence_path.exists():
            components = evidence_path.parts
            evidence_path = ROOT.joinpath(*components[components.index('research'):]) if 'research' in components else evidence_path
        if not evidence_path.exists():
            continue
        detail = {row['name']: row['value'] for row in json.loads(evidence_path.read_text(encoding='utf-8'))}
        detail_ev = dict(requestId='akshare-verified-detail-'+sample['code'], runId='akshare-verified-import',
                         function='bond_info_detail_cm', retrievedAt=identity['retrieved_at'],
                         executionMode='compatibility_adapter', evidence=[str(evidence_path)])
        _save_detail(sample['code'], detail, detail_ev)
        trade = None
        if sample['yield_method'] == 'bond_trade':
            trade = dict(bondcode=sample['code'].removesuffix('.IB'), showDate=target,
                         dmiLatestContraRate=sample['values']['J'])
        # Earlier builder uses same_day_trade; preserve only explicit bond-grain evidence.
        j_row = next(row for row in field_rows if row['target_column'] == 'J')
        if j_row['source_function'] == 'bond_spot_deal' and j_row.get('source_date', '')[:10] == target:
            trade = dict(bondcode=sample['code'].removesuffix('.IB'), showDate=target,
                         dmiLatestContraRate=j_row['value'])
        trade_ev = dict(requestId='akshare-verified-trade', runId='akshare-verified-import',
                        function='bond_spot_deal', retrievedAt=j_row['retrieved_at'], evidence=j_row['evidence'])
        bench_ev = {}
        for key, column in (('indexYieldPct', 'J'), ('indexDurationYears', 'H'), ('curveYieldPct', 'I')):
            observation = next(row for row in field_rows if row['target_column'] == column)
            # The traded bond's J comes from a trade, so use the index observation
            # from another sample when describing the shared index reference.
            if key == 'indexYieldPct':
                observation = next(row for row in observations if row['target_column'] == 'J' and
                                   row['source_function'] == 'bond_index_general_cbond' and row['value'] is not None)
            bench_ev[key] = dict(requestId='akshare-verified-'+key, runId='akshare-verified-import',
                                function='bond_china_close_return' if key == 'curveYieldPct' else 'bond_index_general_cbond',
                                retrievedAt=observation['retrieved_at'], evidence=observation['evidence'], sourceDate=target)
        bond = normalize_bond(detail, target, trade, benchmarks, detail_ev, trade_ev, bench_ev,
                              saved_metrics.get(sample['code'], []))
        bond['fieldObservations'] = field_rows
        bonds.append(bond)
    if bonds:
        data = _dataset(target, bonds, benchmarks,
                        dict(source='akshare', complete=False, origin='verified_import',
                             collectedAt=max(row['retrieved_at'] for row in observations),
                             importedAt=store.now(), dictionaryUrl=DOC, batchLimit=MAX_DETAIL_QUERIES,
                             scope='已验证的5只地方政府债样本', evidenceRoot=str(VERIFIED)))
        availability = VERIFIED/'field-availability.json'
        if availability.exists():
            data['fieldAvailability'] = json.loads(availability.read_text(encoding='utf-8'))
        _save_dataset(data)


def read_available(target, include_evidence=False):
    date.fromisoformat(target)
    from .akshare_read_model import read_dataset
    data = read_dataset(target)
    if not include_evidence:
        for bond in data['bonds']:
            bond['fieldSources'] = {}
            bond.pop('fieldObservations', None)
    return data


def read_available_bond(target, code):
    from .akshare_read_model import bond
    return bond(target, code)


def recalculate_saved(target, archive=True):
    """Rebuild a saved date from local evidence, archiving the previous rules.

    No network access. Stop old-version collectors before upgrading rules.
    """
    with dataset_lock():
        previous = read_available(target, True)
        if not previous['bonds'] or previous['rulesVersion'] == RULES['version']:
            return dict(evaluationDate=target, changed=False, counts=previous['counts'])
        details = _detail_cache()
        saved_metrics = load_saved_metrics(target)
        trades, trade_sources, conflicts = cached_trade_observations(target)
        benchmarks = previous.get('benchmarks') or {}
        benchmark_evidence = {}
        for key in ('indexYieldPct', 'indexDurationYears', 'curveYieldPct'):
            source = next((values[0] for row in previous['bonds']
                           if (values := row.get('fieldSources', {}).get(key))), None)
            if source:
                benchmark_evidence[key] = _evidence_from_field(source)
        bonds = []
        for old in previous['bonds']:
            code = old['code']
            if _code(code) is None:
                row = deepcopy(old)
                row.update(duration=None, termYears=None)
                bonds.append(row)
                continue
            detail, evidence = details.get(code, (None, None))
            if detail is None:
                detail = dict(bondCode=code.removesuffix('.IB'), bondType='地方政府债',
                              bondName=old.get('name'), entyFullName=old.get('issuer'),
                              bondFullName=old.get('name'), issueDate=old.get('issueDate'))
                source = next(iter(old.get('fieldSources', {}).get('code') or []), {})
                evidence = old.get('catalogSource') or _evidence_from_field(source)
            observation, observation_evidence = select_trade_observation(
                old, trades.get(code), trade_sources.get(code), target, conflicted=code in conflicts)
            row = normalize_bond(detail, target, observation, benchmarks, evidence,
                                 observation_evidence, _benchmark_sources_for_bond(old, benchmark_evidence),
                                 saved_metrics.get(code, []))
            for field in ('staticSyncStatus', 'catalogSource', 'syncJobId', 'fieldObservations'):
                if field in old:
                    row[field] = old[field]
            bonds.append(row)
        provenance = dict(previous.get('provenance') or {})
        provenance.update(recalculatedAt=store.now(), previousRulesVersion=previous['rulesVersion'],
                          recalculationOrigin='saved_local_evidence')
        dataset = _dataset(target, bonds, benchmarks, provenance, previous.get('warnings'))
        if previous.get('fullSync'):
            dataset['fullSync'] = dict(previous['fullSync'], marketEligible=dataset['counts']['eligible'],
                                       groupingBasis='modified_duration', rulesVersion=RULES['version'])
        if archive:
            with store.connection() as db:
                db.execute('CREATE TABLE IF NOT EXISTS akshare_dataset_archive ('
                           'id TEXT PRIMARY KEY, target_date TEXT NOT NULL, rules_version TEXT NOT NULL,'
                           'archived_at TEXT NOT NULL, payload TEXT NOT NULL)')
                db.execute('INSERT INTO akshare_dataset_archive VALUES (?,?,?,?,?)',
                           (uuid.uuid4().hex, target, previous['rulesVersion'], store.now(), _dump(previous)))
        _save_dataset(dataset)
        with store.connection() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='akshare_sync_jobs'").fetchone()
            if exists:
                rows = db.execute('SELECT job_id,payload FROM akshare_sync_jobs WHERE target_date=?', (target,)).fetchall()
                for saved in rows:
                    job = json.loads(saved['payload'])
                    job.update(marketEligible=dataset['counts']['eligible'], groupingBasis='modified_duration',
                               rulesVersion=RULES['version'], updatedAt=store.now())
                    db.execute('UPDATE akshare_sync_jobs SET payload=? WHERE job_id=?', (_dump(job), saved['job_id']))
        return dict(evaluationDate=target, changed=True, previousRulesVersion=previous['rulesVersion'],
                    rulesVersion=RULES['version'], before=previous['counts'], counts=dataset['counts'])


def _evidence_from_field(source):
    return dict(requestId=source.get('requestId'), runId=source.get('sessionId'),
                function=source.get('sourceFunction'), retrievedAt=source.get('retrievedAt'),
                executionMode=source.get('executionMode'), evidence=source.get('evidence', []),
                sourceDate=source.get('sourceDate'))


def _benchmark_sources_for_bond(bond, fallback, keys=('indexYieldPct', 'indexDurationYears', 'curveYieldPct')):
    evidence = dict(fallback)
    for key in keys:
        sources = (bond or {}).get('fieldSources', {}).get(key)
        if sources:
            evidence[key] = dict(_evidence_from_field(sources[0]), reusedSameDate=True)
    return evidence


def recalculate_stale_datasets():
    stale = _read_rows("SELECT target_date FROM akshare_datasets WHERE json_extract(payload,'$.rulesVersion') IS NOT ?",
                       (RULES['version'],))
    return [recalculate_saved(row['target_date']) for row in stale]


def available_dates():
    return [row['target_date'] for row in _read_rows('SELECT target_date FROM akshare_datasets ORDER BY target_date DESC')]


def query_detail(query_id):
    """Return recorded public query evidence without reading Wind lineage."""
    rows = _read_rows('SELECT payload FROM akshare_queries WHERE id=?', (query_id,))
    if rows:
        return json.loads(rows[0]['payload'])
    # Imported evidence predates runtime query IDs; expose its saved field-level
    # attribution without pretending it was freshly fetched by this application.
    if query_id.startswith('akshare-verified-'):
        observations = []
        for target in available_dates():
            for bond in read_available(target, True)['bonds']:
                observations.extend(item for fields in bond['fieldSources'].values()
                                    for item in fields if item['requestId'] == query_id)
        if observations:
            return dict(requestId=query_id, source='akshare', origin='verified_import',
                        dictionaryUrl=DOC, observations=observations)
    return None


def read_day(target):
    date.fromisoformat(target)
    runs = _read_rows('SELECT payload FROM job_runs WHERE target_date=? ORDER BY rowid DESC', (target,))
    latest = next((run for row in runs if (run := json.loads(row['payload'])).get('source') == 'akshare'), None)
    return dict(evaluationDate=target, source='akshare', dataState='pending', snapshot=None,
                latestAttempt=latest, yieldDefinition=YIELD_DEFINITION)


def status():
    try:
        version = importlib.metadata.version('akshare')
    except importlib.metadata.PackageNotFoundError:
        version = None
    dates = available_dates()
    from .akshare_read_model import summary
    latest = summary(dates[0]) if dates else None
    return dict(installed=version is not None, available=version is not None,
                version=version, configured=version is not None,
                availableDates=dates, dates=dates, latestDate=dates[0] if dates else None,
                cachedBonds=latest['counts']['bonds'] if latest else 0,
                cacheOrigin=latest.get('provenance', {}).get('origin') if latest else None,
                dictionaryUrl=DOC, batchLimit=MAX_DETAIL_QUERIES, requiresKey=False,
                yieldDefinition=YIELD_DEFINITION,
                message='公开数据接口无需 Key；按查询日保留样本和替代指标的来源。' if version else '实时更新需要安装 requirements.txt 中的 AKShare；已有缓存仍可读取。')


class QueryRecorder:
    """Record exact SDK arguments and public response bodies with time bounds."""
    def __init__(self, run, timeout_seconds=240, on_response=None, before_request=None):
        self.run = run
        self.queries = []
        self.timeout_seconds = timeout_seconds
        self.deadline = time.monotonic()+timeout_seconds
        self.on_response = on_response
        self.before_request = before_request

    def query(self, name, arguments, columns, compatibility=False, resolved_lookup=None):
        import akshare as ak
        import requests
        function = getattr(ak, name)
        inspect.signature(function).bind(**arguments)
        if resolved_lookup is not None:
            if (not compatibility or name != 'bond_info_detail_cm' or len(resolved_lookup) != 1 or
                resolved_lookup[0].get('债券简称') != arguments.get('symbol') or
                resolved_lookup[0].get('债券类型') != '地方政府债' or
                _code(resolved_lookup[0].get('债券代码')) is None or
                not isinstance(resolved_lookup[0].get('查询代码'), str) or not resolved_lookup[0]['查询代码'].strip()):
                raise ValueError('债券详情必须使用唯一匹配的已验证目录身份')
        entry = dict(requestId='akshare-'+str(uuid.uuid4()), runId=self.run['runId'],
                     function=name, arguments=arguments, dictionaryUrl=DOC,
                     retrievedAt=store.now(), executionMode='compatibility_adapter' if compatibility else 'documented_sdk',
                     akshareVersion=ak.__version__, expectedColumns=columns, responses=[])
        if resolved_lookup is not None:
            entry['resolvedLookup'] = resolved_lookup
            entry['compatibilityReason'] = '使用已验证完整目录中的唯一查询代码，避免详情接口重复检索和缺失债券类型参数'
        original_send = requests.Session.send
        original_init = requests.Session.__init__

        def init_session(session):
            original_init(session)
            session.trust_env = False

        def send(session, request, **kwargs):
            if time.monotonic() > self.deadline:
                raise TimeoutError(f'本批 AKShare 查询达到 {self.timeout_seconds} 秒时限')
            parsed = urlsplit(request.url)
            if parsed.hostname not in {'www.chinamoney.com.cn', 'yield.chinabond.com.cn'}:
                raise ValueError('AKShare 返回了不在公开债券查询范围内的地址')
            if self.before_request:
                self.before_request(request)
            kwargs.update(timeout=(5, 15), verify=True)
            response = original_send(session, request, **kwargs)
            try:
                body = response.json()
            except ValueError:
                body = response.text[:100000]
            response_record=dict(url=request.url, status=response.status_code, body=body,
                                 requestBody=request.body.decode('utf-8',errors='replace') if isinstance(request.body,bytes) else request.body)
            entry['responses'].append(response_record)
            if self.on_response:
                self.on_response(response_record)
            response.raise_for_status()
            return response

        try:
            with _QUERY_LOCK:
                requests.Session.send, requests.Session.__init__ = send, init_session
                try:
                    if hasattr(function, 'cache_clear'):
                        function.cache_clear()
                    if compatibility:
                        module = importlib.import_module('akshare.bond.bond_info_cm')
                        original_lookup = module.bond_info_cm
                        def typed_lookup(*args, **kwargs):
                            if resolved_lookup is not None:
                                if args or kwargs.get('bond_name') != arguments['symbol']:
                                    raise ValueError('详情接口内部查询与已验证目录身份不匹配')
                                import pandas as pd
                                return pd.DataFrame(resolved_lookup)
                            kwargs.setdefault('bond_type', '地方政府债')
                            return original_lookup(*args, **kwargs)
                        module.bond_info_cm = typed_lookup
                        try:
                            frame = function(**arguments)
                        finally:
                            module.bond_info_cm = original_lookup
                    else:
                        frame = function(**arguments)
                finally:
                    requests.Session.send, requests.Session.__init__ = original_send, original_init
            if not set(columns) <= set(frame.columns):
                raise ValueError(f'{name} 返回列与数据字典不符')
            records = json.loads(frame.to_json(orient='records', date_format='iso', force_ascii=False))
            entry.update(status='succeeded', rows=len(records), columns=list(frame.columns), records=records)
            return records, entry
        except Exception as exc:
            entry.update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:400]}')
            raise
        finally:
            self.queries.append(entry)
            with store.connection() as db:
                db.execute('INSERT INTO akshare_queries VALUES (?,?,?,?,?,?)',
                           (entry['requestId'], self.run['runId'], self.run['targetDate'],
                            entry['retrievedAt'], name, _dump(entry)))


def _observation_time(value):
    """Compare actual market timestamps in China time, including legacy dates."""
    if not isinstance(value, str):
        return None
    try:
        observed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        china = timezone(timedelta(hours=8))
        return observed.replace(tzinfo=china) if observed.tzinfo is None else observed.astimezone(china)
    except ValueError:
        return None


def _trade_rows(entry, target, official_only=False):
    for response in entry.get('responses', []):
        if official_only:
            url = urlsplit(response.get('url', ''))
            if (response.get('status') != 200 or url.scheme != 'https' or
                    url.hostname != 'www.chinamoney.com.cn' or url.path != '/ags/ms/cm-u-md-bond/CbtPri'):
                continue
        body = response.get('body')
        if not isinstance(body, dict):
            continue
        rows = body.get('records', [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            observed = _observation_time(row.get('showDate'))
            code = _code(row.get('bondcode'))
            if code and observed and observed.date().isoformat() == target and _numeric(row.get('dmiLatestContraRate')) is not None:
                yield code, observed, row


def _trades_from(entry, target):
    groups = defaultdict(list)
    for code, observed, row in _trade_rows(entry, target):
        groups[code].append((observed, row))
    trades, conflicts = {}, []
    for code, rows in groups.items():
        newest = max(observed for observed, _ in rows)
        latest = [row for observed, row in rows if observed == newest]
        if len({_trade_values(row) for row in latest}) > 1:
            conflicts.append(code)
        else:
            trades[code] = latest[0]
    return trades, conflicts


def _trade_values(row):
    return tuple(number(value) if _numeric(value) is not None else None
                 for value in (row.get('dmiLatestContraRate'), row.get('dmiLatestRate')))


def cached_trade_observations(target):
    """Replay successful AKShare public responses without querying a data source.

    Requested dates do not establish observation dates. Only each raw row's
    showDate establishes eligibility, even when it was acquired by another run.
    A newer observation replaces an older one; contradictory values at the
    latest observation time remain quarantined across acquisition runs.
    """
    date.fromisoformat(target)
    groups = defaultdict(list)
    for saved in _read_rows("SELECT payload FROM akshare_queries WHERE function_name='bond_spot_deal' ORDER BY rowid"):
        entry = json.loads(saved['payload'])
        if (entry.get('status') != 'succeeded' or entry.get('function') != 'bond_spot_deal'
                or entry.get('source', 'akshare') != 'akshare'):
            continue
        evidence = {key: entry.get(key) for key in ('requestId', 'runId', 'function', 'arguments',
                    'retrievedAt', 'executionMode', 'dictionaryUrl')}
        evidence['reusedSameDate'] = True
        for code, observed, row in _trade_rows(entry, target, official_only=True):
            groups[code].append((observed, row, evidence))
    trades, sources, conflicts = {}, {}, set()
    for code, rows in groups.items():
        newest = max(observed for observed, _, _ in rows)
        latest = [(row, evidence) for observed, row, evidence in rows if observed == newest]
        values = {_trade_values(row) for row, _ in latest}
        if len(values) != 1:
            conflicts.add(code)
            continue
        # Identical copies retain the first stored provenance for this timestamp.
        trades[code], sources[code] = latest[0]
    return trades, sources, conflicts


def select_trade_observation(old_bond, trade, evidence, target, conflicted=False):
    """Choose a dated individual observation, preserving its original evidence."""
    if conflicted:
        return None, {}
    old = old_bond or {}
    old_code = _code(old.get('code'))
    old_time = _observation_time(old.get('tradeObservedAt') or target)
    independent_trade = 'tradeYieldPct' in old
    old_yield = old.get('tradeYieldPct') if independent_trade else old.get('yieldPct')
    old_valid = (old_code is not None and old.get('source', 'akshare') == 'akshare' and
                 (bool(old.get('tradeObservedAt')) if independent_trade else
                  old.get('yieldDate') == target and old.get('yieldPriceBasis') == 'latest_trade' and old.get('yieldMetric', 'ytm') == 'ytm') and
                 _numeric(old_yield) is not None and old_time is not None and old_time.date().isoformat() == target)
    previous, old_evidence = None, {}
    if old_valid:
        previous = dict(bondcode=old_code.removesuffix('.IB'), showDate=old_time.isoformat(),
                        dmiLatestContraRate=old_yield, dmiLatestRate=old.get('tradeNetPrice'))
        fields = old.get('fieldSources', {}).get('tradeYieldPct' if independent_trade else 'yieldPct', [])
        source = fields[0] if fields else {}
        old_evidence = dict(requestId=source.get('requestId'), runId=source.get('sessionId'),
                            function=source.get('sourceFunction'), retrievedAt=source.get('retrievedAt'),
                            executionMode=source.get('executionMode', 'documented_sdk'),
                            evidence=source.get('evidence', []), reusedSameDate=True)
    incoming_time = _observation_time(trade.get('showDate')) if trade else None
    incoming_code = _code(trade.get('bondcode')) if trade else None
    incoming_valid = (incoming_code is not None and (old_code is None or incoming_code == old_code) and
                      incoming_time is not None and incoming_time.date().isoformat() == target and
                      _numeric(trade.get('dmiLatestContraRate')) is not None)
    if not incoming_valid or (old_valid and old_time > incoming_time):
        return previous, old_evidence
    if old_valid and old_time == incoming_time:
        if number(old_yield) != number(trade['dmiLatestContraRate']):
            return None, {}
        old_price, new_price = _numeric(old.get('tradeNetPrice')), _numeric(trade.get('dmiLatestRate'))
        if old_price is not None and new_price is not None and number(old_price) != number(new_price):
            return None, {}
    return trade, evidence or {}


def collect(run):
    """Refresh a bounded public sample; commit only a useful dated result."""
    if run.get('source') != 'akshare':
        raise ValueError('AKShare 收集器拒绝处理其他来源任务')
    target = date.fromisoformat(run['targetDate']).isoformat()
    initialize()
    previous = read_available(target, True)
    recorder = QueryRecorder(run)
    warnings = []
    _, spot_entry = recorder.query('bond_spot_deal', {}, ['债券简称', '最新收益率', '成交净价'])
    trades, conflicts = _trades_from(spot_entry, target)
    if conflicts:
        warnings.append(f'{len(conflicts)} 只债券同一时点返回值冲突，本批未采用。')
    benchmarks, benchmark_evidence = dict(indexYieldPct=None, indexDurationYears=None, curveNodes=[]), {}
    specifications = [
        ('indexYieldPct', 'bond_index_general_cbond', dict(index_category='地方政府债指数', indicator='平均市值法到期收益率', period='总值'), ['date', 'value']),
        ('indexDurationYears', 'bond_index_general_cbond', dict(index_category='地方政府债指数', indicator='平均市值法久期', period='总值'), ['date', 'value']),
        ('curveYieldPct', 'bond_china_close_return', dict(symbol='地方政府债(AAA)', period='1', start_date=target.replace('-', ''), end_date=target.replace('-', '')), ['日期', '期限', '到期收益率']),
    ]
    for key, name, arguments, columns in specifications:
        try:
            rows, entry = recorder.query(name, arguments, columns)
            if key == 'curveYieldPct':
                points = [(number(row['期限']), number(row['到期收益率'])) for row in rows
                          if _date(row['日期']) == target and _numeric(row['期限']) is not None and _numeric(row['到期收益率']) is not None]
                if len({x for x, _ in points}) != len(points):
                    raise ValueError('同日期曲线期限节点重复')
                benchmarks['curveNodes'] = [[str(x), str(y)] for x, y in sorted(points)]
            else:
                values = {_numeric(row['value']) for row in rows if _date(row['date']) == target}
                values.discard(None)
                if len(values) > 1:
                    raise ValueError('同日期指数指标冲突')
                benchmarks[key] = next(iter(values), None)
            benchmark_evidence[key] = dict(entry, sourceDate=target)
        except Exception as exc:
            warnings.append(f'{name} 未取得目标日指标：{type(exc).__name__}: {str(exc)[:120]}')
    reused_benchmarks = []
    for key, benchmark_key in (('indexYieldPct', 'indexYieldPct'),
                               ('indexDurationYears', 'indexDurationYears'),
                               ('curveYieldPct', 'curveNodes')):
        old_value = previous.get('benchmarks', {}).get(benchmark_key)
        if not benchmarks[benchmark_key] and old_value:
            benchmarks[benchmark_key] = deepcopy(old_value)
            old_source = next((values[0] for bond in previous['bonds']
                               if (values := bond.get('fieldSources', {}).get(key))), None)
            if old_source:
                benchmark_evidence[key] = dict(requestId=old_source['requestId'],
                    runId=old_source.get('sessionId'), function=old_source.get('sourceFunction'),
                    retrievedAt=old_source.get('retrievedAt'),
                    executionMode=old_source.get('executionMode', 'documented_sdk'),
                    evidence=old_source.get('evidence', []), reusedSameDate=True,
                    sourceDate=old_source.get('sourceDate'))
            reused_benchmarks.append(key)
            warnings.append(f'{key} 本批未取得目标日指标，沿用 {target} 已保存值与原采集来源。')
    if not trades and all(not value for value in benchmarks.values()):
        raise ValueError('AKShare 未返回目标日成交或基准数据；保留已有缓存。即时接口不能查询任意历史成交。')
    details = _detail_cache()
    candidates = [code for code, trade in trades.items() if code not in details and
                  _LOCAL_NAME.fullmatch(str(trade.get('abdAssetEncdShrtDesc', '')))]
    # Useful standard-tenor rows first; this remains a disclosed partial sample.
    def candidate_order(code):
        match = re.fullmatch(r'(\d+(?:\.\d+)?)Y', str(trades[code].get('termToMaturity', '')))
        term = int(number(match[1]).quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if match else None
        return (term not in TERMS, code)
    candidates.sort(key=candidate_order)
    for index, code in enumerate(candidates[:MAX_DETAIL_QUERIES]):
        if time.monotonic() > recorder.deadline:
            warnings.append('达到本批查询时限，保留已完成的公开样本。')
            break
        try:
            store.update_run(run['runId'], phase=f'AKShare 查询地方债详情 {index+1}/{min(len(candidates), MAX_DETAIL_QUERIES)}')
            matches, _ = recorder.query('bond_info_cm', dict(bond_code=code.removesuffix('.IB'), bond_type='地方政府债'),
                                        ['债券简称', '债券代码', '债券类型', '查询代码'])
            exact = [row for row in matches if _code(row['债券代码']) == code and row['债券类型'] == '地方政府债']
            if len(exact) != 1:
                raise ValueError('未取得唯一的地方政府债代码匹配')
            rows, entry = recorder.query('bond_info_detail_cm', dict(symbol=exact[0]['债券简称']), ['name', 'value'], compatibility=True)
            detail = {row['name']: row['value'] for row in rows}
            _save_detail(code, detail, entry)
            details[code] = detail, entry
        except Exception as exc:
            warnings.append(f'{code} 详情未取得：{type(exc).__name__}: {str(exc)[:100]}')
    with dataset_lock():
        # Full-catalog collection may have added non-traded bonds while these
        # network requests were in flight. Merge with the latest committed day.
        latest = read_available(target, True)
        details = _detail_cache()
        saved_metrics = load_saved_metrics(target)
        for key, benchmark_key in (('indexYieldPct','indexYieldPct'),
                                    ('indexDurationYears','indexDurationYears'),
                                    ('curveYieldPct','curveNodes')):
            latest_value=latest.get('benchmarks',{}).get(benchmark_key)
            if (key in reused_benchmarks or not benchmarks[benchmark_key]) and latest_value:
                benchmarks[benchmark_key]=deepcopy(latest_value)
                old_source=next((values[0] for bond in latest['bonds']
                                 if (values:=bond.get('fieldSources',{}).get(key))),None)
                if old_source:
                    benchmark_evidence[key]=dict(requestId=old_source['requestId'],
                        runId=old_source.get('sessionId'),function=old_source.get('sourceFunction'),
                        retrievedAt=old_source.get('retrievedAt'),executionMode=old_source.get('executionMode','documented_sdk'),
                        evidence=old_source.get('evidence',[]),reusedSameDate=True,sourceDate=old_source.get('sourceDate'))
        bonds = {bond['code']: bond for bond in latest['bonds']}
        cached_trades, cached_sources, cached_conflicts = cached_trade_observations(target)
        # Live responses are already persisted by QueryRecorder. The explicit
        # fallback also permits callers with an in-memory recorder to use them.
        disputed = cached_conflicts | (set(conflicts) - set(cached_trades))
        for code, (detail, detail_ev) in details.items():
            old = bonds.get(code)
            observation, observation_evidence = select_trade_observation(
                old, cached_trades.get(code, trades.get(code)), cached_sources.get(code, spot_entry),
                target, conflicted=code in disputed)
            fresh = normalize_bond(detail, target, observation, benchmarks, detail_ev,
                                   observation_evidence, _benchmark_sources_for_bond(old, benchmark_evidence, reused_benchmarks),
                                   saved_metrics.get(code, []))
            if old:
                for field in ('staticSyncStatus','catalogSource','syncJobId'):
                    if field in old:
                        fresh[field]=old[field]
            bonds[code] = fresh
        # Pending catalog rows can already have independent market evidence.
        # Refresh only quote-affected identities lacking a cached detail; do not
        # wait for full sync or normalize the entire catalog a second time.
        affected_pending = (set(cached_trades) | set(trades) | disputed | set(saved_metrics)) - set(details)
        for code in affected_pending:
            old = bonds.get(code)
            if (not old or old.get('source') != 'akshare' or not old.get('catalogSource')
                    or old.get('staticSyncStatus') not in ('pending', 'running', 'failed')):
                continue
            observation, observation_evidence = select_trade_observation(
                old, cached_trades.get(code, trades.get(code)), cached_sources.get(code, spot_entry),
                target, conflicted=code in disputed)
            catalog_detail = dict(bondCode=code.removesuffix('.IB'), bondName=old.get('name'),
                                  bondType='地方政府债', entyFullName=old.get('issuer'),
                                  issueDate=old.get('issueDate'))
            fresh = normalize_bond(catalog_detail, target, observation, benchmarks,
                                   old['catalogSource'], observation_evidence, benchmark_evidence, saved_metrics.get(code, []))
            for field in ('staticSyncStatus', 'catalogSource', 'syncJobId'):
                if field in old:
                    fresh[field] = old[field]
            fresh['reason'] = ('基本信息同步失败，仍保留名单身份' if old['staticSyncStatus'] == 'failed'
                               else '已取得地方政府债名单，基本信息等待同步')
            bonds[code] = fresh
        if not bonds:
            raise ValueError('本批未取得可验证身份的地方政府债样本；已保存接口证据，原缓存保留。')
        provenance = dict(source='akshare', complete=False, origin='live_query', collectedAt=store.now(),
                          runId=run['runId'], dictionaryUrl=DOC, queryCount=len(recorder.queries),
                          batchLimit=MAX_DETAIL_QUERIES, candidateCount=len(candidates),
                          tradedBondCount=len(trades), detailCacheCount=len(details),
                          reusedBenchmarkFields=reused_benchmarks,
                          requestIds=[entry['requestId'] for entry in recorder.queries],
                          scope='公开成交中识别的地方政府债及已缓存详情；每批最多新增20只详情')
        data = _dataset(target, list(bonds.values()), benchmarks, provenance, warnings)
        if latest.get('fullSync'):
            data['fullSync']=latest['fullSync']
        _save_dataset(data)
    return dict(counts=data['counts'], message=f"AKShare 工作视图已保存 {len(bonds)} 只地方债样本，其中 {data['counts']['eligible']} 只可按收益率与久期优先规则汇总；采用来源已逐券标注。",
                provenance=provenance)
