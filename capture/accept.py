from dataclasses import dataclass
from fractions import Fraction

from capture.convert import convert_voltage, validate_raw_code
from capture.events import Completed, DeviceEvent, Exposure, StopAccepted, Stopped
from capture.triggers import TerminalFact, TriggerLedger
from jobs.orders import Order, OrderBook, OrderStatus


@dataclass(frozen=True, slots=True)
class SampleCandidate:
    order: Order
    exposure: Exposure
    raw_code: int
    voltage: Fraction


@dataclass(frozen=True, slots=True)
class CaptureResult:
    terminal: TerminalFact | None = None
    candidate: SampleCandidate | None = None


def accept_event(ledger: TriggerLedger, book: OrderBook, event: DeviceEvent) -> CaptureResult:
    if isinstance(event, Exposure):
        ledger.record_exposure(event)
        return CaptureResult()
    if isinstance(event, StopAccepted):
        ledger.record_stop_accepted(event)
        return CaptureResult()
    if not isinstance(event, (Completed, Stopped)):
        return CaptureResult()

    terminal = ledger.close(event)
    if terminal is None or isinstance(event, Stopped):
        return CaptureResult(terminal)
    binding = ledger.get(terminal.trigger_id)
    assert binding is not None
    order = book.get_order(binding.shift_id, binding.order_id)
    exposure = binding.exposure
    if order.status != OrderStatus.CAPTURING or exposure is None:
        return CaptureResult(terminal)
    if (type(event.exposure_step) is not int or event.exposure_step != exposure.step
            or exposure.range_name != binding.range_name):
        return CaptureResult(terminal)
    try:
        raw_code = validate_raw_code(event.raw_code)
        voltage = convert_voltage(raw_code, order.calibration, exposure.range_name)
    except ValueError:
        return CaptureResult(terminal)
    return CaptureResult(terminal, SampleCandidate(order, exposure, raw_code, voltage))
