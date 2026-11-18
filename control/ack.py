from capture.events import RangeApplied
from control.range import RangeState


def apply_range_ack(ranges: RangeState, event: RangeApplied) -> bool:
    return ranges.confirm_change(event.command_id, event.range_name)
