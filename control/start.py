from capture.device import RecordedDevice
from capture.triggers import TriggerLedger
from control.occupancy import Occupancy
from control.range import RangeState
from jobs.orders import OrderBook
from jobs.queue import next_eligible_order
from jobs.status import mark_started


def start_next(book: OrderBook, shift_id: str, ranges: RangeState, occupancy: Occupancy,
               device: RecordedDevice, ledger: TriggerLedger) -> bool:
    if ranges.pending is not None:
        return False
    slot = occupancy.free_slot()
    if slot is None:
        return False
    required = None if occupancy.can_change_range() else ranges.current
    order = next_eligible_order(book, shift_id, required_range=required)
    if order is None:
        return False
    if order.requested_range != ranges.current:
        change = ranges.request_change(order.requested_range)
        device.request_range(change.command_id, change.target)
        return True

    # Admission to the serial device and the next trigger identity can be checked
    # before the persistent start. The port does not consume the name until arm.
    name = device.check_arm(slot)
    if ledger.get(name) is not None:
        raise ValueError(f'trigger identity already bound: {name!r}')
    if not mark_started(book, order.shift_id, order.order_id):
        return False
    trigger_id = device.arm(slot)
    ledger.bind(trigger_id, order.shift_id, order.order_id, slot, ranges.current)
    occupancy.reserve(trigger_id, slot, ranges.current)
    return True
