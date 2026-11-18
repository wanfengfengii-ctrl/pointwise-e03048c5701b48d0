from collections.abc import Iterable
from dataclasses import dataclass

from archive.records import SampleRecord
from jobs.orders import Order, OrderStatus


@dataclass(frozen=True, slots=True)
class Coverage:
    succeeded: tuple[str, ...]
    canceled: tuple[str, ...]
    missing: tuple[str, ...]


def classify_coverage(orders: Iterable[Order], samples: Iterable[SampleRecord]) -> Coverage:
    sample_keys = {(sample.shift_id, sample.order_id) for sample in samples}
    succeeded = []
    canceled = []
    missing = []
    for order in orders:
        if (order.shift_id, order.order_id) in sample_keys:
            succeeded.append(order.order_id)
        elif order.status == OrderStatus.CANCELED:
            canceled.append(order.order_id)
        else:
            missing.append(order.order_id)
    return Coverage(tuple(succeeded), tuple(canceled), tuple(missing))
