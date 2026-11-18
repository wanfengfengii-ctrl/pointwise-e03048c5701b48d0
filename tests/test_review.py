import json
import unittest
from contextlib import closing
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.read import list_shift_samples, read_sample
from archive.schema import initialize_archive, open_store
from audit.coverage import Coverage
from audit.report import review_shift
from control.dispatch import advance, run_recording
from jobs.orders import OrderBook, OrderStatus
from jobs.shifts import ShiftCatalog
from tests.test_collect import Bench


class ReviewTests(unittest.TestCase):
    def test_demo_reopen_zero_extremum_and_origins(self):
        fixture = Path(__file__).resolve().parents[1] / 'samples' / 'day.json'
        recording = json.loads(fixture.read_text(encoding='utf-8'))
        with TemporaryDirectory(prefix='test-voltage-review-') as directory:
            database = Path(directory) / 'shift.sqlite3'
            result = run_recording(recording, database)
            with closing(open_store(database)) as reopened:
                report = review_shift(reopened, 'day')
                self.assertEqual(report, result['report'])
                self.assertEqual(report.coverage, Coverage(('C', 'B'), ('A',), ()))
                self.assertEqual(report.minimum.voltage, Fraction(0, 1))
                self.assertEqual(report.maximum.voltage, Fraction(2, 3))
                self.assertEqual((report.minimum.source.order_id, report.maximum.source.order_id), ('C', 'B'))
                samples = {}
                for endpoint, raw, trigger, step, slot, actual_range in (
                    (report.minimum, 4, 'trigger-C', 6, 0, 'high'),
                    (report.maximum, 5, 'trigger-B', 1, 1, 'low'),
                ):
                    source = endpoint.source
                    item = read_sample(reopened, source.sample_id)
                    self.assertEqual((item.sample.raw_code, source.trigger_id, source.actual_range), (raw, trigger, actual_range))
                    self.assertEqual((source.exposure.recording_id, source.exposure.step, source.exposure.slot), ('chenghe-bench-day', step, slot))
                    self.assertEqual(item.sample.exposure, source.exposure)
                    self.assertEqual(item.receipt.sample_id, source.sample_id)
                    self.assertIn(item, result['samples'])
                    samples[source.order_id] = [item.sample.raw_code, source.exposure.step]
                self.assertIsNone(read_sample(reopened, 'unknown-fixed-sample'))
                trace = result['trace']
                exposure = next(index for index, entry in enumerate(trace) if entry['kind'] == 'exposure' and entry['trigger_id'] == 'trigger-A')
                terminal = next(index for index, entry in enumerate(trace) if entry['kind'] == 'completed' and entry['trigger_id'] == 'trigger-A')
                request = next(index for index, entry in enumerate(trace) if entry['kind'] == 'request_range')
                self.assertLess(exposure, terminal)
                self.assertLess(terminal, request)
                self.assertEqual(trace[exposure]['range_name'], 'low')
                print('POINTWISE_SCENARIO ' + json.dumps({
                    'case_id': 'v7', 'input': {'B': [5, '1/6', '-1/6'], 'C': [4, '1/4', '-1'], 'recording': recording['recording_id']},
                    'expected': {'minimum': [0, 1, 'C'], 'maximum': [2, 3, 'B']},
                    'actual': {
                        'minimum': [report.minimum.voltage.numerator, report.minimum.voltage.denominator, report.minimum.source.order_id],
                        'maximum': [report.maximum.voltage.numerator, report.maximum.voltage.denominator, report.maximum.source.order_id],
                    },
                    'downstream_expected': {'B': [5, 1], 'C': [4, 6]}, 'downstream_actual': samples,
                    'code_paths': ['capture/convert.py', 'archive/records.py', 'archive/read.py', 'audit/voltage.py', 'audit/provenance.py', 'audit/report.py'],
                    'oracle': 'By hand, (5 - 1) / 6 = 2/3 and 4 / 4 - 1 = 0. Use the input exposure steps to check both fixed origins.',
                }, sort_keys=True))

    def test_close_fractions_zero_and_tied_extrema(self):
        with TemporaryDirectory(prefix='test-voltage-fractions-') as directory:
            database = Path(directory) / 'shift.sqlite3'
            bench = Bench(self, capacity=4, database=database)
            bench.order('A', gain='1/3000', offset=0)
            bench.order('B', gain='1/2000', offset=0)
            bench.order('C', gain=1, offset=0)
            bench.order('D', gain='1/4000', offset=0)
            advance(bench.session)
            for step, trigger in enumerate(('a', 'b', 'c', 'd'), 1):
                bench.expose(step, trigger)
            d = bench.complete(5, 'd', 3, 4, 2)
            c = bench.complete(6, 'c', 2, 3, 0)
            b = bench.complete(7, 'b', 1, 2, 1)
            a = bench.complete(8, 'a', 0, 1, 1)
            self.assertEqual((a.sample.voltage, b.sample.voltage, d.sample.voltage), (Fraction(1, 3000), Fraction(1, 2000), Fraction(1, 2000)))
            bench.connection.close()
            with closing(open_store(database)) as reopened:
                report = review_shift(reopened, 'day')
                self.assertEqual(report.minimum.voltage, Fraction(0, 1))
                self.assertEqual(report.minimum.source.sample_id, c.sample.sample_id)
                self.assertEqual(report.minimum.source.exposure.step, 3)
                self.assertEqual(report.maximum.voltage, Fraction(1, 2000))
                self.assertEqual(report.maximum.source.sample_id, b.sample.sample_id)
                self.assertEqual(report.maximum.source.exposure.step, 2)
                self.assertEqual(report.coverage, Coverage(('A', 'B', 'C', 'D'), (), ()))

    def test_tie_for_both_extrema_uses_earliest_registration(self):
        bench = Bench(self, names=('a', 'b'))
        bench.order('A', gain=1, offset=0)
        bench.order('B', gain=2, offset=0)
        advance(bench.session)
        bench.expose(1, 'a')
        bench.expose(2, 'b')
        bench.complete(3, 'b', 1, 2, 0)
        a = bench.complete(4, 'a', 0, 1, 0)
        report = review_shift(bench.connection, 'day')
        self.assertEqual(report.minimum, report.maximum)
        self.assertEqual(report.minimum.voltage, 0)
        self.assertEqual(report.minimum.source.sample_id, a.sample.sample_id)

    def test_initialize_existing_store_keeps_user_changes_and_fixed_records(self):
        with TemporaryDirectory(prefix='test-voltage-init-') as directory:
            database = Path(directory) / 'shift.sqlite3'
            bench = Bench(self, names=('b',), database=database)
            bench.order('A')
            bench.order('B')
            bench.session.cancel('A')
            advance(bench.session)
            bench.expose(1, 'b')
            b = bench.complete(2, 'b', 0, 1, 5)
            bench.order('follow-up')
            ShiftCatalog(bench.connection).ensure_shift('spare')
            before = review_shift(bench.connection, 'day')
            bench.connection.close()
            with closing(open_store(database)) as reopened:
                for _ in range(2):
                    initialize_archive(reopened)
                    ShiftCatalog(reopened).ensure_shift('day')
                self.assertEqual(review_shift(reopened, 'day'), before)
                self.assertEqual(before.coverage, Coverage(('B',), ('A',), ('follow-up',)))
                self.assertEqual(review_shift(reopened, 'spare').coverage, Coverage((), (), ()))
                self.assertEqual(list_shift_samples(reopened, 'day'), (b,))
                self.assertEqual(read_sample(reopened, b.sample.sample_id), b)
                orders = OrderBook(reopened).list_shift_orders('day')
                self.assertEqual([(order.order_id, order.sequence, order.status) for order in orders], [
                    ('A', 1, OrderStatus.CANCELED), ('B', 2, OrderStatus.SUCCEEDED), ('follow-up', 3, OrderStatus.PENDING),
                ])

    def test_missing_canceled_empty_and_unknown_shifts(self):
        bench = Bench(self, names=())
        bench.order('missed')
        bench.order('canceled')
        bench.session.cancel('canceled')
        report = review_shift(bench.connection, 'day')
        self.assertEqual(report.coverage, Coverage((), ('canceled',), ('missed',)))
        self.assertEqual((report.minimum, report.maximum), (None, None))
        ShiftCatalog(bench.connection).ensure_shift('empty')
        empty = review_shift(bench.connection, 'empty')
        self.assertEqual(empty.coverage, Coverage((), (), ()))
        self.assertEqual((empty.minimum, empty.maximum), (None, None))
        with self.assertRaises(KeyError):
            review_shift(bench.connection, 'unknown')
        self.assertFalse(bench.connection.in_transaction)
