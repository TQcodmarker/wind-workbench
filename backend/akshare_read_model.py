"""Indexed AKShare storage and projections for bounded UI reads.

Full datasets remain available for exports and analytical readers. Incremental
publications use authoritative rows and a small metadata marker; legacy full
JSON is still supported. Rows and summary always commit in one transaction.
"""
from collections import Counter
from datetime import date
from hashlib import sha256
import json
import sqlite3
import uuid

from . import storage as store
from .domain import REGIONS

RANK = {'eligible': 0, 'incomplete': 1, 'excluded': 2, 'conflicted': 3}
TIERS = {row['id']: row['tier'] for row in REGIONS}


def _dump(value):
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


def initialize():
    with store.connection() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS akshare_dataset_index (
                target_date TEXT PRIMARY KEY, version TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS akshare_bond_index (
                target_date TEXT NOT NULL, code TEXT NOT NULL, sort_rank INTEGER NOT NULL,
                cohort TEXT, region_id TEXT, tier INTEGER, bond_type TEXT, disposition TEXT,
                search_text TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(target_date,code));
            CREATE INDEX IF NOT EXISTS akshare_bond_index_order ON akshare_bond_index(target_date,sort_rank,code);
            CREATE INDEX IF NOT EXISTS akshare_bond_index_cohort ON akshare_bond_index(target_date,cohort,sort_rank,code);
            CREATE TABLE IF NOT EXISTS akshare_bond_alias_index (
                target_date TEXT NOT NULL, alias TEXT NOT NULL, code TEXT NOT NULL,
                PRIMARY KEY(target_date,alias,code));
            CREATE INDEX IF NOT EXISTS akshare_bond_alias_owner ON akshare_bond_alias_index(target_date,code);
        ''')


def _summary(dataset):
    metadata = {key: value for key, value in dataset.items() if key != 'bonds'}
    facets = Counter()
    observed, static_completed, yield_available = 0, 0, 0
    for bond in dataset['bonds']:
        facets[(bond.get('cohort'), bond.get('regionId'), bond.get('bondType'), bond['disposition'])] += 1
        static_completed += bond.get('staticSyncStatus') == 'completed'
        if 'tradeYieldPct' in bond:
            observed += bond['tradeYieldPct'] is not None and (bond.get('tradeObservedAt') or '')[:10] == dataset['evaluationDate']
        else:
            observed += (bond.get('yieldPct') is not None and bond.get('yieldDate') == dataset['evaluationDate']
                         and bond.get('yieldPriceBasis') == 'latest_trade')
        yield_available += bond.get('yieldPct') is not None and bond.get('yieldDate') == dataset['evaluationDate']
    return dict(metadata, bonds=[], version=uuid.uuid4().hex,
                   coverage=dict(staticCompleted=static_completed, observed=observed, yieldAvailable=yield_available),
                   facets=[dict(cohort=key[0], regionId=key[1], bondType=key[2], disposition=key[3], count=count)
                           for key, count in facets.items()])


def _encode(bonds):
    for bond in bonds:
        encoded = _dump(bond)
        yield bond, encoded, sha256(encoded.encode('utf-8')).hexdigest()


def _prepare(dataset):
    metadata = {key: value for key, value in dataset.items() if key != 'bonds'}
    rows = list(_encode(dataset['bonds']))
    # Serialize each large bond object only once for both storage forms.
    full_payload = _dump(metadata)[:-1] + ',"bonds":[' + ','.join(encoded for _, encoded, _ in rows) + ']}'
    return rows, _summary(dataset), full_payload


def _write_projection(db, target, rows, summary, replace=True):
    previous = {row['code']: row['fingerprint'] for row in db.execute(
        'SELECT code,fingerprint FROM akshare_bond_index WHERE target_date=?', (target,))}
    for bond, encoded, fingerprint in rows:
        code = bond['code']
        if previous.pop(code, None) == fingerprint:
            continue
        aliases = list(dict.fromkeys([code, *(bond.get('codes') or [])]))
        search = ' '.join(str(value or '') for value in [code, bond.get('name'), bond.get('bondId'), *aliases]).casefold()
        db.execute('INSERT INTO akshare_bond_index VALUES (?,?,?,?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(target_date,code) DO UPDATE SET sort_rank=excluded.sort_rank,cohort=excluded.cohort,'
                   'region_id=excluded.region_id,tier=excluded.tier,bond_type=excluded.bond_type,'
                   'disposition=excluded.disposition,search_text=excluded.search_text,'
                   'fingerprint=excluded.fingerprint,payload=excluded.payload',
                   (target, code, RANK[bond['disposition']], bond.get('cohort'), bond.get('regionId'),
                    TIERS.get(bond.get('regionId')), bond.get('bondType'), bond['disposition'], search, fingerprint, encoded))
        db.execute('DELETE FROM akshare_bond_alias_index WHERE target_date=? AND code=?', (target, code))
        db.executemany('INSERT INTO akshare_bond_alias_index VALUES (?,?,?)', [(target, alias, code) for alias in aliases])
    for code in previous if replace else ():
        db.execute('DELETE FROM akshare_bond_index WHERE target_date=? AND code=?', (target, code))
        db.execute('DELETE FROM akshare_bond_alias_index WHERE target_date=? AND code=?', (target, code))
    db.execute('INSERT OR REPLACE INTO akshare_dataset_index VALUES (?,?,?)', (target, summary['version'], _dump(summary)))


def save_dataset(dataset):
    initialize()
    rows, metadata, encoded = _prepare(dataset)
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR REPLACE INTO akshare_datasets VALUES (?,?,?)',
                   (dataset['evaluationDate'], dataset.get('provenance', {}).get('collectedAt') or store.now(), encoded))
        _write_projection(db, dataset['evaluationDate'], rows, metadata)


def save_delta(dataset, changed_bonds, expected_version, validate=None):
    """Commit changed rows and metadata without rewriting the full day's JSON.

    The indexed representation is authoritative for this storage marker. Public
    full reads reconstruct it inside one SQLite read transaction. A competing
    publisher invalidates this delta instead of silently overwriting its data.
    """
    initialize()
    target = dataset['evaluationDate']
    rows = list(_encode(dataset['bonds'] if expected_version is None else changed_bonds))
    metadata = _summary(dataset)
    stored = {key: value for key, value in dataset.items() if key != 'bonds'}
    stored.update(bonds=[], _storage='akshare-index-v1', _indexVersion=metadata['version'])
    encoded = _dump(stored)
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        revision = db.execute('SELECT version FROM akshare_dataset_index WHERE target_date=?', (target,)).fetchone()
        if (revision['version'] if revision else None) != expected_version:
            raise RuntimeError('AKShare 数据已由其他发布更新，请从最新版本重新整理')
        if validate:
            validate(db)
        _write_projection(db, target, rows, metadata, replace=expected_version is None)
        db.execute('INSERT OR REPLACE INTO akshare_datasets VALUES (?,?,?)',
                   (target, dataset.get('provenance', {}).get('collectedAt') or store.now(), encoded))
    return metadata['version']


def read_dataset(target):
    """Read either legacy JSON or the complete indexed dataset consistently."""
    from . import akshare_provider as provider
    if not store.DB.exists():
        return provider._dataset(target, [])
    with store.connection() as db:
        db.execute('BEGIN')
        try:
            row = db.execute('SELECT payload FROM akshare_datasets WHERE target_date=?', (target,)).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table: akshare_datasets' not in str(exc):
                raise
            row = None
        if not row:
            return provider._dataset(target, [])
        data = json.loads(row['payload'])
        if data.pop('_storage', None) == 'akshare-index-v1':
            revision = db.execute('SELECT version FROM akshare_dataset_index WHERE target_date=?', (target,)).fetchone()
            if not revision or revision['version'] != data.pop('_indexVersion'):
                raise RuntimeError('AKShare 个券索引与摘要版本不一致')
            data['bonds'] = [json.loads(item['payload']) for item in db.execute(
                'SELECT payload FROM akshare_bond_index WHERE target_date=? ORDER BY sort_rank,code', (target,))]
            if len(data['bonds']) != data['counts']['bonds']:
                raise RuntimeError('AKShare 个券索引不完整')
        return data


def ensure_index(target):
    date.fromisoformat(target)
    try:
        with store.connection() as db:
            if db.execute('SELECT 1 FROM akshare_dataset_index WHERE target_date=?', (target,)).fetchone():
                return
            if not db.execute('SELECT 1 FROM akshare_datasets WHERE target_date=?', (target,)).fetchone():
                return
    except sqlite3.OperationalError as exc:
        if 'no such table: akshare_datasets' in str(exc):
            return
        if 'no such table: akshare_dataset_index' not in str(exc):
            raise
        initialize()
        return ensure_index(target)
    from . import akshare_provider as provider
    # One-time migration of an existing dataset; normal warm requests only read
    # indexed rows. Recheck under the same lock used by all dataset publishers.
    with provider.dataset_lock():
        with store.connection() as db:
            if db.execute('SELECT 1 FROM akshare_dataset_index WHERE target_date=?', (target,)).fetchone():
                return
        dataset = provider.read_available(target, True)
        rows, metadata, _ = _prepare(dataset)
        with store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            _write_projection(db, target, rows, metadata)


def summary(target):
    ensure_index(target)
    with store.connection() as db:
        row = db.execute('SELECT payload FROM akshare_dataset_index WHERE target_date=?', (target,)).fetchone()
    if row:
        return json.loads(row['payload'])
    from . import akshare_provider as provider
    return dict(provider._dataset(target, []), version=None, coverage=dict(staticCompleted=0, observed=0), facets=[])


def _filters(cohort='all', tier='all', scope='all', region='', status='all', q=''):
    if cohort not in ('all', 'unknown', 'before_20250808', 'on_or_after_20250808'):
        raise ValueError('发行组无效')
    if str(tier) not in ('all', '1', '2', '3') or scope not in ('all', 'overall', 'general', 'special') or status not in ('all', *RANK):
        raise ValueError('筛选条件无效')
    where, args = [], []
    if cohort == 'unknown':
        where.append('cohort IS NULL')
    elif cohort != 'all':
        where.append('cohort=?'); args.append(cohort)
    allowed = [row['id'] for row in REGIONS if (str(tier) == 'all' or row['tier'] == int(tier)) and region in row['name']]
    region_sql = 'region_id IN (' + ','.join('?' for _ in allowed) + ')'
    where.append('(' + region_sql + (' OR region_id IS NULL)' if str(tier) == 'all' and not region else ')'))
    args.extend(allowed)
    if scope not in ('all', 'overall'):
        where.append('bond_type=?'); args.append(scope)
    if status != 'all':
        where.append('disposition=?'); args.append(status)
    if q.strip():
        where.append("search_text LIKE ? ESCAPE '\\'")
        args.append('%' + q.strip().casefold().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
    return where, args


def page(target, page=1, page_size=25, **filters):
    if page < 1 or not 1 <= page_size <= 100:
        raise ValueError('页码必须为正数，每页数量为 1 至 100')
    ensure_index(target)
    where, args = _filters(**filters)
    clause = 'target_date=? AND ' + ' AND '.join(where)
    with store.connection() as db:
        db.execute('BEGIN')
        revision = db.execute('SELECT version FROM akshare_dataset_index WHERE target_date=?', (target,)).fetchone()
        total = db.execute('SELECT COUNT(*) n FROM akshare_bond_index WHERE ' + clause, (target, *args)).fetchone()['n']
        page = min(page, max(1, (total+page_size-1)//page_size))
        rows = db.execute('SELECT payload FROM akshare_bond_index WHERE ' + clause + ' ORDER BY sort_rank,code LIMIT ? OFFSET ?',
                          (target, *args, page_size, (page-1)*page_size)).fetchall()
    bonds = [json.loads(row['payload']) for row in rows]
    for bond in bonds:
        bond['fieldSources'] = {}
        bond.pop('fieldObservations', None)
    return dict(evaluationDate=target, source='akshare', version=revision['version'] if revision else None,
                total=total, page=page, pageSize=page_size, bonds=bonds)


def bond(target, code):
    ensure_index(target)
    with store.connection() as db:
        row = db.execute('SELECT payload FROM akshare_bond_index WHERE target_date=? AND code=?', (target, code)).fetchone()
        if not row:
            row = db.execute('SELECT b.payload FROM akshare_bond_alias_index a JOIN akshare_bond_index b '
                             'ON b.target_date=a.target_date AND b.code=a.code WHERE a.target_date=? AND a.alias=? '
                             'ORDER BY b.sort_rank,b.code LIMIT 1', (target, code)).fetchone()
    return json.loads(row['payload']) if row else None
