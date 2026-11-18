import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.publish import sample_id_for
from archive.read import list_shift_samples, read_sample
from archive.schema import open_store
from control.dispatch import advance
from jobs.orders import OrderStatus
from tests.test_collect import Bench


class ArchiveTests(unittest.TestCase):
    def _check_archive_failure(self, failure_sql, expected_message):
        with TemporaryDirectory(prefix='test-voltage-archive-') as directory:
            database = Path(directory) / 'shift.sqlite3'
            bench = Bench(self, names=('old', 'new', 'next'), capacity=1, database=database)
            try:
                bench.order('old')
                bench.order('new')
                advance(bench.session)
                bench.expose(1, 'old')
                old = bench.complete(2, 'old', 0, 1, 5)
                bench.order('next', 'high')
                bench.expose(3, 'new')
                bench.connection.executescript(failure_sql)
                with closing(open_store(database)) as observer:
                    with self.assertRaisesRegex(sqlite3.IntegrityError, expected_message):
                        bench.complete(4, 'new', 0, 3, 11)
                    new_id = sample_id_for('day', 'new')
                    self.assertFalse(bench.connection.in_transaction)
                    self.assertEqual(bench.status('new'), OrderStatus.CAPTURING)
                    self.assertTrue(bench.session.occupancy.can_change_range())
                    self.assertIsNone(read_sample(observer, new_id))
                    self.assertEqual(list_shift_samples(observer, 'day'), (old,))
                    self.assertEqual(read_sample(observer, old.sample.sample_id), old)
                    self.assertEqual(observer.execute('SELECT sample_id FROM samples').fetchall()[0][0], old.sample.sample_id)
                    self.assertEqual(observer.execute('SELECT COUNT(*) FROM samples').fetchone()[0], 1)
                    self.assertEqual(observer.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 1)
                    status = observer.execute('SELECT status FROM orders WHERE order_id = ?', ('new',)).fetchone()[0]
                    self.assertEqual(status, 'capturing')
                self.assertIsNone(bench.event(5, 'replay', of_step=4))
                self.assertEqual([entry['target'] for entry in bench.commands('request_range')], ['high'])
                self.assertIsNone(read_sample(bench.connection, new_id))
                bench.connection.close()
                with closing(open_store(database)) as reopened:
                    self.assertEqual(read_sample(reopened, old.sample.sample_id), old)
                    self.assertIsNone(read_sample(reopened, new_id))
            finally:
                bench.connection.close()

    def test_write_failure_rolls_back_sample_and_receipt(self):
        self._check_archive_failure('''
            CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts BEGIN
                SELECT RAISE(ABORT, 'receipt write failed');
            END;
        ''', 'receipt write failed')

    def test_deferred_commit_failure_rolls_back_success_state(self):
        self._check_archive_failure('''
            CREATE TABLE commit_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE commit_guard (
                id INTEGER REFERENCES commit_parent(id) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TRIGGER fail_commit AFTER INSERT ON receipts BEGIN
                INSERT INTO commit_guard (id) VALUES (1);
            END;
        ''', 'FOREIGN KEY constraint failed')

    def test_very_large_integer_fields_round_trip_exactly(self):
        bench = Bench(self, names=('a',), capacity=1)
        denominator = 10**35
        bench.order('A', gain=f'1/{denominator}', offset=0)
        advance(bench.session)
        bench.expose(1, 'a')
        result = bench.complete(2, 'a', 0, 1, denominator + 1)
        reread = read_sample(bench.connection, result.sample.sample_id)
        self.assertEqual(reread, result)
        self.assertEqual(reread.sample.raw_code, denominator + 1)
        self.assertEqual((reread.sample.voltage.numerator, reread.sample.voltage.denominator), (denominator + 1, denominator))
