from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RangeApplied:
    command_id: int
    range_name: str
    step: int


@dataclass(frozen=True, slots=True)
class Exposure:
    trigger_id: str
    slot: int
    range_name: str
    recording_id: str
    step: int


@dataclass(frozen=True, slots=True)
class Completed:
    trigger_id: str
    slot: int
    exposure_step: object
    raw_code: object
    step: int


@dataclass(frozen=True, slots=True)
class StopAccepted:
    trigger_id: str
    slot: int
    step: int


@dataclass(frozen=True, slots=True)
class Stopped:
    trigger_id: str
    slot: int
    step: int


DeviceEvent = RangeApplied | Exposure | Completed | StopAccepted | Stopped


def _number(raw: Mapping, field: str, minimum: int) -> int:
    value = raw.get(field)
    if type(value) is not int or value < minimum:
        raise ValueError(f'{field} must be an integer >= {minimum}')
    return value


def _text(raw: Mapping, field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be a nonempty string')
    return value


def decode_device_event(raw: Mapping) -> DeviceEvent:
    kind = raw.get('kind')
    step = _number(raw, 'step', 1)
    if kind == 'range_applied':
        return RangeApplied(_number(raw, 'command_id', 1), _text(raw, 'range_name'), step)
    if kind not in ('exposure', 'completed', 'stop_accepted', 'stopped'):
        raise ValueError(f'unknown device event: {kind!r}')
    trigger_id = _text(raw, 'trigger_id')
    slot = _number(raw, 'slot', 0)
    match kind:
        case 'exposure':
            return Exposure(trigger_id, slot, _text(raw, 'range_name'), _text(raw, 'recording_id'), step)
        case 'completed':
            return Completed(trigger_id, slot, raw.get('exposure_step'), raw.get('raw_code'), step)
        case 'stop_accepted':
            return StopAccepted(trigger_id, slot, step)
        case 'stopped':
            return Stopped(trigger_id, slot, step)
    raise AssertionError('validated device event has no decoder')
