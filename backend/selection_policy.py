"""Pure selection rules for dated bond yields and duration references.

The caller supplies normalized observations with explicit instrument, date and
unit. An index can supply a duration reference only; it cannot supply a bond's
yield or masquerade as an observed individual duration.
"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, DecimalException

from .bond_duration import DURATION_BUCKETS, duration_bucket


_INDEX_FUNCTION = 'bond_index_general_cbond'
_PROXY_FUNCTIONS = {_INDEX_FUNCTION, 'bond_china_yield', 'bond_china_close_return'}
_YTM_PRICE_BASIS_GROUPS = (('latest_trade',), ('close', 'closing_price'))
_PROXY_QUALITIES = {'proxy', 'substitute', 'index', 'index_reference', 'curve',
                    'benchmark', '替代', '指数', '曲线'}


def _date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _number(value, *, positive=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
    except (DecimalException, ValueError):
        return None
    if not result.is_finite() or (positive and result <= 0):
        return None
    return result


def _text(value):
    return format(value, 'f')


def _sources(record):
    sources = record.get('fieldSources') or []
    return [deepcopy(item) for item in sources if isinstance(item, Mapping)] if isinstance(sources, list) else []


def _proxy(record):
    flag = record.get('isProxy')
    if flag is not None and str(flag).strip().lower() not in {'', 'false', 'no', '0', '否'}:
        return True
    grain = record.get('entityGrain')
    if grain is not None and grain != 'bond':
        return True
    if str(record.get('evidenceQuality') or '').strip().lower() in _PROXY_QUALITIES:
        return True
    return record.get('sourceFunction') in _PROXY_FUNCTIONS


def _bond_observation(record, code, target, metric):
    if (not isinstance(code, str) or not code.strip() or not isinstance(record, Mapping)
            or record.get('code') != code or not target):
        return None
    if _date(record.get('date')) != target or record.get('metric') != metric:
        return None
    if record.get('unit') != ('年' if metric == 'modified_duration' else '%'):
        return None
    if _proxy(record) or any(_proxy(source) for source in _sources(record)):
        return None
    return _number(record.get('value'), positive=metric == 'modified_duration')


def _resolve(code, target, observations, metric):
    candidates = []
    for record in observations:
        number = _bond_observation(record, code, target, metric)
        if number is not None:
            candidates.append((number, record))
    if not candidates:
        return None, 'missing'
    if len({number for number, _ in candidates}) != 1:
        return None, 'conflict'
    first = candidates[0][1]
    evidence, source_names = [], []
    for _, candidate in candidates:
        for entry in _sources(candidate):
            if entry not in evidence:
                evidence.append(entry)
        source = candidate.get('source')
        if isinstance(source, str) and source and source not in source_names:
            source_names.append(source)
    return dict(value=_text(candidates[0][0]), sourceDate=target,
                source='；'.join(source_names), fieldSources=evidence,
                priceBasis=first.get('priceBasis') or 'latest_trade'), None


def _resolve_ytm(code, target, records):
    conflicts = []
    for basis_group in _YTM_PRICE_BASIS_GROUPS:
        candidates = [record for record in records if isinstance(record, Mapping)
                      and (record.get('priceBasis') or 'latest_trade') in basis_group]
        resolved, issue = _resolve(code, target, candidates, 'ytm')
        if resolved:
            return resolved, 'latest_trade_conflict' if 'latest_trade' in conflicts else None
        if issue == 'conflict':
            conflicts.append(basis_group[0])
    return None, 'conflict' if conflicts else 'missing'


def select_yield(code, target, observations, trade_candidate=None):
    """Prefer same-day individual ChinaBond valuation yield, then bond YTM.

    YTM uses latest trade before close, checking conflicts within each price
    basis. Zero and negative finite yields remain valid observations.
    """
    target = _date(target)
    records = list(observations or [])
    if isinstance(trade_candidate, Mapping):
        records.append(trade_candidate)
    valuation, valuation_issue = _resolve(code, target, records, 'chinabond_valuation')
    traded, traded_issue = _resolve_ytm(code, target, records)
    result = dict(value=None, kind='unavailable', label='收益率不可用',
                  reason='缺少目标日的个券中债估值收益率和到期收益率',
                  sourceDate=None, source='', fieldSources=[],
                  yieldMetric=None, yieldPriceBasis=None,
                  valuationValue=valuation['value'] if valuation else None,
                  tradeValue=traded['value'] if traded else None)
    if valuation:
        result.update({key: valuation[key] for key in ('value', 'sourceDate', 'source', 'fieldSources')})
        result.update(kind='chinabond_valuation', label='中债估值收益率',
                      reason='优先采用目标日的个券中债估值收益率',
                      yieldMetric='chinabond_valuation', yieldPriceBasis='valuation')
    elif traded:
        result.update({key: traded[key] for key in ('value', 'sourceDate', 'source', 'fieldSources')})
        reason = ('目标日的个券中债估值收益率存在冲突' if valuation_issue == 'conflict'
                  else '未取得目标日的个券中债估值收益率')
        if traded_issue == 'latest_trade_conflict':
            reason += '；最新成交到期收益率存在冲突，回退到收盘口径'
        result.update(kind='ytm', label='到期收益率', reason=f'{reason}，采用个券到期收益率',
                      yieldMetric='ytm', yieldPriceBasis=traded['priceBasis'])
    elif valuation_issue == 'conflict' or traded_issue == 'conflict':
        result['reason'] = '同日同券的收益率来源存在冲突，且无可用的其他个券收益率'
    if not target:
        result['reason'] = '目标日期无效，要求 YYYY-MM-DD'
    return result


def _valid_calculation(calculation, code, target):
    if not isinstance(calculation, Mapping) or not target:
        return False
    inputs = calculation.get('inputs')
    if not isinstance(inputs, Mapping) or _date(inputs.get('targetDate')) != target:
        return False
    if inputs.get('code') is not None and inputs['code'] != code:
        return False
    return (calculation.get('status') in {'calculated', 'estimated'}
            and _number(calculation.get('modifiedYears'), positive=True) is not None)


def _valid_index(candidate, target):
    if not isinstance(candidate, Mapping) or not target:
        return False
    if candidate.get('entityGrain') != 'index' or candidate.get('metric') != 'index_duration':
        return False
    if _date(candidate.get('date')) != target or _number(candidate.get('value'), positive=True) is None:
        return False
    # The normalized candidate must carry actual provenance from this endpoint.
    return any(source.get('sourceFunction') == _INDEX_FUNCTION for source in _sources(candidate))


def select_duration(code, target, observations, calculation, index_candidate=None):
    """Individual observed duration -> computed duration -> dated index proxy.

    A proxy is explicitly returned as ``index_reference`` even when a known
    cash-flow constraint prevented the individual calculation. It never becomes
    an individual duration observation merely because it permits grouping.
    """
    target = _date(target)
    direct, direct_issue = _resolve(code, target, observations or [], 'modified_duration')
    result = dict(value=None, kind='unavailable', label='久期不可用',
                  reason='缺少目标日的个券久期、可用计算结果及地方债指数久期参考',
                  sourceDate=None, source='', fieldSources=[],
                  bucketYears=None, individualValue=None)
    if direct:
        result.update({key: direct[key] for key in ('value', 'sourceDate', 'source', 'fieldSources')})
        result.update(kind='direct', label='个券修正久期（直接值）',
                      reason='优先采用目标日的个券修正久期直接值',
                      bucketYears=duration_bucket(direct['value']), individualValue=direct['value'])
        return result
    if _valid_calculation(calculation, code, target):
        value = _text(_number(calculation['modifiedYears'], positive=True))
        kind = calculation['status']
        bucket = calculation.get('bucketYears')
        # The calculator buckets before displaying its rounded duration value.
        if isinstance(bucket, bool) or bucket not in DURATION_BUCKETS:
            bucket = duration_bucket(value)
        reason = calculation.get('reason') or '使用个券现金流计算的修正久期'
        if direct_issue == 'conflict':
            reason = f'个券久期直接值存在冲突；{reason}'
        result.update(value=value, kind=kind,
                      label='个券修正久期（估算）' if kind == 'estimated' else '个券修正久期（计算）',
                      reason=reason, sourceDate=target,
                      source=calculation.get('source') or '本地现金流计算',
                      fieldSources=_sources(calculation), bucketYears=bucket,
                      individualValue=value)
        return result
    if _valid_index(index_candidate, target):
        value = _text(_number(index_candidate['value'], positive=True))
        reason = '个券久期直接值存在冲突' if direct_issue == 'conflict' else '未取得可用的个券直接久期'
        if isinstance(calculation, Mapping) and calculation.get('reason'):
            reason += f'；个券计算未采用：{calculation["reason"]}'
        reason += '；使用同日地方政府债指数久期作共同参考，不代表个券实际久期或利率风险'
        result.update(value=value, kind='index_reference', label='地方债指数久期参考',
                      reason=reason, sourceDate=target,
                      source=index_candidate.get('source') or '地方政府债指数',
                      fieldSources=_sources(index_candidate), bucketYears=duration_bucket(value))
    elif direct_issue == 'conflict':
        result['reason'] = '同日同券的直接久期存在冲突，且无可用计算结果或同日地方债指数久期参考'
    if not target:
        result['reason'] = '目标日期无效，要求 YYYY-MM-DD'
    return result
