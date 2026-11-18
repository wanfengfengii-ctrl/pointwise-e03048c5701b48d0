from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RangeChange:
    command_id: int
    target: str


class RangeState:
    def __init__(self, supported: Iterable[str], initial: str):
        names = tuple(supported)
        if not names or any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError('supported ranges must be nonempty strings')
        self.supported = frozenset(names)
        if not self.supports(initial):
            raise ValueError(f'unsupported initial range: {initial!r}')
        self.current = initial
        self.pending: RangeChange | None = None
        self._next_command_id = 1

    def supports(self, range_name: str) -> bool:
        return isinstance(range_name, str) and range_name in self.supported

    def request_change(self, target: str) -> RangeChange:
        if self.pending is not None:
            raise ValueError('a range change is already in flight')
        if not self.supports(target) or self.current == target:
            raise ValueError(f'invalid range change to {target!r}')
        command = RangeChange(self._next_command_id, target)
        self._next_command_id += 1
        self.pending = command
        return command

    def confirm_change(self, command_id: int, range_name: str) -> bool:
        if type(command_id) is not int or self.pending != RangeChange(command_id, range_name):
            return False
        self.current = range_name
        self.pending = None
        return True
