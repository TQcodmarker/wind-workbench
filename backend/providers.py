"""Select a provider without mutating the legacy Wind store or its credentials.

Selection affects future requests only. Jobs retain their source when queued.
There is deliberately no automatic fallback from a public provider to Wind MCP.
"""
import os
from . import storage as store
from .domain import RULES as WIND_RULES

PROVIDERS = [{'id': 'akshare', 'label': 'AKShare'}, {'id': 'wind', 'label': 'Wind MCP'}]


def active_provider():
    if store.MODE == 'demo':
        return 'demo'
    with store.connection() as db:
        row = db.execute("SELECT value FROM metadata WHERE key='active_provider'").fetchone()
    value = row['value'] if row else os.environ.get('BOND_DATA_SOURCE', 'akshare')
    if value not in ('akshare', 'wind'):
        raise ValueError('BOND_DATA_SOURCE 必须是 akshare 或 wind')
    return value


def select_provider(provider):
    if provider not in ('akshare', 'wind'):
        raise ValueError('数据源必须是 akshare 或 wind')
    if store.MODE == 'demo':
        raise ValueError('演示服务使用独立数据库；请在真实数据服务中切换来源')
    with store.connection() as db:
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('active_provider',?)", (provider,))
    return provider


def current_rules(provider=None):
    if (provider or active_provider()) == 'akshare':
        from .akshare_provider import RULES
        return RULES
    return WIND_RULES


def read_day(target, provider=None):
    if (provider or active_provider()) == 'akshare':
        from .akshare_provider import read_day as read
        return read(target)
    return store.read_day(target)


def read_available(target, include_evidence=False, provider=None):
    if (provider or active_provider()) == 'akshare':
        from .akshare_provider import read_available as read
    else:
        from .available_data import read_available as read
    return read(target, include_evidence=include_evidence)


def read_available_bond(target, code, provider=None):
    if (provider or active_provider()) == 'akshare':
        from .akshare_provider import read_available_bond as read
    else:
        from .available_data import read_available_bond as read
    return read(target, code)


def read_available_summary(target, provider=None):
    selected = provider or active_provider()
    if selected == 'akshare':
        from .akshare_read_model import summary
        return summary(target)
    from .akshare_read_model import _prepare
    return _prepare(read_available(target, provider=selected))[1]


def read_available_page(target, provider=None, **filters):
    selected = provider or active_provider()
    if selected == 'akshare':
        from .akshare_read_model import page
        return page(target, **filters)
    from .akshare_read_model import _filters
    from .domain import REGIONS
    page_number, page_size = filters.pop('page', 1), filters.pop('page_size', 25)
    if page_number < 1 or not 1 <= page_size <= 100:
        raise ValueError('页码必须为正数，每页数量为 1 至 100')
    _filters(**filters)  # Apply the same validation in every provider mode.
    cohort, tier, scope = filters.get('cohort', 'all'), str(filters.get('tier', 'all')), filters.get('scope', 'all')
    region, status, query = filters.get('region', ''), filters.get('status', 'all'), filters.get('q', '').strip().casefold()
    allowed = {r['id'] for r in REGIONS if (tier == 'all' or r['tier'] == int(tier)) and region in r['name']}
    data = read_available(target, provider=selected)
    def matches(bond):
        return ((cohort == 'all' or (not bond.get('cohort') if cohort == 'unknown' else bond.get('cohort') == cohort))
                and (bond.get('regionId') in allowed if bond.get('regionId') else tier == 'all' and not region)
                and (scope in ('all', 'overall') or bond.get('bondType') == scope)
                and (status == 'all' or bond.get('disposition') == status)
                and (not query or query in ' '.join(str(v or '') for v in [bond['code'], bond.get('name'), bond.get('bondId'), *(bond.get('codes') or [])]).casefold()))
    bonds = [bond for bond in data['bonds'] if matches(bond)]
    total = len(bonds)
    page_number = min(page_number, max(1, (total+page_size-1)//page_size))
    return dict(evaluationDate=target, source=selected, version=(data.get('provenance') or {}).get('collectedAt'),
                total=total, page=page_number, pageSize=page_size,
                bonds=bonds[(page_number-1)*page_size:page_number*page_size])


def available_dates(provider=None):
    if (provider or active_provider()) == 'akshare':
        from .akshare_provider import available_dates as read
    else:
        from .available_data import available_dates as read
    return read()


def ready_dates(provider=None):
    # AKShare currently publishes verified samples, not nationwide snapshots.
    return [] if (provider or active_provider()) == 'akshare' else store.ready_dates()


def runs():
    provider = active_provider()
    return [run for run in store.runs() if run.get('source', 'demo') == provider]


def analysis_day(target, provider=None):
    """Read-only analytical projection; it never publishes a sample as a full day."""
    selected=provider or active_provider()
    day=read_day(target,provider=selected)
    if day['source'] != 'akshare':
        return day
    data=read_available_summary(target,provider=selected)
    if not data['counts']['bonds']:
        return day
    from .domain import REGIONS
    provenance=data.get('provenance') or {}
    rules=data.get('rules') or dict(current_rules('akshare'), version=data['rulesVersion'],
                                   groupingBasis=data.get('groupingBasis', 'remaining_term'))
    day=dict(day)
    day['snapshot']={
        'source':'akshare', 'complete':False, 'scope':'saved_sample',
        'publishedRunId':provenance.get('runId') or f"akshare-sample:{target}",
        'publishedAt':provenance.get('collectedAt'),
        'rulesVersion':data['rulesVersion'], 'mappingVersion':data['mappingVersion'],
        'yieldDefinition':data['yieldDefinition'], 'rules':rules,
        'regions':REGIONS, 'cells':data['cells'], 'groupingBasis':data.get('groupingBasis', 'remaining_term'),
    }
    return day
