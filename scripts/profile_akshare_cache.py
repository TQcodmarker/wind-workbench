"""Read-only AK cache benchmark; writes only an optional small JSON report.

python -m scripts.profile_akshare_cache --samples 1 --base-url http://127.0.0.1:8765
No migration, materialization, data-source query, or real dataset write runs.
"""
import argparse
from contextlib import contextmanager
import cProfile
import gzip
import io
import json
from pathlib import Path
import pstats
import sqlite3
import sys
import time
from unittest.mock import patch
from urllib.parse import quote, urlsplit
from urllib.request import build_opener, ProxyHandler, Request

from backend import akshare_provider as provider, storage as store


@contextmanager
def read_connection():
    db = sqlite3.connect(store.DB.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    try:
        yield db
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target')
    parser.add_argument('--samples', type=int, choices=(1, 2, 3), default=1)
    parser.add_argument('--base-url', help='Optional existing loopback service; GET only')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--indexed-only', action='store_true', help='Measure only prebuilt summary/page/point projections')
    parser.add_argument('--code', help='Use the same bond identity as an earlier benchmark')
    parser.add_argument('--baseline', type=Path, help='Attach comparable point-read timings from this report')
    parser.add_argument('--max-single-ms', type=float, help='Fail when slowest single-bond read exceeds this limit')
    args = parser.parse_args()
    report = {'database': str(store.DB), 'python': sys.version, 'executable': sys.executable,
              'samples': args.samples, 'mode': 'indexed' if args.indexed_only else 'full', 'measurements': {},
              'limitations': ('Warm local cache; existing indexed projections only; no full dataset reads or writes.' if args.indexed_only
                              else 'Warm local cache; dataset write measurement is JSON CPU only, excluding SQLite I/O.')}

    def measure(name, operation, samples=None):
        times, value = [], None
        for _ in range(samples or args.samples):
            began = time.perf_counter()
            value = operation()
            times.append(round((time.perf_counter() - began) * 1000, 3))
        report['measurements'][name] = {'milliseconds': times}
        print(name, times, flush=True)
        return value

    with patch.object(store, 'connection', read_connection), patch.object(
            provider.QueryRecorder, 'query', side_effect=AssertionError('Benchmark cannot collect')):
        with read_connection() as db:
            row = db.execute('SELECT target_date FROM akshare_datasets ORDER BY target_date DESC LIMIT 1').fetchone()
        if not row:
            raise SystemExit('No saved AKShare dataset; benchmark did not initialize or fetch one.')
        target = args.target or row['target_date']
        report['target'] = target

        def read_payload():
            with read_connection() as db:
                return db.execute('SELECT payload FROM akshare_datasets WHERE target_date=?', (target,)).fetchone()['payload']

        if args.indexed_only:
            from backend import akshare_read_model as index
            with read_connection() as db:
                # Fail before invoking a lazy migration: this harness never writes the real cache.
                if not db.execute('SELECT 1 FROM akshare_dataset_index WHERE target_date=?', (target,)).fetchone():
                    raise SystemExit('Indexed benchmark requires an already migrated dataset.')
                code = args.code or db.execute('SELECT code FROM akshare_bond_index WHERE target_date=? '
                    'ORDER BY sort_rank DESC,code DESC LIMIT 1', (target,)).fetchone()['code']
            summary = measure('indexed_summary_read', lambda: index.summary(target))
            page = measure('indexed_page_read', lambda: index.page(target, page_size=25))
            report.update(bondCount=summary['counts']['bonds'],
                          summaryResponseBytes=len(provider._dump(summary).encode('utf-8')),
                          pageResponseBytes=len(provider._dump(page).encode('utf-8')))
            bond = measure('single_bond_read', lambda: provider.read_available_bond(target, code))
        else:
            raw = measure('sqlite_read_full_payload', read_payload)
            full = measure('json_parse_full_payload', lambda: json.loads(raw))
            report['datasetBytes'] = len(raw.encode('utf-8'))
            report['storageFormat'] = full.get('_storage', 'legacy-json')
            if full.get('_storage') == 'akshare-index-v1':
                full = measure('reconstruct_indexed_dataset', lambda: provider.read_available(target, True))
            report['bondCount'] = len(full['bonds'])
            code = args.code or full['bonds'][-1]['code']
            measure('read_available_with_evidence', lambda: provider.read_available(target, True))
            compact = measure('read_available_without_evidence', lambda: provider.read_available(target, False))
            bond = measure('single_bond_read', lambda: provider.read_available_bond(target, code))
            measure('rebuild_counts_and_cells', lambda: provider._dataset(target, full['bonds'],
                    full.get('benchmarks'), full.get('provenance')))
            encoded = measure('save_dataset_json_cpu_only', lambda: provider._dump(full))
            report['serializedDatasetBytes'] = len(encoded.encode('utf-8'))
            encoded = measure('available_response_json_cpu_only', lambda: provider._dump(compact))
            report['availableResponseBytes'] = len(encoded.encode('utf-8'))
        report['singleBondCode'] = code
        report['singleBondResponseBytes'] = len(provider._dump(bond).encode('utf-8'))
        profile = cProfile.Profile()
        profile.runcall(provider.read_available_bond, target, code)
        stream = io.StringIO()
        pstats.Stats(profile, stream=stream).strip_dirs().sort_stats('cumulative').print_stats(12)
        report['singleBondProfile'] = stream.getvalue()

    if args.base_url:
        if urlsplit(args.base_url).hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise SystemExit('HTTP benchmark accepts an existing loopback service only.')
        opener = build_opener(ProxyHandler({}))
        routes = [('http_summary', '/summary'), ('http_page', '/page?page_size=25')] if args.indexed_only else [('http_available', '')]
        for label, suffix in [*routes, ('http_single_bond', '/' + quote(code, safe=''))]:
            url = args.base_url.rstrip('/') + '/api/datasets/' + target + '/available' + suffix
            def request():
                with opener.open(Request(url, headers={'Accept-Encoding': 'gzip'}), timeout=60) as response:
                    body = response.read()
                    return body, response.headers.get('Content-Encoding')
            body, encoding = measure(label, request)
            decoded = gzip.decompress(body) if encoding == 'gzip' else body
            payload = json.loads(decoded)
            if payload.get('source') != 'akshare':
                raise AssertionError('Existing HTTP service is not serving the AKShare source.')
            report['measurements'][label].update(wireBytes=len(body), decodedBytes=len(decoded), encoding=encoding)
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
        if baseline['target'] != target or baseline['singleBondCode'] != code:
            raise AssertionError('Baseline date and bond identity must match.')
        report['baseline'] = str(args.baseline)
        report['comparison'] = {}
        for key in ('single_bond_read', 'http_single_bond'):
            if key in report['measurements'] and key in baseline['measurements']:
                before = baseline['measurements'][key]['milliseconds']
                after = report['measurements'][key]['milliseconds']
                report['comparison'][key] = dict(beforeMs=before, afterMs=after,
                    meanSpeedup=round((sum(before)/len(before))/(sum(after)/len(after)), 2))
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding='utf-8')
    print(output, flush=True)
    if args.max_single_ms is not None and max(report['measurements']['single_bond_read']['milliseconds']) > args.max_single_ms:
        raise SystemExit('Single-bond read exceeded the requested performance limit.')


if __name__ == '__main__':
    main()
