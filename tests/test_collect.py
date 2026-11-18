import unittest
from fractions import Fraction

from archive.read import list_shift_samples, read_sample
from archive.schema import initialize_archive, open_store
from capture.convert import Calibration
from capture.device import RecordedDevice
from capture.events import Exposure
from control.dispatch import Session, advance
from control.range import RangeState
from jobs.orders import OrderStatus
from jobs.queue import pending_orders
from jobs.shifts import ShiftCatalog
from jobs.status import get_order_status


class Bench:
    def __init__(self, case, names=('a', 'b', 'c', 'd'), *, capacity=2, database=':memory:',
                 shift_id='day', recording_id='recording'):
        self.connection = open_store(database)
        case.addCleanup(self.connection.close)
        initialize_archive(self.connection)
        ShiftCatalog(self.connection).ensure_shift(shift_id)
        self.shift_id = shift_id
        self.device = RecordedDevice(recording_id, 'low', names, capacity=capacity)
        self.session = Session(self.connection, shift_id, RangeState(('low', 'high'), 'low'), self.device)
        self.book = self.session.orders

    def order(self, order_id, range_name='low', *, revision='L1', gain='1/6', offset='-1/6'):
        return self.book.register(
            self.shift_id, order_id, range_name, Calibration(range_name, revision, gain, offset)
        )

    def status(self, order_id):
        return get_order_status(self.book, self.shift_id, order_id)

    def event(self, step, kind, **fields):
        return advance(self.session, self.device.next_event({'kind': kind, 'step': step, **fields}))

    def expose(self, step, trigger):
        return self.event(step, 'exposure', trigger_id=trigger)

    def complete(self, step, trigger, slot, exposure_step, raw_code):
        return self.event(
            step, 'completed', trigger_id=trigger, slot=slot, exposure_step=exposure_step, raw_code=raw_code
        )

    def commands(self, kind):
        return [entry for entry in self.device.trace if entry['kind'] == kind]


class CollectTests(unittest.TestCase):
    def test_parallel_reverse_notifications_and_frozen_calibrations(self):
        bench = Bench(self)
        bench.order('A')
        bench.order('B', revision='L2', gain='1/5', offset=0)
        self.assertIsNone(advance(bench.session))
        self.assertEqual([(entry['trigger_id'], entry['slot']) for entry in bench.commands('arm')], [('a', 0), ('b', 1)])
        bench.expose(2, 'a')
        bench.expose(3, 'b')
        b = bench.complete(6, 'b', 1, 3, 5)
        self.assertEqual(bench.status('A'), OrderStatus.CAPTURING)
        a = bench.complete(8, 'a', 0, 2, 5)
        self.assertEqual((a.sample.order_id, a.sample.trigger_id, a.sample.raw_code, a.sample.exposure.step), ('A', 'a', 5, 2))
        self.assertEqual((b.sample.order_id, b.sample.trigger_id, b.sample.raw_code, b.sample.exposure.step), ('B', 'b', 5, 3))
        self.assertEqual((a.sample.voltage, b.sample.voltage), (Fraction(2, 3), Fraction(1)))
        self.assertEqual((a.sample.calibration.revision, a.sample.calibration.gain, a.sample.calibration.offset), ('L1', Fraction(1, 6), Fraction(-1, 6)))
        self.assertEqual((b.sample.calibration.revision, b.sample.calibration.gain, b.sample.calibration.offset), ('L2', Fraction(1, 5), Fraction(0)))
        self.assertEqual(list_shift_samples(bench.connection, 'day'), (a, b))
        self.assertEqual(read_sample(bench.connection, a.sample.sample_id), a)
        self.assertEqual((a.receipt.order_id, a.receipt.sample_id), ('A', a.sample.sample_id))
        self.assertEqual((bench.status('A'), bench.status('B')), (OrderStatus.SUCCEEDED, OrderStatus.SUCCEEDED))
        self.assertTrue(bench.session.occupancy.can_change_range())

    def test_bad_range_duplicate_and_basis_leave_queue_unchanged(self):
        bench = Bench(self)
        first = bench.order('A')
        second = bench.order('B')
        with self.assertRaisesRegex(ValueError, 'unsupported range'):
            bench.order('X', 'medium')
        with self.assertRaisesRegex(ValueError, 'duplicate order'):
            bench.order('A', gain=2)
        with self.assertRaises(ValueError):
            bench.book.register('day', 'X', 'low', Calibration('high', 'H1', 1, 0))
        with self.assertRaises(ValueError):
            bench.order('X', gain='1/0')
        with self.assertRaises(KeyError):
            bench.book.register('night', 'A', 'low', first.calibration)
        self.assertEqual(pending_orders(bench.book, 'day'), (first, second))
        with self.assertRaises(KeyError):
            bench.book.get_order('day', 'X')
        self.assertEqual(bench.order('Y').sequence, 3)

    def test_order_and_sample_keys_are_scoped_by_shift(self):
        day = Bench(self, names=('a',))
        day.order('A')
        advance(day.session)
        day.expose(1, 'a')
        first = day.complete(2, 'a', 0, 1, 5)
        ShiftCatalog(day.connection).ensure_shift('night')
        night_device = RecordedDevice('night-recording', 'low', ('a',))
        night = Session(day.connection, 'night', RangeState(('low', 'high'), 'low'), night_device)
        order = night.orders.register('night', 'A', 'low', Calibration('low', 'N1', 1, 0))
        self.assertEqual(order.sequence, 1)
        advance(night)
        advance(night, night_device.next_event({'step': 1, 'kind': 'exposure', 'trigger_id': 'a'}))
        second = advance(night, night_device.next_event({
            'step': 2, 'kind': 'completed', 'trigger_id': 'a', 'slot': 0, 'exposure_step': 1, 'raw_code': 7,
        }))
        self.assertNotEqual(first.sample.sample_id, second.sample.sample_id)
        self.assertEqual(list_shift_samples(day.connection, 'day'), (first,))
        self.assertEqual(list_shift_samples(day.connection, 'night'), (second,))
        self.assertEqual(read_sample(day.connection, second.sample.sample_id).receipt.shift_id, 'night')

    def test_wait_for_matching_range_ack(self):
        bench = Bench(self, names=('a', 'b', 'c', 'd'))
        for order_id, range_name in (('A', 'low'), ('B', 'low'), ('C', 'high'), ('D', 'low')):
            bench.order(order_id, range_name)
        advance(bench.session)
        bench.expose(1, 'a')
        bench.expose(2, 'b')
        bench.complete(3, 'b', 1, 2, 5)
        self.assertEqual(bench.status('D'), OrderStatus.CAPTURING)
        self.assertEqual(bench.status('C'), OrderStatus.PENDING)
        bench.expose(4, 'c')
        bench.complete(5, 'a', 0, 1, 9)
        self.assertEqual(bench.commands('request_range'), [])
        bench.complete(6, 'c', 1, 4, 7)
        self.assertEqual(bench.commands('request_range'), [{'kind': 'request_range', 'command_id': 1, 'target': 'high'}])
        bench.event(7, 'range_applied', command_id=2, range_name='high')
        bench.event(8, 'range_applied', command_id=1, range_name='low')
        self.assertEqual((bench.session.ranges.current, bench.device.actual_range), ('low', 'low'))
        self.assertEqual(bench.session.ranges.pending.target, 'high')
        self.assertEqual(len(bench.commands('arm')), 3)
        self.assertEqual(bench.status('C'), OrderStatus.PENDING)
        bench.event(9, 'range_applied', command_id=1, range_name='high')
        self.assertEqual((bench.session.ranges.current, bench.device.actual_range), ('high', 'high'))
        self.assertIsNone(bench.session.ranges.pending)
        self.assertEqual(bench.commands('arm')[-1], {'kind': 'arm', 'trigger_id': 'd', 'slot': 0, 'range_name': 'high'})
        bench.event(10, 'replay', of_step=9)
        self.assertEqual(len(bench.commands('arm')), 4)
        bench.expose(11, 'd')
        self.assertEqual(bench.complete(12, 'd', 0, 11, 5).sample.order_id, 'C')

    def test_invalid_measurements_release_only_the_confirmed_trigger(self):
        cases = (
            ('no raw code', None, 2, 'normal'), ('not integer', '5', 2, 'normal'),
            ('boolean', True, 2, 'normal'), ('no exposure', 5, 2, 'none'),
            ('wrong reference', 5, 1, 'normal'), ('wrong range', 5, 2, 'wrong range'),
            ('wrong recording', 5, 2, 'wrong recording'),
        )
        for title, raw_code, reference, exposure in cases:
            with self.subTest(title=title):
                bench = Bench(self, names=('a',), capacity=1)
                bench.order('A')
                bench.order('C', 'high')
                advance(bench.session)
                bench.complete(1, 'unknown', 0, 2, 5)
                self.assertEqual(bench.commands('request_range'), [])
                if exposure == 'normal':
                    bench.expose(2, 'a')
                elif exposure == 'wrong range':
                    advance(bench.session, Exposure('a', 0, 'high', 'recording', 2))
                elif exposure == 'wrong recording':
                    advance(bench.session, Exposure('a', 0, 'low', 'different-recording', 2))
                bench.complete(3, 'a', 1, reference, raw_code)
                self.assertEqual(bench.commands('request_range'), [])
                notice = {'trigger_id': 'a', 'slot': 0, 'exposure_step': reference}
                if raw_code is not None:
                    notice['raw_code'] = raw_code
                self.assertIsNone(bench.event(4, 'completed', **notice))
                self.assertEqual(bench.status('A'), OrderStatus.CAPTURING)
                self.assertEqual(list_shift_samples(bench.connection, 'day'), ())
                self.assertTrue(bench.session.occupancy.can_change_range())
                self.assertEqual([entry['target'] for entry in bench.commands('request_range')], ['high'])

    def test_binding_identity_and_first_exposure_do_not_change(self):
        bench = Bench(self, names=('a',), capacity=1)
        bench.order('A')
        advance(bench.session)
        with self.assertRaises(ValueError):
            bench.session.ledger.bind('a', 'day', 'different-order', 0, 'low')
        bench.expose(1, 'a')
        bench.expose(2, 'a')
        result = bench.complete(3, 'a', 0, 1, 5)
        self.assertEqual((result.sample.order_id, result.sample.exposure.step), ('A', 1))

    def test_exhausted_recording_leaves_unstarted_order_pending(self):
        bench = Bench(self, names=())
        bench.order('A')
        with self.assertRaisesRegex(ValueError, 'no trigger name'):
            advance(bench.session)
        self.assertEqual(bench.status('A'), OrderStatus.PENDING)
        self.assertEqual(bench.commands('arm'), [])
        self.assertTrue(bench.session.occupancy.can_change_range())
