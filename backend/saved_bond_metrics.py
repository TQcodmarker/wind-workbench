"""Read dated individual-bond metrics from immutable, already saved Wind replies.

This bridge does not invoke Wind, change the selected provider, or reuse the old
normalized projection (whose yield policy may differ). Conflicting valid cells
remain separate observations for the caller's selection policy.
"""
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from datetime import date

from . import storage as store
from .available_data import _response_rows
from .wind_mapping import CODE, PERCENT_UNITS, base_name, number
from .wind_mcp import WindError


_YIELDS = {
    '中债估值收益率': ('chinabond_valuation', 'valuation'),
    '中债估价收益率': ('chinabond_valuation', 'valuation'),
    '中债估值到期收益率': ('chinabond_valuation', 'valuation'),
    '中债估价到期收益率': ('chinabond_valuation', 'valuation'),
    '收盘价到期收益率': ('ytm', 'close'), '收盘到期收益率': ('ytm', 'close'),
    '到期收益率(收盘价)': ('ytm', 'close'), '到期收益率（收盘价）': ('ytm', 'close'),
    '收盘价收益率': ('ytm', 'close'), '收盘收益率': ('ytm', 'close'),
    '到期收益率': ('ytm', 'close'),
}
_DURATIONS = {
    '收盘价修正久期': ('modified_duration', 'close'),
    '基于净价的收盘价修正久期': ('modified_duration', 'close'),
    '中债估价修正久期': ('modified_duration', 'valuation'),
    '中债估值修正久期': ('modified_duration', 'valuation'),
}
_METRICS = {**_YIELDS, **_DURATIONS}
_IDENTITY_FIELDS = {'Wind代码', '债券代码类型', '主证券代码', '跨市场代码'}
_GENERIC_DATES = {'数据日期', '交易日期', '行情日期', '估值日期', '收益率数据日期',
                  '实际收益率日期', '收益率日期', '到期收益率日期', '收盘行情日期'}
_METRIC_DATES = {
    **{n + suffix: {spec} for n, spec in _METRICS.items() for suffix in ('时间', '日期')},
    '修正久期时间': {('modified_duration', 'close'), ('modified_duration', 'valuation')},
    '修正久期日期': {('modified_duration', 'close'), ('modified_duration', 'valuation')},
    '收盘价净价时间': {('ytm', 'close'), ('modified_duration', 'close')},
    '收盘价净价日期': {('ytm', 'close'), ('modified_duration', 'close')},
}
_DATE_PATTERN = re.compile(r'\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日')


def _date_value(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = _DATE_PATTERN.fullmatch(text)
    if not match:
        return None
    pieces = re.findall(r'\d+', text)
    try:
        return date(*(int(v) for v in pieces)).isoformat()
    except ValueError:
        return None


def _field(name, target):
    """Return an alias only when every explicit date in the header agrees."""
    dates = [_date_value(part) for part in _DATE_PATTERN.findall(name)]
    if dates and any(value != target for value in dates):
        return None, False
    # The existing parser recognizes ISO and unpadded Chinese dates. Normalize
    # other supported header spellings before applying its wrapper stripping.
    normalized = _DATE_PATTERN.sub(target, name)
    return base_name(normalized, target)


def _requests(target):
    if not store.DB.exists():
        return []
    with closing(sqlite3.connect(store.DB.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'source_sessions', 'source_requests'} <= tables:
            return []
        # A failed run can contain a useful complete response. The table parser
        # still rejects actual error envelopes or malformed data.
        return [dict(row) for row in db.execute('''
            SELECT r.id, r.session_id, r.response, s.target_date
              FROM source_requests r JOIN source_sessions s ON s.id=r.session_id
             WHERE r.method='tools/call' AND r.response IS NOT NULL AND s.target_date=?
             ORDER BY r.started_at, r.rowid''', (target,))]


def _source(cell, session_id, target, date_basis):
    return dict(deepcopy(cell), sessionId=session_id, source='wind', sourceDate=target,
                dateBasis=date_basis, entityGrain='bond', evidenceKind='raw_cell')


def _number(cell, metric):
    value = number(cell['value'])
    unit = str(cell.get('unit') or '').strip().lower()
    # Wind's named yield/duration fields use percent/years when no unit cell is
    # returned; explicit units must be recognized and converted, never guessed.
    if metric == 'modified_duration':
        if unit not in ('', '年', 'year', 'years') or value <= 0:
            raise ValueError('Invalid duration')
        return str(value), '年'
    if unit:
        factor = PERCENT_UNITS.get(unit)
        if factor is None:
            raise ValueError('Unknown yield unit')
        value *= number(factor)
    return str(value), '%'


def _metadata_agrees(cells, spec, target):
    metric, basis = spec
    for cell in cells:
        if cell['value'] is None or cell['value'] == '':
            continue
        name, _ = _field(cell['name'], target)
        value = str(cell['value']).strip().lower()
        if name in {'证券简称', '证券全称'} and any(mark in value for mark in ('指数', '收益率曲线')):
            return False
        if name in {'数据粒度', '实体粒度', '指标粒度', 'entitygrain'} and value not in {'bond', '个券', '债券'}:
            return False
        if name in {'数据类型', '数值类型', '数据性质', '来源性质', '取值性质', '数据来源', '指标来源',
                    '修正久期来源', '久期来源', '收益率来源'} and any(
                mark in value for mark in ('指数', '曲线', '替代', '参考', 'proxy', 'index', 'curve', '估算')):
            return False
        if name in {'收益率类型', '收益率指标'} and metric != 'modified_duration':
            allowed = {'ytm', '到期收益率', 'yield to maturity'} if metric == 'ytm' else {
                'chinabond_valuation', '中债估值收益率', '中债估价收益率', '中债估值到期收益率'}
            if value not in allowed:
                return False
        if name in {'收益率价格口径', '收益率价格基准', '价格口径'}:
            allowed = {'close', '收盘', '收盘价', '收盘价格'} if basis == 'close' else {'valuation', '估值', '中债估值', '中债估价'}
            if value not in allowed:
                return False
    return True


def load_saved_metrics(target):
    """Return valid dated IB-bond observations; a missing database yields {}.

    Exact IB codes are used directly. An exchange listing can map to an IB code
    only through unambiguous primary/cross-market cells in these saved replies.
    Source-specific observation dates override dated column titles.
    """
    date.fromisoformat(target)
    rows = []
    alias_candidates = defaultdict(set)
    alias_evidence = defaultdict(list)
    for request in _requests(target):
        try:
            parsed = _response_rows(request)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, WindError):
            continue
        for code, cells in parsed:
            identities = [cell for cell in cells if _field(cell['name'], target)[0] in _IDENTITY_FIELDS
                          and isinstance(cell['value'], str) and CODE.fullmatch(cell['value'])]
            ib_codes = {cell['value'] for cell in identities if cell['value'].endswith('.IB')}
            if len(ib_codes) == 1:
                ib_code = next(iter(ib_codes))
                for cell in identities:
                    if not cell['value'].endswith('.IB'):
                        alias_candidates[cell['value']].add(ib_code)
                        alias_evidence[(cell['value'], ib_code)].extend(
                            _source(item, request['session_id'], target, 'identity') for item in identities)
            rows.append((code, cells, request['session_id']))

    def canonical(code):
        if code.endswith('.IB'):
            return code
        candidates = alias_candidates.get(code, set())
        return next(iter(candidates)) if len(candidates) == 1 else None

    # Some Wind replies contain observation timestamps in a separate table or
    # request. Apply explicitly named timestamps to matching metrics across the
    # same bond's saved target-day responses, so a stale quote is not relabeled.
    reported_dates = defaultdict(list)
    for code, cells, session_id in rows:
        code = canonical(code)
        if code is None:
            continue
        for cell in cells:
            name, _ = _field(cell['name'], target)
            if cell['value'] is not None and cell['value'] != '':
                for spec in _METRIC_DATES.get(name, ()):
                    reported_dates[(code, spec)].append((cell, session_id))

    result = defaultdict(list)
    for original_code, cells, session_id in rows:
        code = canonical(original_code)
        if code is None:
            continue
        for cell in cells:
            name, dated = _field(cell['name'], target)
            spec = _METRICS.get(name)
            if spec is None or cell['value'] is None or cell['value'] == '' or not _metadata_agrees(cells, spec, target):
                continue
            dates = list(reported_dates.get((code, spec), ()))
            dates.extend((other, session_id) for other in cells
                         if _field(other['name'], target)[0] in _GENERIC_DATES
                         and other['value'] is not None and other['value'] != '')
            if any(_date_value(other['value']) != target for other, _ in dates):
                continue
            if not dated and not dates:
                continue  # A session/query date alone is not a returned value date.
            try:
                value, unit = _number(cell, spec[0])
            except ValueError:
                continue
            date_basis = 'reported_date' if dates else 'field_date'
            sources = [_source(cell, session_id, target, date_basis)]
            sources.extend(_source(other, sid, target, 'reported_date') for other, sid in dates)
            sources.extend(_source(other, session_id, target, 'identity') for other in cells
                           if _field(other['name'], target)[0] in _IDENTITY_FIELDS)
            if original_code != code:
                sources.extend(deepcopy(alias_evidence[(original_code, code)]))
            result[code].append(dict(code=code, date=target, metric=spec[0], value=value,
                                     unit=unit, source='Wind 已保存原始证据', priceBasis=spec[1],
                                     sourceCode=original_code, dateBasis=date_basis,
                                     fieldSources=sources))
    return dict(result)
