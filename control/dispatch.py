import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, is_dataclass
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.publish import publish_sample, publish_sample_transaction
from archive.read import list_shift_samples
from archive.records import ArchivedSample
from archive.schema import initialize_archive, open_store
from audit.report import review_shift
from capture.accept import accept_event
from capture.convert import Calibration
from capture.device import RecordedDevice
from capture.events import DeviceEvent, Exposure, RangeApplied
from capture.triggers import TerminalFact, TriggerBinding, TriggerLedger
from control import handover_store
from control.ack import apply_range_ack
from control.handover_store import checkpoint, load_bindings, load_handover, mark_confirmed
from control.occupancy import Occupancy
from control.range import RangeChange, RangeState
from control.start import start_next
from jobs.cancel import cancel
from jobs.orders import OrderBook, OrderStatus
from jobs.shifts import ShiftCatalog


class Session:
    def __init__(self, connection: sqlite3.Connection, shift_id: str,
                 ranges: RangeState, device: RecordedDevice):
        ShiftCatalog(connection).require_shift(shift_id)
        if ranges.current != device.actual_range or ranges.pending is not None:
            raise ValueError('session requires an initially synchronized device')
        self.shift_id = shift_id
        self.orders = OrderBook(connection, ranges)
        self.ranges = ranges
        self.device = device
        self.ledger = TriggerLedger(device.recording_id)
        self.occupancy = Occupancy(device.capacity)

    def cancel(self, order_id: str, *, stop: bool = False) -> OrderStatus:
        status = cancel(self.orders, self.shift_id, order_id)
        if stop:
            binding = self.ledger.live_for(self.shift_id, order_id)
            if binding is not None and self.device.request_stop(binding.trigger_id):
                self.ledger.mark_stop_requested(binding.trigger_id)
        return status


def advance(session: Session, event: DeviceEvent | None = None) -> ArchivedSample | None:
    if isinstance(session, HandoverSession):
        if event is not None:
            raise ValueError('handover sessions consume notices from the device port, not events')
        return drain_notices(session)
    published = None
    if isinstance(event, RangeApplied):
        apply_range_ack(session.ranges, event)
    elif event is not None:
        captured = accept_event(session.ledger, session.orders, event)
        if captured.terminal is not None:
            session.occupancy.release(captured.terminal)
        if captured.candidate is not None:
            published = publish_sample(session.orders, captured.candidate)

    while start_next(
        session.orders, session.shift_id, session.ranges, session.occupancy, session.device, session.ledger
    ):
        pass
    return published


class HandoverSession(Session):
    """A duty session whose device keeps running while the store may reopen."""

    def __init__(self, connection: sqlite3.Connection, shift_id: str, ranges: RangeState,
                 device: RecordedDevice, *, confirmed: int = 0, ledger: TriggerLedger | None = None):
        # Do not require initial synchronization: a reopened session may hold
        # an in-flight range change and a range already applied on the bench.
        ShiftCatalog(connection).require_shift(shift_id)
        self.shift_id = shift_id
        self.orders = OrderBook(connection, ranges)
        self.ranges = ranges
        self.device = device
        self.ledger = ledger if ledger is not None else TriggerLedger(device.recording_id)
        self.occupancy = Occupancy(device.capacity)
        self._confirmed = confirmed
        self._settled = confirmed

    @classmethod
    def open(cls, connection: sqlite3.Connection, shift_id: str, ranges: RangeState,
             device: RecordedDevice, *, confirmed: int, ledger: TriggerLedger | None = None):
        return cls(connection, shift_id, ranges, device, confirmed=confirmed, ledger=ledger)

    def cancel(self, order_id: str, *, stop: bool = False) -> OrderStatus:
        connection = self.orders.connection
        connection.execute('BEGIN IMMEDIATE')
        with connection:
            status = cancel(self.orders, self.shift_id, order_id)
            if stop:
                binding = self.ledger.live_for(self.shift_id, order_id)
                if binding is not None and self.device.request_stop(binding.trigger_id):
                    self.ledger.mark_stop_requested(binding.trigger_id)
            checkpoint(connection, self.shift_id, self, self._confirmed)
        return status


def start_handover(connection: sqlite3.Connection, shift_id: str, ranges: RangeState,
                   device: RecordedDevice) -> HandoverSession:
    """Enable handover on a fresh recording before its first command is sent."""
    ShiftCatalog(connection).require_shift(shift_id)
    if ranges.current != device.actual_range or ranges.pending is not None:
        raise ValueError('handover requires an initially synchronized device')
    state = device.delivery_state()
    if state['tail'] != 0 or state['confirmed'] != 0 or state['live'] or state['pending_range'] is not None:
        raise ValueError('handover can only start before the first command of the recording')
    if device._next_name != 0:
        raise ValueError('handover can only start before the first command of the recording')
    # Binding itself sends no commands; it arms the delivery ports so notices
    # produced while no duty session exists are still handed over.
    device.enable_handover()
    connection.execute('BEGIN IMMEDIATE')
    with connection:
        handover_store.begin_handover(connection, shift_id, device.recording_id, ranges)
        checkpoint(connection, shift_id, _HandoverSeed(ranges, device), 0)
    return HandoverSession.open(connection, shift_id, ranges, device, confirmed=0)


class _HandoverSeed:
    """Minimal session view for the initial handover checkpoint."""

    def __init__(self, ranges: RangeState, device: RecordedDevice):
        self.ranges = ranges
        self.device = device
        self.ledger = TriggerLedger(device.recording_id)


def _rebuild_ranges(row: sqlite3.Row) -> RangeState:
    supported = tuple(json.loads(row['ranges_text']))
    ranges = RangeState(supported, row['initial_range'])
    ranges.current = row['current_range']
    ranges._next_command_id = row['next_command_id']
    if row['pending_command_id'] is not None:
        ranges.pending = RangeChange(row['pending_command_id'], row['pending_target'])
    return ranges


def _rebuild_ledger(rows: tuple[sqlite3.Row, ...], recording_id: str) -> TriggerLedger:
    ledger = TriggerLedger(recording_id)
    for row in rows:
        exposure = None
        if row['exposure_step'] is not None:
            exposure = Exposure(
                row['trigger_id'], row['exposure_slot'], row['range_name'],
                row['exposure_recording'], row['exposure_step'],
            )
        binding = TriggerBinding(
            row['trigger_id'], row['shift_id'], row['order_id'], row['slot'],
            row['range_name'], exposure,
            bool(row['stop_requested']), bool(row['stop_accepted']), None,
        )
        if row['terminal_reason'] is not None:
            binding.terminal = TerminalFact(
                binding.trigger_id, binding.slot, binding.range_name, row['terminal_reason']
            )
        ledger._bindings[binding.trigger_id] = binding
    return ledger


def _rebuild_occupancy(ledger: TriggerLedger, capacity: int) -> Occupancy:
    occupancy = Occupancy(capacity)
    for binding in ledger._bindings.values():
        if binding.terminal is None:
            occupancy.reserve(binding.trigger_id, binding.slot, binding.range_name)
    return occupancy


def _check_live_state(device: RecordedDevice, ledger: TriggerLedger) -> None:
    state = device.delivery_state()
    live = {trigger_id: (slot, range_name, stop_requested)
            for trigger_id, slot, range_name, stop_requested in state['live']}
    ledger_live = {
        binding.trigger_id: (binding.slot, binding.range_name, binding.stop_requested)
        for binding in ledger._bindings.values() if binding.terminal is None
    }
    if set(live) != set(ledger_live):
        raise ValueError('live triggers do not match the persisted handover evidence')
    for trigger_id, facts in ledger_live.items():
        if facts != live[trigger_id]:
            raise ValueError(f'live state for trigger {trigger_id!r} does not match the bench')
    if device._next_name != len(ledger._bindings):
        raise ValueError('armed trigger count does not match the persisted handover evidence')


def _check_range_state(device: RecordedDevice, ranges: RangeState) -> None:
    state = device.delivery_state()
    if state['actual_range'] != ranges.current:
        raise ValueError('actual bench range does not match the persisted handover evidence')
    pending = None if ranges.pending is None else (ranges.pending.command_id, ranges.pending.target)
    if state['pending_range'] != pending:
        raise ValueError('in-flight range change does not match the persisted handover evidence')


def _read_contiguous(device: RecordedDevice, after: int):
    notices = device.read_notices(after)
    expected = after + 1
    for position, _event in notices:
        if position != expected:
            raise ValueError(f'notice interval has a gap at position {expected}')
        expected += 1
    return notices


def _settle_notice(session: 'HandoverSession', position: int, event: DeviceEvent) -> ArchivedSample | None:
    connection = session.orders.connection
    published = None
    # Phase 1: business settlement and recovery evidence become durable at the
    # new position while the durable confirmation marker stays where the bench
    # has actually recycled.
    connection.execute('BEGIN IMMEDIATE')
    with connection:
        if isinstance(event, RangeApplied):
            apply_range_ack(session.ranges, event)
        else:
            captured = accept_event(session.ledger, session.orders, event)
            if captured.terminal is not None:
                session.occupancy.release(captured.terminal)
            if captured.candidate is not None:
                published = publish_sample_transaction(session.orders, captured.candidate)
        checkpoint(
            connection, session.shift_id, session,
            confirmed_position=session._confirmed, settled_position=position,
        )
        session._settled = position
    # Phase 2: ask the bench to recycle the notice. If this call fails, the
    # store already holds the settlement and the notice is simply reprocessed
    # idempotently at the next resume.
    session.device.confirm_notices(position)
    # Phase 3: only after the bench accepted confirmation do we advance the
    # durable confirmation marker.
    connection.execute('BEGIN IMMEDIATE')
    with connection:
        mark_confirmed(connection, session.shift_id, position)
    session._confirmed = position
    return published


def _schedule(session) -> None:
    """Run queue dispatch and persist any new arms or range commands."""
    connection = session.orders.connection
    connection.execute('BEGIN IMMEDIATE')
    with connection:
        while start_next(
            session.orders, session.shift_id, session.ranges,
            session.occupancy, session.device, session.ledger
        ):
            pass
        checkpoint(connection, session.shift_id, session, session._confirmed)


def _drain_only(session: 'HandoverSession') -> ArchivedSample | None:
    """Settle every available notice without opening new scheduling."""
    last_published = None
    state = session.device.delivery_state()
    after = session._confirmed
    if state['confirmed'] != after:
        raise ValueError('device confirmation position does not match the persisted handover evidence')
    notices = _read_contiguous(session.device, after)
    for position, event in notices:
        published = _settle_notice(session, position, event)
        if published is not None:
            last_published = published
    return last_published


def drain_notices(session: 'HandoverSession') -> ArchivedSample | None:
    last_published = _drain_only(session)
    # Open scheduling again only after the whole pending interval is consumed.
    _schedule(session)
    return last_published


def resume(connection: sqlite3.Connection, shift_id: str, device: RecordedDevice) -> HandoverSession:
    """Reopen a handover session, verifying source and continuation evidence."""
    ShiftCatalog(connection).require_shift(shift_id)
    row = load_handover(connection, shift_id)
    if row is None:
        raise ValueError(f'no handover evidence for shift {shift_id!r}')
    # Source verification happens before any command could be sent to the bench.
    if device.recording_id != row['recording_id']:
        raise ValueError('device recording does not match the persisted handover evidence')
    if not getattr(device, '_handover', False):
        raise ValueError('device handover ports must be enabled with enable_handover()')
    ranges = _rebuild_ranges(row)
    ledger = _rebuild_ledger(load_bindings(connection, shift_id), row['recording_id'])
    session = HandoverSession.open(
        connection, shift_id, ranges, device,
        confirmed=row['confirmed_position'], ledger=ledger
    )
    session._settled = row['settled_position']
    session.occupancy = _rebuild_occupancy(ledger, device.capacity)
    # The bench may have advanced since the store closed: its actual range and
    # live triggers are reconciled through the undecided notices below (an
    # in-flight shift may already be physically applied while its ack is still
    # pending), so only the durable confirmation position is verified up front.
    state = device.delivery_state()
    if state['confirmed'] != row['confirmed_position']:
        raise ValueError('device confirmation position does not match the persisted handover evidence')
    # Consume every undecided notice. Recovery only settles and verifies the
    # bench; new sampling, range and stop commands wait for later duty actions.
    if state['tail'] > state['confirmed']:
        _drain_only(session)
    final_state = device.delivery_state()
    if final_state['confirmed'] != final_state['tail']:
        raise ValueError('incomplete notice interval cannot be treated as consumed')
    # Only after the whole interval is settled may the bench's live state be
    # expected to match the recovered ledger, occupancy and range evidence.
    _check_range_state(device, session.ranges)
    _check_live_state(device, session.ledger)
    return session


def run_recording(recording: dict, database: str | Path) -> dict:
    shift_id = recording['shift_id']
    ranges = RangeState(recording['ranges'], recording['initial_range'])
    device = RecordedDevice(
        recording['recording_id'], ranges.current, recording['trigger_names'], capacity=recording['capacity']
    )
    with closing(open_store(database)) as connection:
        initialize_archive(connection)
        ShiftCatalog(connection).ensure_shift(shift_id)
        session = Session(connection, shift_id, ranges, device)
        for action in recording['actions']:
            match action['action']:
                case 'register':
                    basis = Calibration(action['range'], action['revision'], action['gain'], action['offset'])
                    session.orders.register(shift_id, action['order_id'], action['range'], basis)
                case 'advance':
                    advance(session)
                case 'cancel':
                    stop = action.get('stop', False)
                    if type(stop) is not bool:
                        raise ValueError('stop must be a boolean')
                    session.cancel(action['order_id'], stop=stop)
                case 'event':
                    advance(session, device.next_event(action['event']))
                case other:
                    raise ValueError(f'unknown recording action: {other!r}')
        trace = device.trace

    with closing(open_store(database)) as connection:
        return {
            'trace': trace,
            'samples': list_shift_samples(connection, shift_id),
            'report': review_shift(connection, shift_id),
        }


def _json_value(value: object) -> object:
    if isinstance(value, Fraction):
        return f'{value.numerator}/{value.denominator}'
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f'unsupported JSON value: {type(value).__name__}')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Replay a recorded voltage collection shift')
    parser.add_argument('recording', type=Path)
    args = parser.parse_args(argv)
    with args.recording.open(encoding='utf-8') as source:
        recording = json.load(source)
    with TemporaryDirectory(prefix='voltage-bench-') as directory:
        result = run_recording(recording, Path(directory) / 'archive.sqlite3')
    print(json.dumps(result, default=_json_value, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
