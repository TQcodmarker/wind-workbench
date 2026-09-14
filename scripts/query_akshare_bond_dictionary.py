"""Query documented AKShare bond functions and keep field-level provenance.

Run with .tmp/akshare-venv/Scripts/python.exe. Raw observations remain immutable.
The separate build step reuses the already recorded market and detail responses.
"""
import argparse
import importlib
import inspect
import json
from pathlib import Path
import sys

import akshare as ak
import verify_akshare_bonds as harness

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'research' / 'akshare-dictionary-normalized-20260912'
DOC = 'https://akshare.akfamily.xyz/data/bond/bond.html'
DEFINITIONS = {
    'bond_types': ('bond_info_cm_query', {'symbol': '债券类型'}, ['name', 'code']),
    'index_catalog': ('bond_available_index_cbond', {}, ['index', 'value']),
    'index_duration': ('bond_index_general_cbond', {'index_category': '地方政府债指数', 'indicator': '平均市值法久期', 'period': '总值'}, ['date', 'value']),
    'index_yield': ('bond_index_general_cbond', {'index_category': '地方政府债指数', 'indicator': '平均市值法到期收益率', 'period': '总值'}, ['date', 'value']),
    'traded_lookup': ('bond_info_cm', {'bond_code': '101948', 'bond_type': '地方政府债'}, ['债券简称', '债券代码', '发行人/受托机构', '债券类型', '发行日期', '查询代码']),
    'traded_detail': ('bond_info_detail_cm', {'symbol': '23四川18'}, ['name', 'value']),
}

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

def query():
    harness.OUT = OUT / 'queries'
    harness.CASES = {key: (spec[0], spec[1]) for key, spec in DEFINITIONS.items()}
    harness.SUPPLEMENTAL_CASES = {}
    contracts = []
    for case, (name, arguments, expected_columns) in DEFINITIONS.items():
        signature = inspect.signature(getattr(ak, name))
        signature.bind(**arguments)
        if name == 'bond_index_general_cbond':
            catalog = read(harness.OUT / 'index_catalog' / 'data.json')
            assert arguments['index_category'] in {row['value'] for row in catalog}
            assert arguments['indicator'] in {'平均市值法久期', '平均市值法到期收益率'}
            assert arguments['period'] == '总值'
        if case in {'traded_lookup', 'traded_detail'}:
            types = read(harness.OUT / 'bond_types' / 'data.json')
            assert '地方政府债' in {row['name'] for row in types}
        result_path = harness.OUT / case / 'result.json'
        mode = 'compatibility_adapter' if case == 'traded_detail' else 'documented_sdk'
        if not result_path.exists():
            if case == 'traded_detail':
                module = importlib.import_module('akshare.bond.bond_info_cm')
                original = module.bond_info_cm
                def typed_lookup(*args, **kwargs):
                    kwargs.setdefault('bond_type', '地方政府债')
                    return original(*args, **kwargs)
                module.bond_info_cm = typed_lookup
                try:
                    harness.run_case(case)
                finally:
                    module.bond_info_cm = original
            else:
                harness.run_case(case)
        result = read(result_path)
        valid = result['status'] == 'returned_data' and set(expected_columns).issubset(result['columns'])
        contracts.append({'case': case, 'function': name, 'arguments': arguments,
                          'dictionary_url': DOC, 'installed_signature': str(signature),
                          'expected_columns': expected_columns, 'schema_valid': valid,
                          'execution_mode': mode, 'result': str(result_path),
                          'schema_note': 'Dictionary parameter table says periods; official example and runtime use period.' if name == 'bond_index_general_cbond' else
                          'SDK detail has only symbol; compatibility adapter adds the required bond_type to its internal lookup.' if mode == 'compatibility_adapter' else ''})
        save(OUT / 'query-contracts.json', contracts)
        if not valid:
            raise RuntimeError(f'{case}: output does not meet the documented contract')
    print(json.dumps({'validated_contracts': len(contracts), 'output': str(OUT)}, ensure_ascii=False))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--query', action='store_true')
    args = parser.parse_args()
    if args.query:
        query()
