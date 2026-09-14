"""Indexed storage commits and reads one complete, versioned AKShare dataset."""
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import akshare_provider as provider, akshare_read_model as index, storage as store


TARGET = '2026-09-11'


def bond(code='809336', value='1.8'):
    detail = dict(bondCode=code, bondName='测试河北债' + code, bondType='地方政府债',
                  entyFullName='河北省人民政府', bondFullName='河北省政府一般债券',
                  issueDate='2026-04-20', mrtyDate='2036-09-11', issueAmnt='10', parCouponRate='1.85',
                  couponType='附息式固定利率', couponFrqncy='年',
                  frstValueDate='2026-09-11', frstCpnDt='2027-09-11')
    trade = dict(bondcode=code, showDate=TARGET + ' 16:20:00', dmiLatestContraRate=value)
    return provider.normalize_bond(detail, TARGET, trade,
        detail_evidence=dict(requestId='detail-' + code, function='bond_info_detail_cm', runId='fixture'),
        trade_evidence=dict(requestId='trade-' + code, function='bond_spot_deal', runId='fixture'))


class IndexedStorageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(store, 'DB', Path(directory) / 'indexed.sqlite3'))
        self.stack.enter_context(patch.object(store, 'MODE', 'wind'))
        self.stack.enter_context(patch('requests.Session.send', side_effect=AssertionError('No live requests')))
        store.initialize(seed=False)  # WAL permits the concurrent-reader regression.
        provider.initialize(seed=False)
        self.data = provider._dataset(TARGET, [bond(), bond('809337', '0')])
        self.compact(self.data)

    def compact(self, data):
        provider._save_dataset(data)
        version = index.summary(TARGET)['version']
        return index.save_delta(data, [], version)

    def primary(self):
        with store.connection() as db:
            return db.execute('SELECT payload FROM akshare_datasets WHERE target_date=?', (TARGET,)).fetchone()['payload']

    def revised(self):
        return provider._dataset(TARGET, [bond(value='2.2'), bond('809337', '0')])

    def test_delta_projection_failure_rolls_back_marker_rows_and_version(self):
        before, summary = self.primary(), index.summary(TARGET)
        real_write = index._write_projection
        def interrupted(*args, **kwargs):
            real_write(*args, **kwargs)
            raise RuntimeError('simulated failure after index writes')
        revised = self.revised()
        with patch.object(index, '_write_projection', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'simulated failure'):
                index.save_delta(revised, [revised['bonds'][0]], summary['version'])
        self.assertEqual(self.primary(), before)
        self.assertEqual(index.summary(TARGET), summary)
        self.assertEqual(provider.read_available(TARGET, True), self.data)

    def test_stale_delta_cannot_overwrite_a_new_full_publication(self):
        stale_version = index.summary(TARGET)['version']
        newer = self.revised()
        provider._save_dataset(newer)
        current, summary = self.primary(), index.summary(TARGET)
        with self.assertRaisesRegex(RuntimeError, '其他发布更新'):
            index.save_delta(self.data, self.data['bonds'], stale_version)
        self.assertEqual(self.primary(), current)
        self.assertEqual(index.summary(TARGET), summary)
        self.assertEqual(provider.read_available(TARGET, True), newer)

    def test_full_read_keeps_old_snapshot_when_new_version_commits_mid_read(self):
        version = index.summary(TARGET)['version']
        newer, original_connection = self.revised(), store.connection
        observed = {'committed': False, 'readerInTransaction': False}

        class InterleavingConnection:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, args=()):
                cursor = self.db.execute(sql, args)
                if sql.startswith('SELECT payload FROM akshare_datasets') and not observed['committed']:
                    observed['readerInTransaction'] = self.db.in_transaction
                    observed['committed'] = True
                    # The first connection already holds its read snapshot.
                    # A second real WAL connection now commits a full new generation.
                    with patch.object(store, 'connection', original_connection):
                        index.save_delta(newer, [newer['bonds'][0]], version)
                return cursor

        @contextmanager
        def interleaved_connection():
            with original_connection() as db:
                yield InterleavingConnection(db)

        with patch.object(store, 'connection', interleaved_connection):
            read_during_commit = index.read_dataset(TARGET)
        self.assertTrue(observed['committed'])
        self.assertTrue(observed['readerInTransaction'])
        self.assertEqual(read_during_commit, self.data)
        self.assertEqual(index.read_dataset(TARGET), newer)

    def test_missing_index_rows_or_version_are_detected_without_partial_export(self):
        for missing in ('bond', 'version'):
            with self.subTest(missing=missing):
                self.compact(self.data)
                with store.connection() as db:
                    if missing == 'bond':
                        db.execute('DELETE FROM akshare_bond_index WHERE target_date=? AND code=?', (TARGET, '809336.IB'))
                    else:
                        db.execute('DELETE FROM akshare_dataset_index WHERE target_date=?', (TARGET,))
                with self.assertRaisesRegex(RuntimeError, '索引'):
                    provider.read_available(TARGET, True)

    def test_full_save_can_replace_compact_format_and_keep_complete_exports(self):
        self.assertEqual(json.loads(self.primary())['_storage'], 'akshare-index-v1')
        replacement = provider._dataset(TARGET, [bond('809338'), bond('809339', '0')])
        provider._save_dataset(replacement)
        self.assertNotIn('_storage', json.loads(self.primary()))
        self.assertEqual(provider.read_available(TARGET, True), replacement)
        self.assertEqual(index.page(TARGET)['total'], 2)
        self.assertIsNone(index.bond(TARGET, '809336.IB'))
        self.assertEqual(index.bond(TARGET, '809339.IB')['yieldPct'], '0')
        version = index.summary(TARGET)['version']
        index.save_delta(replacement, [], version)
        self.assertEqual(provider.read_available(TARGET, True), replacement)


if __name__ == '__main__':
    unittest.main()
