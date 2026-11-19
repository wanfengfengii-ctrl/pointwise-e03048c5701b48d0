from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass

from capture.events import Completed, DeviceEvent, Exposure, RangeApplied, Stopped, decode_device_event


_NOTIFICATION_KINDS = frozenset((
    'range_applied', 'exposure', 'completed', 'stop_accepted', 'stopped', 'replay',
))


@dataclass(slots=True)
class _Arm:
    slot: int
    range_name: str
    stop_requested: bool = False
    live: bool = True


class RecordedDevice:
    def __init__(self, recording_id: str, initial_range: str, trigger_names: Iterable[str], *, capacity: int = 2):
        if not isinstance(recording_id, str) or not recording_id.strip():
            raise ValueError('recording_id must be a nonempty string')
        if not isinstance(initial_range, str) or not initial_range.strip():
            raise ValueError('initial range must be a nonempty string')
        if type(capacity) is not int or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        names = tuple(trigger_names)
        if any(not isinstance(name, str) or not name.strip() for name in names) or len(set(names)) != len(names):
            raise ValueError('recording trigger names must be unique nonempty strings')
        self.recording_id = recording_id
        self.actual_range = initial_range
        self.capacity = capacity
        self._names = names
        self._next_name = 0
        self._pending_range: tuple[int, str] | None = None
        self._active_slots: dict[int, str] = {}
        self._arms: dict[str, _Arm] = {}
        self._events: dict[int, DeviceEvent] = {}
        self._last_step = 0
        self._trace: list[dict] = []
        self._handover = False
        self._mailbox: list[DeviceEvent] = []
        self._confirmed = 0

    @property
    def trace(self) -> tuple[dict, ...]:
        return tuple(dict(item) for item in self._trace)

    def request_range(self, command_id: int, target: str) -> None:
        if type(command_id) is not int or command_id < 1 or not isinstance(target, str) or not target.strip():
            raise ValueError('invalid range command')
        if self._pending_range is not None:
            raise ValueError('the device already has a pending range command')
        self._pending_range = (command_id, target)
        self._trace.append({'kind': 'request_range', 'command_id': command_id, 'target': target})

    def check_arm(self, slot: int) -> str:
        if type(slot) is not int or not 0 <= slot < self.capacity or slot in self._active_slots:
            raise ValueError(f'device slot unavailable: {slot!r}')
        if self._next_name == len(self._names):
            raise ValueError('recording has no trigger name for the next start')
        return self._names[self._next_name]

    def arm(self, slot: int) -> str:
        trigger_id = self.check_arm(slot)
        self._next_name += 1
        self._arms[trigger_id] = _Arm(slot, self.actual_range)
        self._active_slots[slot] = trigger_id
        self._trace.append({
            'kind': 'arm', 'trigger_id': trigger_id, 'slot': slot, 'range_name': self.actual_range,
        })
        return trigger_id

    def request_stop(self, trigger_id: str) -> bool:
        armed = self._arms.get(trigger_id)
        if armed is None or not armed.live or armed.stop_requested:
            return False
        armed.stop_requested = True
        self._trace.append({'kind': 'request_stop', 'trigger_id': trigger_id, 'slot': armed.slot})
        return True

    def enable_handover(self) -> None:
        """Turn on the handover delivery ports before the first duty command."""
        self._handover = True

    def delivery_state(self) -> dict:
        live = tuple(sorted(
            (trigger_id, armed.slot, armed.range_name, armed.stop_requested)
            for trigger_id, armed in self._arms.items() if armed.live
        ))
        return {
            'recording_id': self.recording_id,
            'actual_range': self.actual_range,
            'pending_range': self._pending_range,
            'live': live,
            'confirmed': self._confirmed,
            'tail': self._confirmed + len(self._mailbox),
        }

    def _check_position(self, position: int) -> None:
        if type(position) is not int or not self._confirmed <= position <= self._confirmed + len(self._mailbox):
            raise ValueError(
                f'position must be between confirmed {self._confirmed} and tail {self._confirmed + len(self._mailbox)}'
            )

    def read_notices(self, after: int) -> tuple[tuple[int, DeviceEvent], ...]:
        self._check_position(after)
        return tuple(
            (self._confirmed + index + 1, event)
            for index, event in enumerate(self._mailbox[after - self._confirmed:])
        )

    def confirm_notices(self, through: int) -> int:
        self._check_position(through)
        consumed = through - self._confirmed
        recycled_steps = {event.step for event in self._mailbox[:consumed]}
        del self._mailbox[:consumed]
        self._confirmed = through
        if self._handover and recycled_steps:
            # Confirmed business payloads are recycled. The recording command
            # trajectory (arms, requests, stops) stays available for diagnosis;
            # confirmed notification events can no longer be fetched from it.
            self._trace = [
                entry for entry in self._trace
                if not (entry.get('kind') in _NOTIFICATION_KINDS and entry.get('step') in recycled_steps)
            ]
        return through

    def _observe(self, event: DeviceEvent) -> None:
        if isinstance(event, RangeApplied):
            if self._pending_range == (event.command_id, event.range_name):
                self.actual_range = event.range_name
                self._pending_range = None
        elif isinstance(event, (Completed, Stopped)):
            armed = self._arms.get(event.trigger_id)
            if armed is None or not armed.live or event.slot != armed.slot:
                return
            if isinstance(event, Stopped) and not armed.stop_requested:
                return
            armed.live = False
            del self._active_slots[event.slot]

    def next_event(self, raw: Mapping) -> DeviceEvent:
        kind = raw.get('kind')
        step = raw.get('step')
        if type(step) is not int or step <= self._last_step:
            raise ValueError('device steps must be strictly increasing positive integers')
        if kind == 'replay':
            original_step = raw.get('of_step')
            event = self._events.get(original_step) if type(original_step) is int else None
            if event is None or isinstance(event, Exposure):
                raise ValueError('replay requires an earlier non-exposure notification')
            if self._handover and not any(notice.step == original_step for notice in self._mailbox):
                raise ValueError('replay cannot retrieve a recycled notice')
            trace_entry = {'kind': kind, 'step': step, 'of_step': original_step}
        else:
            if kind == 'exposure':
                trigger_id = raw.get('trigger_id')
                armed = self._arms.get(trigger_id) if isinstance(trigger_id, str) else None
                if armed is None or not armed.live:
                    raise ValueError('an actual exposure requires a live device trigger')
                raw = dict(raw, slot=armed.slot, range_name=self.actual_range, recording_id=self.recording_id)
            event = decode_device_event(raw)
            self._observe(event)
            trace_entry = {'kind': kind, **asdict(event)}
        self._last_step = step
        self._events[step] = event
        self._trace.append(trace_entry)
        if self._handover:
            self._mailbox.append(event)
        return event
