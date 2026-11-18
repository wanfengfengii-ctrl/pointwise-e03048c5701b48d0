from dataclasses import dataclass
from fractions import Fraction


def _coefficient(value: Fraction | int | str) -> Fraction:
    if not isinstance(value, Fraction) and type(value) not in (int, str):
        raise ValueError('coefficient must be an exact rational number')
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError('invalid rational coefficient') from error


@dataclass(frozen=True, slots=True, init=False)
class Calibration:
    range_name: str
    revision: str
    gain: Fraction
    offset: Fraction

    def __init__(self, range_name: str, revision: str, gain: Fraction | int | str,
                 offset: Fraction | int | str):
        if not isinstance(range_name, str) or not range_name.strip():
            raise ValueError('range name must be a nonempty string')
        if not isinstance(revision, str) or not revision.strip():
            raise ValueError('calibration revision must be a nonempty string')
        object.__setattr__(self, 'range_name', range_name)
        object.__setattr__(self, 'revision', revision)
        object.__setattr__(self, 'gain', _coefficient(gain))
        object.__setattr__(self, 'offset', _coefficient(offset))


def validate_raw_code(raw_code: object) -> int:
    if type(raw_code) is not int:
        raise ValueError('raw code must be an integer')
    return raw_code


def convert_voltage(raw_code: object, calibration: Calibration, actual_range: str) -> Fraction:
    if actual_range != calibration.range_name:
        raise ValueError('actual range does not match the frozen calibration')
    return validate_raw_code(raw_code) * calibration.gain + calibration.offset
