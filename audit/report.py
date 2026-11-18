import sqlite3
from dataclasses import dataclass
from fractions import Fraction

from archive.read import list_shift_samples
from audit.coverage import Coverage, classify_coverage
from audit.provenance import ExtremeSource, source_for_sample
from audit.voltage import ValuedSample, find_extrema
from jobs.orders import OrderBook
from jobs.shifts import ShiftCatalog


@dataclass(frozen=True, slots=True)
class Extremum:
    voltage: Fraction
    source: ExtremeSource


@dataclass(frozen=True, slots=True)
class ShiftReport:
    shift_id: str
    coverage: Coverage
    minimum: Extremum | None
    maximum: Extremum | None


def _extremum(valued_sample: ValuedSample | None) -> Extremum | None:
    if valued_sample is None:
        return None
    value, sample = valued_sample
    return Extremum(value, source_for_sample(sample))


def review_shift(connection: sqlite3.Connection, shift_id: str) -> ShiftReport:
    connection.execute('BEGIN')
    with connection:
        ShiftCatalog(connection).require_shift(shift_id)
        orders = OrderBook(connection).list_shift_orders(shift_id)
        samples = tuple(archived.sample for archived in list_shift_samples(connection, shift_id))
    coverage = classify_coverage(orders, samples)
    minimum, maximum = find_extrema(samples)
    return ShiftReport(shift_id, coverage, _extremum(minimum), _extremum(maximum))
