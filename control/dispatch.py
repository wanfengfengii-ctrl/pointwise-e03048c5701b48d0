import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, is_dataclass
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.publish import publish_sample
from archive.read import list_shift_samples
from archive.records import ArchivedSample
from archive.schema import initialize_archive, open_store
from audit.report import review_shift
from capture.accept import accept_event
from capture.convert import Calibration
from capture.device import RecordedDevice
from capture.events import DeviceEvent, RangeApplied
from capture.triggers import TriggerLedger
from control.ack import apply_range_ack
from control.occupancy import Occupancy
from control.range import RangeState
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
