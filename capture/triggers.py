from dataclasses import dataclass

from capture.events import Completed, Exposure, StopAccepted, Stopped


@dataclass(frozen=True, slots=True)
class TerminalFact:
    trigger_id: str
    slot: int
    range_name: str
    reason: str


@dataclass(slots=True)
class TriggerBinding:
    trigger_id: str
    shift_id: str
    order_id: str
    slot: int
    range_name: str
    exposure: Exposure | None = None
    stop_requested: bool = False
    stop_accepted: bool = False
    terminal: TerminalFact | None = None


class TriggerLedger:
    def __init__(self, recording_id: str):
        self.recording_id = recording_id
        self._bindings: dict[str, TriggerBinding] = {}

    def bind(self, trigger_id: str, shift_id: str, order_id: str, slot: int, range_name: str) -> TriggerBinding:
        if trigger_id in self._bindings:
            raise ValueError(f'trigger identity already bound: {trigger_id!r}')
        binding = TriggerBinding(trigger_id, shift_id, order_id, slot, range_name)
        self._bindings[trigger_id] = binding
        return binding

    def get(self, trigger_id: str) -> TriggerBinding | None:
        return self._bindings.get(trigger_id)

    def live_for(self, shift_id: str, order_id: str) -> TriggerBinding | None:
        return next((
            binding for binding in self._bindings.values()
            if binding.shift_id == shift_id and binding.order_id == order_id and binding.terminal is None
        ), None)

    def _live_match(self, trigger_id: str, slot: int) -> TriggerBinding | None:
        binding = self.get(trigger_id)
        if binding is None or binding.terminal is not None or binding.slot != slot:
            return None
        return binding

    def record_exposure(self, event: Exposure) -> bool:
        binding = self._live_match(event.trigger_id, event.slot)
        if binding is None or event.recording_id != self.recording_id or binding.exposure is not None:
            return False
        binding.exposure = event
        return True

    def mark_stop_requested(self, trigger_id: str) -> None:
        binding = self._bindings[trigger_id]
        if binding.terminal is not None:
            raise ValueError('cannot request stop for a terminal trigger')
        binding.stop_requested = True

    def record_stop_accepted(self, event: StopAccepted) -> bool:
        binding = self._live_match(event.trigger_id, event.slot)
        if binding is None or not binding.stop_requested or binding.stop_accepted:
            return False
        binding.stop_accepted = True
        return True

    def close(self, event: Completed | Stopped) -> TerminalFact | None:
        if not isinstance(event, (Completed, Stopped)):
            return None
        binding = self._live_match(event.trigger_id, event.slot)
        if binding is None:
            return None
        if isinstance(event, Stopped) and not binding.stop_requested:
            return None
        reason = 'stopped' if isinstance(event, Stopped) else 'completed'
        binding.terminal = TerminalFact(binding.trigger_id, binding.slot, binding.range_name, reason)
        return binding.terminal
