from dataclasses import dataclass

from archive.records import SampleRecord
from capture.events import Exposure


@dataclass(frozen=True, slots=True)
class ExtremeSource:
    sample_id: str
    shift_id: str
    order_id: str
    trigger_id: str
    actual_range: str
    exposure: Exposure


def source_for_sample(sample: SampleRecord) -> ExtremeSource:
    return ExtremeSource(
        sample.sample_id, sample.shift_id, sample.order_id, sample.trigger_id,
        sample.actual_range, sample.exposure,
    )
