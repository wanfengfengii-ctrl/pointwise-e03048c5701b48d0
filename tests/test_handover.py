import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.publish import sample_id_for
from archive.read import read_sample
from archive.schema import initialize_archive, open_store
from capture.convert import Calibration
from capture.device import RecordedDevice
from capture.events import RangeApplied
from control.dispatch import (
    HandoverSession, advance, resume, start_handover,
)
from control.range import RangeState
from jobs.orders import OrderBook
from jobs.shifts import ShiftCatalog


class DevicePortTests(unittest.TestCase):
    def _device(self):
        device = RecordedDevice('take-1', 'low', ('a', 'b'), capacity=2)
        device.enable_handover()
        return device

    def test_delivery_state_and_position_bounds(self):
        device = self._device()
        self.assertEqual(device.delivery_state(), {
            'recording_id': 'take-1', 'actual_range': 'low', 'pending_range': None,
            'live': (), 'confirmed': 0, 'tail': 0,
        })
        device.arm(0)
        device.next_event({'kind': 'exposure', 'step': 1, 'trigger_id': 'a'})
        state = device.delivery_state()
        self.assertEqual((state['confirmed'], state['tail']), (0, 1))
        self.assertEqual(state['live'], (('a', 0, 'low', False),))
        self.assertEqual([p for p, _ in device.read_notices(0)], [1])
        self.assertEqual(device.read_notices(1), ())
        with self.assertRaises(ValueError):
            device.read_notices(-1)
        with self.assertRaises(ValueError):
            device.read_notices(2)
        device.confirm_notices(1)
        self.assertEqual((device.delivery_state()['confirmed'], device.delivery_state()['tail']), (1, 1))
        # Repeating the same confirmation is safe; reading below it is not.
        device.confirm_notices(1)
        with self.assertRaises(ValueError):
            device.read_notices(0)
        # Recycled exposure payloads can no longer be fetched from the trace.
        self.assertFalse(any(item.get('step') == 1 for item in device.trace))

    def test_replay_can_only_reuse_retained_notices(self):
        device = self._device()
        device.arm(0)
        device.next_event({'kind': 'exposure', 'step': 1, 'trigger_id': 'a'})
        completed = device.next_event({
            'kind': 'completed', 'step': 2, 'trigger_id': 'a', 'slot': 0,
            'exposure_step': 1, 'raw_code': 5,
        })
        replayed = device.next_event({'kind': 'replay', 'step': 3, 'of_step': 2})
        self.assertEqual(replayed.step, completed.step)
        self.assertEqual([p for p, _ in device.read_notices(0)], [1, 2, 3])
        # Confirming through position 3 recycles both the original completion
        # and its replay, so neither can be fetched through replay any more.
        device.confirm_notices(3)
        with self.assertRaises(ValueError):
            device.next_event({'kind': 'replay', 'step': 4, 'of_step': 2})

    def test_handover_ports_do_not_change_unenabled_recordings(self):
        device = RecordedDevice('plain', 'low', ('a',))
        # Ports exist but report an empty, unconfirmed delivery channel and
        # next_event does not enqueue notices while handover is disabled.
        self.assertEqual(device.delivery_state()['tail'], 0)
        device.arm(0)
        device.next_event({'kind': 'exposure', 'step': 1, 'trigger_id': 'a'})
        self.assertEqual(device.delivery_state()['tail'], 0)
        self.assertTrue(any(item.get('step') == 1 for item in device.trace))
        device.enable_handover()
        device.next_event({'kind': 'exposure', 'step': 2, 'trigger_id': 'a'})
        self.assertEqual(device.delivery_state()['tail'], 1)

    def test_invalid_recording_action_does_not_occupy_a_position(self):
        device = self._device()
        with self.assertRaises(ValueError):
            device.next_event({'kind': 'replay', 'step': 1, 'of_step': 99})
        self.assertEqual(device.delivery_state()['tail'], 0)
        self.assertEqual(device.trace, ())


class HandoverFlowTests(unittest.TestCase):
    def _store(self, database=':memory:'):
        connection = open_store(database)
        self.addCleanup(connection.close)
        initialize_archive(connection)
        ShiftCatalog(connection).ensure_shift('day')
        ranges = RangeState(('low', 'high'), 'low')
        book = OrderBook(connection, ranges)
        book.register('day', 'A', 'low', Calibration('low', 'L1', '1/6', '-1/6'))
        book.register('day', 'B', 'low', Calibration('low', 'L2', '1/5', 0))
        book.register('day', 'C', 'high', Calibration('high', 'H1', '1/4', -1))
        return connection, ranges

    def _device(self):
        return RecordedDevice('take-1', 'low', ('a', 'b', 'c'))

    def test_handover_advance_returns_last_sample_and_recycles_prefix(self):
        connection, ranges = self._store()
        device = self._device()
        session = start_handover(connection, 'day', ranges, device)
        self.assertIsInstance(session, HandoverSession)
        self.assertIsNone(advance(session))
        self.assertEqual([item['trigger_id'] for item in device.trace if item['kind'] == 'arm'], ['a', 'b'])
        device.next_event({'kind': 'exposure', 'step': 10, 'trigger_id': 'a'})
        device.next_event({
            'kind': 'completed', 'step': 20, 'trigger_id': 'a', 'slot': 0,
            'exposure_step': 10, 'raw_code': 5,
        })
        published = advance(session)
        self.assertEqual(published.sample.order_id, 'A')
        state = device.delivery_state()
        # Two notices (exposure, completion) were settled; arming is a command
        # trajectory entry, not a delivery position.
        self.assertEqual((state['confirmed'], state['tail']), (2, 2))
        with self.assertRaises(ValueError):
            device.read_notices(1)
        # An explicit event is rejected: notices come only from the device port.
        with self.assertRaises(ValueError):
            advance(session, RangeApplied(1, 'high', 1))

    def test_wrong_recording_and_missing_notice_are_rejected_without_commands(self):
        class GapDevice(RecordedDevice):
            omit_position = None

            def read_notices(self, after):
                return tuple(
                    item for item in super().read_notices(after) if item[0] != self.omit_position
                )

        with TemporaryDirectory(prefix='test-handover-') as directory:
            database = Path(directory) / 'shift.sqlite3'
            connection, ranges = self._store(database)
            device = GapDevice('take-1', 'low', ('a', 'b', 'c'))
            session = start_handover(connection, 'day', ranges, device)
            advance(session)
            device.next_event({'kind': 'exposure', 'step': 10, 'trigger_id': 'a'})
            device.next_event({
                'kind': 'completed', 'step': 20, 'trigger_id': 'a', 'slot': 0,
                'exposure_step': 10, 'raw_code': 5,
            })
            advance(session)
            # Two more notices arrive while the store is about to close.
            device.next_event({'kind': 'exposure', 'step': 30, 'trigger_id': 'b'})
            missing = device.delivery_state()['tail']
            connection.close()

            foreign = RecordedDevice('take-2', 'low', ('a', 'b', 'c'))
            foreign.enable_handover()
            with closing(open_store(database)) as reopened:
                before = foreign.trace
                with self.assertRaises(ValueError):
                    resume(reopened, 'day', foreign)
                self.assertEqual(foreign.trace, before)

            # Hiding one notice creates a gap: take-over must be refused.
            device.omit_position = missing
            with closing(open_store(database)) as reopened:
                saved = read_sample(reopened, sample_id_for('day', 'A'))
                with self.assertRaises(ValueError):
                    resume(reopened, 'day', device)
                # Prior achievement is preserved and nothing was dispatched.
                self.assertEqual(read_sample(reopened, sample_id_for('day', 'A')), saved)
            self.assertEqual(
                [item['kind'] for item in device.trace if item['kind'] == 'request_range'], []
            )

            # Once the evidence is complete, recovery succeeds and settles b's
            # exposure without dispatching new commands during resume.
            device.omit_position = None
            with closing(open_store(database)) as reopened:
                resumed = resume(reopened, 'day', device)
                self.assertEqual(device.delivery_state()['confirmed'], device.delivery_state()['tail'])
                self.assertEqual(resumed.ledger.get('b').exposure.step, 30)
            self.assertEqual(
                [item['kind'] for item in device.trace if item['kind'] == 'request_range'], []
            )

    def test_armed_trigger_keeps_its_slot_until_physical_termination(self):
        connection, ranges = self._store()
        device = self._device()
        session = start_handover(connection, 'day', ranges, device)
        advance(session)
        session.cancel('B', stop=True)
        self.assertTrue(device.delivery_state()['live'] == (
            ('a', 0, 'low', False), ('b', 1, 'low', True),
        ))
        device.next_event({'kind': 'stop_accepted', 'step': 15, 'trigger_id': 'b', 'slot': 1})
        device.next_event({'kind': 'exposure', 'step': 18, 'trigger_id': 'b'})
        device.next_event({'kind': 'stopped', 'step': 25, 'trigger_id': 'b', 'slot': 1})
        advance(session)
        # No new arm identity was consumed and b kept its original slot.
        self.assertEqual(
            [item['trigger_id'] for item in device.trace if item['kind'] == 'arm'], ['a', 'b']
        )
        self.assertEqual(
            [item['slot'] for item in device.trace if item['kind'] == 'arm'], [0, 1]
        )
        # The bench stays on low until the still-live a reaches its own
        # physical terminal; the high order is not requested early.
        self.assertEqual(
            [item['kind'] for item in device.trace if item['kind'] == 'request_range'], []
        )
        device.next_event({'kind': 'exposure', 'step': 28, 'trigger_id': 'a'})
        device.next_event({
            'kind': 'completed', 'step': 30, 'trigger_id': 'a', 'slot': 0,
            'exposure_step': 28, 'raw_code': 5,
        })
        advance(session)
        self.assertEqual(
            [item['target'] for item in device.trace if item['kind'] == 'request_range'], ['high']
        )
        self.assertIsNone(read_sample(connection, sample_id_for('day', 'B')))

    def test_in_flight_range_change_is_not_re_requested_after_resume(self):
        with TemporaryDirectory(prefix='test-handover-range-') as directory:
            database = Path(directory) / 'shift.sqlite3'
            connection, ranges = self._store(database)
            device = self._device()
            session = start_handover(connection, 'day', ranges, device)
            advance(session)
            # Terminate both low triggers so the high order becomes due.
            for step, name, slot, exposure in (
                (10, 'a', 0, 5), (30, 'b', 1, 17),
            ):
                device.next_event({'kind': 'exposure', 'step': step - 1, 'trigger_id': name})
                device.next_event({
                    'kind': 'completed', 'step': step, 'trigger_id': name, 'slot': slot,
                    'exposure_step': step - 1, 'raw_code': exposure,
                })
            advance(session)
            request = next(item for item in device.trace if item['kind'] == 'request_range')
            connection.close()

            # Matching ack arrives while no duty session exists.
            device.next_event({
                'kind': 'range_applied', 'step': 40,
                'command_id': request['command_id'], 'range_name': 'high',
            })
            with closing(open_store(database)) as reopened:
                resumed = resume(reopened, 'day', device)
                # The same command id is reused; no duplicate range request yet.
                self.assertEqual(
                    [item['command_id'] for item in device.trace if item['kind'] == 'request_range'],
                    [request['command_id']],
                )
                advance(resumed)
                self.assertEqual(
                    [item['command_id'] for item in device.trace if item['kind'] == 'request_range'],
                    [request['command_id']],
                )
                self.assertEqual(resumed.ranges.current, 'high')
                self.assertEqual(
                    [item['trigger_id'] for item in device.trace if item['kind'] == 'arm'],
                    ['a', 'b', 'c'],
                )
                device.next_event({'kind': 'exposure', 'step': 80, 'trigger_id': 'c'})
                device.next_event({
                    'kind': 'completed', 'step': 90, 'trigger_id': 'c', 'slot': 0,
                    'exposure_step': 80, 'raw_code': 4,
                })
                published = advance(resumed)
                self.assertEqual(published.sample.order_id, 'C')
                self.assertEqual(str(published.sample.voltage), '0')

    def test_settled_sample_survives_unconfirmed_cutover_and_keeps_receipt(self):
        class Cutover(Exception):
            pass

        class FlakyDevice(RecordedDevice):
            cut_at = None

            def confirm_notices(self, through):
                if self.cut_at is not None and through >= self.cut_at:
                    raise Cutover()
                return super().confirm_notices(through)

        with TemporaryDirectory(prefix='test-handover-cut-') as directory:
            database = Path(directory) / 'shift.sqlite3'
            connection, ranges = self._store(database)
            device = FlakyDevice('take-1', 'low', ('a', 'b', 'c'))
            session = start_handover(connection, 'day', ranges, device)
            advance(session)
            device.next_event({'kind': 'exposure', 'step': 10, 'trigger_id': 'a'})
            device.next_event({
                'kind': 'completed', 'step': 20, 'trigger_id': 'a', 'slot': 0,
                'exposure_step': 10, 'raw_code': 5,
            })
            advance(session)
            device.next_event({'kind': 'exposure', 'step': 35, 'trigger_id': 'b'})
            device.next_event({
                'kind': 'completed', 'step': 50, 'trigger_id': 'b', 'slot': 1,
                'exposure_step': 35, 'raw_code': 17,
            })
            device.cut_at = 4
            with self.assertRaises(Cutover):
                advance(session)
            device.cut_at = None
            connection.close()

            with closing(open_store(database)) as reopened:
                # The submitted sample and its receipt survived; no duplicate is made.
                before = read_sample(reopened, sample_id_for('day', 'B'))
                self.assertIsNotNone(before)
                resumed = resume(reopened, 'day', device)
                after = read_sample(reopened, sample_id_for('day', 'B'))
                self.assertEqual(after, before)
                self.assertEqual(device.delivery_state()['confirmed'], device.delivery_state()['tail'])
                advance(resumed)
                self.assertEqual(read_sample(reopened, sample_id_for('day', 'B')), before)


if __name__ == '__main__':
    unittest.main()
