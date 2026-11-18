from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from ipo_monitor import CollectionError
from ipo_monitor.daily_state import DailyLedger, ensure_schema, file_lock


class DailyStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'daily.sqlite3'

    def test_day_budget_cross_model_and_rollover(self):
        with DailyLedger(self.path) as ledger:
            run = ledger.start_run('2026-11-16')
            first = ledger.reserve('2026-11-16', 'a', run, 'hash', 2)
            ledger.mark_dispatched(first)
            ledger.complete(first, {'input_tokens': 3, 'output_tokens': 4, 'http_status': 200})
            ledger.reserve('2026-11-16', 'b', run, 'hash', 2)
            self.assertIsNone(ledger.reserve('2026-11-16', 'c', run, 'hash', 2))
            self.assertEqual(ledger.remaining('2026-11-16', 1), 0)
            next_run = ledger.start_run('2026-11-17')
            self.assertIsNotNone(ledger.reserve('2026-11-17', 'a', next_run, 'hash', 2))
            self.assertEqual(ledger.attempts_for('hash', 'a'), 2)
            self.assertEqual(ledger.summary()['input_tokens'], 3)
            self.assertEqual(ledger.summary()['output_tokens'], 4)
            self.assertEqual(ledger.summary('2026-11-16')['states']['reserved'], 1)

    def test_crash_uncertain_reservations_survive_and_no_refund(self):
        with DailyLedger(self.path) as ledger:
            run = ledger.start_run('2026-11-16')
            pending = ledger.reserve('2026-11-16', 'a', run, 'x', 1)
        with DailyLedger(self.path) as ledger:
            self.assertEqual(ledger.remaining('2026-11-16', 1), 0)
            ledger.complete(pending, status='failed')
            self.assertEqual(ledger.remaining('2026-11-16', 1), 0)
            self.assertEqual(ledger.summary()['states']['failed'], 1)

    def test_simultaneous_reservations_never_exceed_limit(self):
        with DailyLedger(self.path) as ledger:
            run = ledger.start_run('2026-11-16')
        def reserve(number):
            with DailyLedger(self.path) as ledger:
                return ledger.reserve('2026-11-16', 'a', run, str(number), 3)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(16)))
        self.assertEqual(sum(result is not None for result in results), 3)

    def test_lock_contention_stable_inode_and_release_on_exception(self):
        path = self.path.with_suffix('.lock')
        with file_lock(path):
            inode = path.stat().st_ino
            with self.assertRaises(CollectionError):
                with file_lock(path):
                    self.fail('competing lock acquired')
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(RuntimeError, 'test'):
            with file_lock(path):
                raise RuntimeError('test')
        with file_lock(path):
            self.assertEqual(path.stat().st_ino, inode)

    def test_lock_competing_process(self):
        path = self.path.with_suffix('.lock')
        script = """import sys
from pathlib import Path
from ipo_monitor import CollectionError
from ipo_monitor.daily_state import file_lock
try:
    with file_lock(Path(sys.argv[1])):
        pass
except CollectionError:
    sys.exit(7)
"""
        with file_lock(path):
            result = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True)
            self.assertEqual(result.returncode, 7, result.stderr.decode())
        result = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_lock_rejects_symlink(self):
        target = self.path.with_suffix('.target')
        target.write_text('unchanged')
        link = self.path.with_suffix('.lock')
        link.symlink_to(target)
        with self.assertRaises(CollectionError):
            with file_lock(link):
                self.fail('symlink accepted')
        self.assertEqual(target.read_text(), 'unchanged')

    def test_safe_metadata_and_summary_serialization(self):
        with DailyLedger(self.path) as ledger:
            run = ledger.start_run('2026-11-16')
            reservation = ledger.reserve('2026-11-16', "model'; DROP TABLE daily_runs;--", run, 'x', 1)
            ledger.complete(reservation, {'input_tokens': 2, 'authorization': 'test-secret'})
            with self.assertRaises(CollectionError):
                ledger.finish_run(run, 'error', {'error': ValueError('test-secret')})
            with self.assertRaises(CollectionError):
                ledger.finish_run(run, 'success', {'nan': float('nan')})
            ledger.finish_run(run, 'partial', {'errors': 1})
            summary = ledger.summary()
            self.assertEqual(summary['last_run']['summary'], {'errors': 1})
            self.assertNotIn('test-secret', repr(summary))
            self.assertNotIn('test-secret', repr(ledger.conn.execute('SELECT * FROM daily_reservations').fetchall()))
            with self.assertRaises(CollectionError):
                ledger.complete(reservation, status='failed')

    def test_invalid_inputs_do_not_mutate(self):
        with DailyLedger(self.path) as ledger:
            for day in ('20261116', '2026-11-31', None):
                with self.assertRaises(CollectionError):
                    ledger.start_run(day)
            run = ledger.start_run('2026-11-16')
            for limit in (True, -1, 1.5):
                with self.assertRaises(CollectionError):
                    ledger.reserve('2026-11-16', 'a', run, 'x', limit)
            with self.assertRaises(CollectionError):
                ledger.reserve('2026-11-17', 'a', run, 'x', 2)
            reservation = ledger.reserve('2026-11-16', 'a', run, 'x', 2)
            for usage in ({'input_tokens': True}, {'output_tokens': -1}, {'http_status': 999}):
                with self.assertRaises(CollectionError):
                    ledger.complete(reservation, usage)
            self.assertEqual(ledger.summary()['states']['reserved'], 1)

    def test_ensure_schema_preserves_callers_transaction(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE existing (value TEXT)')
        conn.execute("INSERT INTO existing VALUES ('pending')")
        ensure_schema(conn)
        self.assertTrue(conn.in_transaction)
        conn.rollback()
        self.assertEqual(conn.execute('SELECT count(*) FROM existing').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
