import sqlite3
from dataclasses import dataclass
from fractions import Fraction

from archive.receipts import SuccessReceipt
from capture.convert import Calibration
from capture.events import Exposure


@dataclass(frozen=True, slots=True)
class SampleRecord:
    sample_id: str
    shift_id: str
    order_id: str
    sequence: int
    raw_code: int
    calibration: Calibration
    voltage: Fraction
    exposure: Exposure

    @property
    def actual_range(self) -> str:
        return self.exposure.range_name

    @property
    def trigger_id(self) -> str:
        return self.exposure.trigger_id


@dataclass(frozen=True, slots=True)
class ArchivedSample:
    sample: SampleRecord
    receipt: SuccessReceipt


def encode_sample(sample: SampleRecord) -> tuple[str | int, ...]:
    basis = sample.calibration
    source = sample.exposure
    return (
        sample.sample_id, sample.shift_id, sample.order_id, source.trigger_id, str(sample.raw_code),
        source.range_name, basis.revision, str(basis.gain.numerator), str(basis.gain.denominator),
        str(basis.offset.numerator), str(basis.offset.denominator), str(sample.voltage.numerator),
        str(sample.voltage.denominator), source.recording_id, source.step, source.slot,
    )


def decode_sample(row: sqlite3.Row) -> SampleRecord:
    basis = Calibration(
        row['actual_range'], row['revision'],
        Fraction(int(row['gain_n']), int(row['gain_d'])),
        Fraction(int(row['offset_n']), int(row['offset_d'])),
    )
    voltage = Fraction(int(row['voltage_n']), int(row['voltage_d']))
    source = Exposure(row['trigger_id'], row['slot'], row['actual_range'], row['recording_id'], row['exposure_step'])
    return SampleRecord(
        row['sample_id'], row['shift_id'], row['order_id'], row['sequence'],
        int(row['raw_code']), basis, voltage, source,
    )
