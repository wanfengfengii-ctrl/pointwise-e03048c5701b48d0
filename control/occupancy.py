from capture.triggers import TerminalFact


class Occupancy:
    def __init__(self, capacity: int):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        self.capacity = capacity
        self._positions: dict[str, tuple[int, str]] = {}

    def reserve(self, trigger_id: str, slot: int, range_name: str) -> None:
        if type(slot) is not int or not 0 <= slot < self.capacity:
            raise ValueError(f'invalid slot: {slot!r}')
        if trigger_id in self._positions or any(slot == position[0] for position in self._positions.values()):
            raise ValueError('trigger or slot is already occupied')
        if any(range_name != position[1] for position in self._positions.values()):
            raise ValueError('cannot reserve a different range while occupied')
        self._positions[trigger_id] = (slot, range_name)

    def release(self, fact: TerminalFact) -> bool:
        if self._positions.get(fact.trigger_id) != (fact.slot, fact.range_name):
            return False
        del self._positions[fact.trigger_id]
        return True

    def free_slot(self) -> int | None:
        used = {slot for slot, _ in self._positions.values()}
        return next((slot for slot in range(self.capacity) if slot not in used), None)

    def can_change_range(self) -> bool:
        return not self._positions
