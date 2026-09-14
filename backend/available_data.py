"""Read already recorded MCP tables as a dated, explicitly partial bond sample.

This projection never fetches Wind data or publishes a nationwide snapshot. Failed
runs can still contain useful responses, so the run's outcome is not a read gate.
"""
import json
import sqlite3
import hashlib
from collections import OrderedDict
from contextlib import closing
from copy import deepcopy
from datetime import date
from decimal import ROUND_HALF_UP
from pathlib import Path
from threading import RLock

from . import storage as store
from .domain import RULES, YIELD_DEFINITION, calculate, classify, number
from .wind_mapping import ALIASES, CODE, VERSION, Merge, base_name
from .wind_mcp import WindError
from .wind_verification import tables_from


_CACHE_LOCK = RLock()
_PROJECTIONS = OrderedDict()
_DATES = OrderedDict()
_CACHE_LIMIT = 2


def clear_available_cache():
    """Discard process-local projections after an explicit rules reload."""
    with _CACHE_LOCK:
        _PROJECTIONS.clear()
        _DATES.clear()


def _policy_revision():
    # Version labels are part of the API contract, but edits to a rule or mapping
    # must also invalidate a warm projection before its label is incremented.
    files = tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size)
                  for path in (Path(__file__), Path(__file__).with_name('domain.py'),
                               Path(__file__).with_name('wind_mapping.py')))
    policy = json.dumps((RULES, YIELD_DEFINITION, VERSION, ALIASES),
                        ensure_ascii=False, sort_keys=True, default=str)
    functions = (id(classify), id(calculate), id(Merge.normalize), id(Merge.pick),
                 id(base_name), id(tables_from))
    return files, policy, functions


def _data_revision(target=None):
    """Fingerprint request metadata, including updates to an existing row.

    Production responses already have a SHA-256 stored by Recorder.finish, so
    polling reads small metadata rows, not the potentially huge response bodies.
    Older evidence/test schemas without a digest fall back to hashing that body.
    """
    if not store.DB.exists():
        return 'missing'
    digest = hashlib.sha256()
    with closing(sqlite3.connect(store.DB.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'source_requests', 'source_sessions'} <= tables:
            return 'uninitialized'
        columns = {row[1] for row in db.execute('PRAGMA table_info(source_requests)')}
        sha = 'r.response_sha256' if 'response_sha256' in columns else 'NULL'
        finished = 'r.finished_at' if 'finished_at' in columns else 'NULL'
        query = f'''SELECT r.id, r.session_id, s.target_date, r.started_at,
                           r.status, {finished}, {sha},
                           CASE WHEN {sha} IS NULL OR {sha}='' THEN r.response ELSE NULL END,
                           r.response IS NOT NULL
                    FROM source_requests r JOIN source_sessions s ON s.id=r.session_id
                    WHERE r.method='tools/call' AND s.target_date IS NOT NULL'''
        params = ()
        if target is not None:
            query += ' AND s.target_date=?'
            params = (target,)
        for row in db.execute(query+' ORDER BY r.id', params):
            digest.update(json.dumps(tuple(row), ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8'))
            digest.update(b'\n')
    return digest.hexdigest()


def _requests(target=None):
    if not store.DB.exists():
        return []
    # A read endpoint must not create a database, initialize tables, or mutate the
    # source evidence, even when called outside the application's lifespan.
    with closing(sqlite3.connect(store.DB.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'source_requests', 'source_sessions'} <= tables:
            return []
        query = '''SELECT r.id, r.session_id, r.response, s.target_date
                   FROM source_requests r JOIN source_sessions s ON s.id=r.session_id
                   WHERE r.method='tools/call' AND r.response IS NOT NULL
                     AND s.target_date IS NOT NULL'''
        params = ()
        if target is not None:
            query += ' AND s.target_date=?'
            params = (target,)
        return [dict(r) for r in db.execute(query+' ORDER BY r.started_at, r.rowid', params)]


def _response_rows(request, warnings=None):
    response = json.loads(request['response'])
    if not isinstance(response, dict):
        raise ValueError('响应不是对象')
    result = response.get('result', response)
    if not isinstance(result, dict):
        raise ValueError('数据结果不是对象')
    rows = []
    for ti, table in enumerate(tables_from(result)):
        columns = table['columns']
        # A primary-code column can be present but empty for individual rows.
        # Resolve each row independently; one missing identity must not discard
        # the other usable rows in the same saved response.
        indexes = [ci for name in ('主证券代码', *ALIASES['code'])
                   for ci, column in enumerate(columns)
                   if base_name(column['name'],request['target_date'])[0] == name]
        if not indexes:
            continue
        for ri, values in enumerate(table['rows']):
            code = next((values[ci] for ci in indexes
                         if isinstance(values[ci], str) and CODE.fullmatch(values[ci])), None)
            if code is None:
                if warnings is not None:
                    warnings.append(f"已保存请求 {request['id']} 第 {ti+1} 张表第 {ri+1} 行无有效债券代码，原始响应仍可查阅")
                continue
            fields = [dict(name=column['name'], value=value, unit=column.get('unit'),
                           requestId=request['id'], tableIndex=ti, rowIndex=ri, columnIndex=ci)
                      for ci, (column, value) in enumerate(zip(columns, values))]
            rows.append((code, fields))
    return rows


def available_dates():
    """Dates with parseable saved bond rows, including dates without a snapshot."""
    with _CACHE_LOCK:
        key = str(store.DB.resolve())
        revision = (_data_revision(), _policy_revision())
        entry = _DATES.get(key)
        if entry is None or entry[0] != revision:
            dates = set()
            for request in _requests():
                target = request['target_date']
                if target in dates:
                    continue
                try:
                    date.fromisoformat(target)
                    if _response_rows(request):
                        dates.add(target)
                except (ValueError, TypeError, KeyError, IndexError, AttributeError, WindError):
                    continue
            entry = (revision, tuple(sorted(dates, reverse=True)))
            _DATES[key] = entry
            if len(_DATES) > _CACHE_LIMIT:
                _DATES.popitem(last=False)
        _DATES.move_to_end(key)
        return list(entry[1])


def _merge_rows(requests):
    """Union explicit primary/cross-market identities before merging field cells."""
    parents = {}
    rows = []
    warnings = []
    contributing = {}

    def find(code):
        parents.setdefault(code, code)
        while parents[code] != code:
            parents[code] = parents[parents[code]]
            code = parents[code]
        return code

    for request in requests:
        try:
            parsed = _response_rows(request, warnings)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, WindError):
            warnings.append(f"已保存请求 {request['id']} 的表格无法解析，原始响应仍可查阅")
            continue
        if parsed:
            contributing[request['id']] = request['session_id']
        for code, cells in parsed:
            aliases = {code}
            aliases.update(c['value'] for c in cells
                           if base_name(c['name'],request['target_date'])[0] in (*ALIASES['code'], *ALIASES['bondId'])
                           and isinstance(c['value'], str) and CODE.fullmatch(c['value']))
            for alias in sorted(aliases):
                parents[find(alias)] = find(code)
            for cell in cells:
                cell['sessionId'] = request['session_id']
            rows.append((code, cells, aliases))

    groups = {}
    for code, cells, aliases in rows:
        group = groups.setdefault(find(code), {'fields': [], 'codes': set()})
        group['fields'].extend(cells)
        group['codes'].update(aliases)
    return groups.values(), contributing, warnings


def _missing(row):
    missing = []
    for field, label in [('bondId', '稳定债券身份'), ('regionId', '发行地区'),
                         ('bondType', '一般/专项分类'), ('issueDate', '发行日期'),
                         ('yieldPct', '收盘价到期收益率'), ('issueAmountYi', '发行规模'),
                         ('remainingYears', '剩余期限')]:
        if row.get(field) is None or row.get(field) == '':
            missing.append(label)
    return missing


def _build_available(target):
    date.fromisoformat(target)
    groups, contributing, warnings = _merge_rows(_requests(target))
    bonds, eligible = [], []
    counts = dict(bonds=0, eligible=0, incomplete=0, excluded=0, conflicted=0,
                  requests=len(contributing), sessions=len(set(contributing.values())))
    for group in groups:
        fields = group['fields']
        primary = sorted({c['value'] for c in fields if base_name(c['name'],target)[0] == '主证券代码'
                          and isinstance(c['value'], str) and CODE.fullmatch(c['value'])})
        codes = sorted(group['codes'], key=lambda code: (not code.endswith('.IB'), code))
        code = primary[0] if len(primary) == 1 else codes[0]
        merge = Merge(target)
        merge.fields[code] = fields
        row, sources = merge.normalize(code)
        cross_market_identity = not primary and len(codes) > 1 and any(
            base_name(cell['name'],target)[0] == '跨市场代码' and isinstance(cell['value'], str)
            and CODE.fullmatch(cell['value']) for cell in fields) and all(
                cell['value'] is None or cell['value'] == '' for cell in fields
                if base_name(cell['name'],target)[0] == '主证券代码')
        if cross_market_identity:
            # Reciprocal cross-market codes are links between listings, not
            # conflicting claims about the primary code. Use the component's
            # deterministic identity without inventing an original Wind cell.
            row['bondId'] = code
        # Merge deliberately suppresses optional-field errors for the nationwide
        # pipeline. Here expose their conflicts too, rather than silently choose
        # one value for a bond detail table or include that bond in the sample.
        errors = [error for error in row['_validationErrors']
                  if not (cross_market_identity and error == 'bondId 多次返回值冲突')]
        for field in ALIASES:
            if field != 'code' and not (cross_market_identity and field == 'bondId'):
                merge.pick(code, field, errors, {}, optional=False)
        errors = list(dict.fromkeys(errors))
        row['_validationErrors'] = errors
        decision = classify(row, target)
        conflicts = [error for error in errors if '冲突' in error]
        if conflicts:
            disposition, reason = 'conflicted', '返回值冲突：'+'、'.join(conflicts)
        elif decision['disposition'] == 'included':
            disposition, reason = 'eligible', '已具备样本汇总所需字段'
            eligible.append(row)
        elif decision['category'] == 'missing':
            disposition, reason = 'incomplete', decision['reason']
        else:
            disposition, reason = 'excluded', decision['reason']
        cohort, term = None, None
        try:
            issued = date.fromisoformat(row['issueDate'])
            cohort = 'before_20250808' if issued < date(2025, 8, 8) else 'on_or_after_20250808'
        except (ValueError, TypeError):
            pass
        try:
            term = int(number(row['remainingYears']).quantize(1, rounding=ROUND_HALF_UP))
        except ValueError:
            pass
        descriptive = row['_descriptive']
        request_ids = list(dict.fromkeys(cell['requestId'] for cell in fields))
        session_ids = list(dict.fromkeys(contributing[rid] for rid in request_ids))
        bond = {key: row.get(key) for key in ('code', 'bondId', 'regionId', 'bondType',
                'issueDate', 'remainingYears', 'issueAmountYi', 'yieldPct')}
        bond.update({key: descriptive.get(key) for key in ('name', 'issuer', 'maturityDate',
                    'outstandingBalanceYi', 'couponPct', 'duration', 'closeNetPrice', 'currency')})
        bond.update(cohort=cohort, termYears=term, disposition=disposition, reason=reason,
                    missingFields=_missing(row), validationErrors=errors, fieldSources=sources,
                    sourceUnits=descriptive['sourceUnits'], codes=codes,
                    sessionIds=session_ids, requestIds=request_ids)
        if cross_market_identity:
            bond['identityBasis'] = 'explicit_cross_market_links'
        bonds.append(bond)
        counts[disposition] += 1
    # Passing only merged, eligible identities avoids both duplicate weighting
    # and publication-style hard failure from one unusable partial record.
    cells, _ = calculate(eligible, target)
    counts['bonds'] = len(bonds)
    order = {'eligible': 0, 'incomplete': 1, 'conflicted': 2, 'excluded': 3}
    bonds.sort(key=lambda bond: (order[bond['disposition']], bond['code']))
    if bonds:
        warnings.insert(0, '仅展示该查询日期已保存的债券样本，覆盖范围未经全市场完整性确认')
    return dict(evaluationDate=target, scope='saved_sample', complete=False,
                rulesVersion=RULES['version'], mappingVersion=VERSION,
                yieldDefinition=YIELD_DEFINITION, counts=counts, bonds=bonds,
                cells=[cell for cell in cells if cell['sampleCount']], warnings=warnings)


def _cached_projection(target):
    """Return an internal entry. Callers hold _CACHE_LOCK and never expose it."""
    date.fromisoformat(target)
    key = (str(store.DB.resolve()), target)
    revision = (_data_revision(target), _policy_revision())
    entry = _PROJECTIONS.get(key)
    if entry is None or entry[0] != revision:
        full = _build_available(target)
        # Share nested immutable-to-callers metadata within the cache, and omit
        # evidence BEFORE copying. A list request never clones the large evidence
        # tree just to discard it again.
        lightweight = dict(full, bonds=[dict(bond, fieldSources={}) for bond in full['bonds']])
        by_code = {}
        for bond in full['bonds']:
            for code in (*bond['codes'], bond['code'], bond['bondId']):
                if code is not None:
                    by_code[code] = bond
        entry = (revision, full, lightweight, by_code)
        _PROJECTIONS[key] = entry
        if len(_PROJECTIONS) > _CACHE_LIMIT:
            _PROJECTIONS.popitem(last=False)
    _PROJECTIONS.move_to_end(key)
    return entry


def read_available(target, include_evidence=True):
    """Return a detached dated projection; list endpoints can omit field sources."""
    with _CACHE_LOCK:
        entry = _cached_projection(target)
        return deepcopy(entry[1] if include_evidence else entry[2])


def read_available_bond(target, code):
    """Return one detached full bond, resolving explicit cross-market aliases."""
    with _CACHE_LOCK:
        bond = _cached_projection(target)[3].get(code)
        return deepcopy(bond) if bond is not None else None
