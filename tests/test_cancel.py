import json
import unittest

from archive.read import list_shift_samples, read_sample
from control.dispatch import advance
from jobs.orders import OrderStatus
from jobs.queue import pending_orders
from tests.test_collect import Bench


class CancelTests(unittest.TestCase):
    def test_natural_terminal_controls_range_and_replay_cannot_claim_success(self):
        bench = Bench(self)
        bench.order('A')
        advance(bench.session)
        self.assertEqual(bench.session.cancel('A'), OrderStatus.CANCELED)
        bench.order('C', 'high', revision='H1', gain='1/4', offset=-1)
        bench.order('B')
        advance(bench.session)
        self.assertEqual(bench.status('B'), OrderStatus.CAPTURING)
        self.assertEqual([entry['slot'] for entry in bench.commands('arm')], [0, 1])
        bench.expose(1, 'b')
        b = bench.complete(2, 'b', 1, 1, 5)
        premature_ranges = [entry['target'] for entry in bench.commands('request_range')]
        premature_status = str(bench.status('C'))
        bench.expose(3, 'a')
        a_range = bench.commands('exposure')[-1]['range_name']
        self.assertEqual((premature_ranges, premature_status, a_range), ([], 'pending', 'low'))
        self.assertIsNone(bench.complete(4, 'a', 0, 3, 9))
        self.assertEqual([entry['target'] for entry in bench.commands('request_range')], ['high'])
        self.assertEqual(bench.status('C'), OrderStatus.PENDING)
        bench.event(5, 'range_applied', command_id=1, range_name='high')
        self.assertEqual(bench.commands('arm')[-1]['slot'], 0)
        bench.expose(6, 'c')
        self.assertIsNone(bench.event(7, 'replay', of_step=4))
        after_replay = [entry.sample.order_id for entry in list_shift_samples(bench.connection, 'day')]
        self.assertEqual(after_replay, ['B'])
        self.assertEqual(bench.status('C'), OrderStatus.CAPTURING)
        self.assertFalse(bench.session.occupancy.can_change_range())
        c = bench.complete(8, 'c', 0, 6, 4)
        self.assertIsNone(bench.event(9, 'replay', of_step=8))
        self.assertEqual(bench.status('A'), OrderStatus.CANCELED)
        self.assertEqual(list_shift_samples(bench.connection, 'day'), (c, b))
        self.assertEqual(read_sample(bench.connection, c.sample.sample_id), c)
        collected = {entry.sample.order_id: [entry.sample.raw_code, entry.sample.exposure.step]
                     for entry in list_shift_samples(bench.connection, 'day')}
        self.assertEqual(collected, {'B': [5, 1], 'C': [4, 6]})
        self.assertEqual(bench.connection.execute('SELECT COUNT(*) FROM receipts WHERE order_id = ?', ('A',)).fetchone()[0], 0)
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'v3', 'input': {'canceled': 'A', 'completion_order': ['B', 'A', 'C'], 'replay_step': 4},
            'expected': {'ranges_before_A': [], 'C_before_A': 'pending', 'A_exposure_range': 'low'},
            'actual': {'ranges_before_A': premature_ranges, 'C_before_A': premature_status, 'A_exposure_range': a_range},
            'downstream_expected': {'B': [5, 1], 'C': [4, 6]}, 'downstream_actual': collected,
            'code_paths': ['control/dispatch.py', 'control/start.py', 'control/occupancy.py', 'capture/device.py', 'capture/accept.py', 'archive/read.py'],
            'oracle': 'Track the two low-range triggers on paper until their own terminal steps and take each raw code and exposure step from its input event.',
        }, sort_keys=True))

    def test_replayed_completion_after_same_range_slot_reuse(self):
        bench = Bench(self, names=('a', 'b'), capacity=1)
        bench.order('A')
        bench.order('B')
        advance(bench.session)
        bench.expose(1, 'a')
        bench.session.cancel('A')
        bench.complete(2, 'a', 0, 1, 9)
        self.assertEqual(bench.status('B'), OrderStatus.CAPTURING)
        self.assertEqual([entry['slot'] for entry in bench.commands('arm')], [0, 0])
        bench.expose(3, 'b')
        self.assertIsNone(bench.event(4, 'replay', of_step=2))
        self.assertEqual(list_shift_samples(bench.connection, 'day'), ())
        self.assertFalse(bench.session.occupancy.can_change_range())
        b = bench.complete(5, 'b', 0, 3, 5)
        self.assertEqual((b.sample.trigger_id, b.sample.raw_code, b.sample.exposure.step), ('b', 5, 3))
        self.assertEqual(list_shift_samples(bench.connection, 'day'), (b,))

    def test_pending_cancel_and_cancel_errors(self):
        bench = Bench(self, names=('b',), capacity=1)
        bench.order('A')
        bench.order('B')
        self.assertEqual(bench.session.cancel('A', stop=True), OrderStatus.CANCELED)
        self.assertEqual([order.order_id for order in pending_orders(bench.book, 'day')], ['B'])
        self.assertEqual(bench.commands('request_stop'), [])
        with self.assertRaises(KeyError):
            bench.session.cancel('unknown')
        with self.assertRaises(ValueError):
            bench.session.cancel('A')
        advance(bench.session)
        bench.expose(1, 'b')
        archived = bench.complete(2, 'b', 0, 1, 5)
        with self.assertRaises(ValueError):
            bench.session.cancel('B', stop=True)
        self.assertEqual(read_sample(bench.connection, archived.sample.sample_id), archived)
        self.assertEqual(bench.commands('request_stop'), [])

    def test_stopping_requires_terminal_evidence_and_keeps_first_terminal(self):
        for winner in ('stopped', 'completed'):
            with self.subTest(winner=winner):
                bench = Bench(self, names=('a', 'c'), capacity=1)
                bench.order('A')
                bench.order('C', 'high')
                advance(bench.session)
                bench.event(1, 'stopped', trigger_id='a', slot=0)
                self.assertFalse(bench.session.occupancy.can_change_range())
                bench.session.cancel('A', stop=True)
                self.assertEqual(bench.commands('request_stop'), [{'kind': 'request_stop', 'trigger_id': 'a', 'slot': 0}])
                bench.event(2, 'stop_accepted', trigger_id='a', slot=0)
                bench.event(3, 'replay', of_step=2)
                binding = bench.session.ledger.get('a')
                self.assertTrue(binding.stop_accepted)
                self.assertIsNone(binding.terminal)
                self.assertEqual(bench.commands('request_range'), [])
                self.assertEqual(bench.status('C'), OrderStatus.PENDING)
                bench.expose(4, 'a')
                self.assertEqual(bench.commands('exposure')[-1]['range_name'], 'low')
                if winner == 'stopped':
                    bench.event(5, 'stopped', trigger_id='a', slot=0)
                else:
                    bench.complete(5, 'a', 0, 4, 9)
                first_terminal = binding.terminal
                self.assertEqual(first_terminal.reason, winner)
                self.assertEqual([entry['target'] for entry in bench.commands('request_range')], ['high'])
                bench.event(6, 'range_applied', command_id=1, range_name='high')
                bench.expose(7, 'c')
                if winner == 'stopped':
                    bench.complete(8, 'a', 0, 4, 9)
                else:
                    bench.event(8, 'stopped', trigger_id='a', slot=0)
                bench.event(9, 'replay', of_step=5)
                self.assertIs(binding.terminal, first_terminal)
                self.assertEqual(bench.status('C'), OrderStatus.CAPTURING)
                self.assertEqual(bench.session.occupancy.free_slot(), None)
                self.assertEqual(list_shift_samples(bench.connection, 'day'), ())
                c = bench.complete(10, 'c', 0, 7, 4)
                self.assertEqual((c.sample.order_id, c.sample.exposure.step), ('C', 7))
                self.assertEqual(list_shift_samples(bench.connection, 'day'), (c,))
