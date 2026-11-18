from collections.abc import Iterable
from fractions import Fraction

from archive.records import SampleRecord
from capture.convert import convert_voltage


ValuedSample = tuple[Fraction, SampleRecord]


def recompute_voltage(sample: SampleRecord) -> Fraction:
    voltage = convert_voltage(sample.raw_code, sample.calibration, sample.actual_range)
    if voltage != sample.voltage:
        raise ValueError(f'archived voltage does not match raw evidence: {sample.sample_id}')
    return voltage


def find_extrema(samples: Iterable[SampleRecord]) -> tuple[ValuedSample | None, ValuedSample | None]:
    lowest = highest = None
    for sample in samples:
        value = recompute_voltage(sample)
        if lowest is None or (value, sample.sequence) < (lowest[0], lowest[1].sequence):
            lowest = (value, sample)
        if highest is None or (value, -sample.sequence) > (highest[0], -highest[1].sequence):
            highest = (value, sample)
    return lowest, highest
