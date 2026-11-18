import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from archive.publish import sample_id_for
from archive.read import list_shift_samples, read_sample
from archive.schema import initialize_archive, open_store
from capture.convert import Calibration
from capture.device import RecordedDevice
from control.dispatch import Session, advance
from control.range import RangeState
from jobs.orders import OrderBook
from jobs.shifts import ShiftCatalog


class Cutover(BaseException):
    """The caller is interrupted before the device accepts confirmation."""


class FixtureDevice(RecordedDevice):
    def __init__(self, recording_id="take-1"):
        super().__init__(recording_id, "low", ("a", "b", "c"))
        self.cut_at = None
        self.omit_position = None

    def read_notices(self, after):
        return tuple(item for item in super().read_notices(after) if item[0] != self.omit_position)

    def confirm_notices(self, through):
        if self.cut_at is not None and through >= self.cut_at:
            raise Cutover("confirmation not delivered")
        return super().confirm_notices(through)


def raises(exception, call, *args):
    try:
        call(*args)
    except exception:
        return
    raise AssertionError(f"expected {exception.__name__}")


def setup(connection):
    initialize_archive(connection)
    ShiftCatalog(connection).ensure_shift("day")
    ranges = RangeState(("low", "high"), "low")
    book = OrderBook(connection, ranges)
    for name, band, revision, gain, offset in (
        ("A", "low", "L1", "1/6", "-1/6"),
        ("B", "low", "L2", "1/5", 0),
        ("C", "high", "H1", "1/4", -1),
    ):
        book.register("day", name, band, Calibration(band, revision, gain, offset))
    return ranges


def send(device, kind, step, **fields):
    return device.next_event(dict(kind=kind, step=step, **fields))


def complete(device, step, name, slot, exposure, code):
    return send(device, "completed", step, trigger_id=name, slot=slot, exposure_step=exposure, raw_code=code)


def by_order(connection, order):
    return read_sample(connection, sample_id_for("day", order))


def commands(device, kind):
    return [item for item in device.trace if item["kind"] == kind]


def check_sample(item, order, trigger, slot, exposure, code, volts, revision, band):
    assert item is not None, order
    sample = item.sample
    assert (sample.order_id, sample.trigger_id, sample.exposure.slot, sample.exposure.step) == (order, trigger, slot, exposure)
    assert (sample.exposure.recording_id, sample.raw_code, str(sample.voltage)) == ("take-1", code, volts)
    assert (sample.calibration.revision, sample.actual_range) == (revision, band)
    assert (item.receipt.shift_id, item.receipt.order_id, item.receipt.sample_id) == ("day", order, sample.sample_id)


def check_a(item):
    check_sample(item, "A", "a", 0, 10, 5, "2/3", "L1", "low")


def check_b(item):
    check_sample(item, "B", "b", 1, 35, 17, "17/5", "L2", "low")


def legacy():
    device = RecordedDevice("take-1", "low", ("a", "b", "c"))
    with closing(open_store(":memory:")) as db:
        session = Session(db, "day", setup(db), device)
        advance(session)
        advance(session, send(device, "exposure", 10, trigger_id="a"))
        check_a(advance(session, complete(device, 20, "a", 0, 10, 5)))
        advance(session, send(device, "exposure", 35, trigger_id="b"))
        assert commands(device, "request_range") == []
        check_b(advance(session, complete(device, 50, "b", 1, 35, 17)))
        switch, = commands(device, "request_range")
        assert switch["target"] == "high"
        print("A=2/3 V, B=17/5 V. V1 requested high after B terminated.")


FAIL_AT_COMMIT = """
CREATE TABLE fixture_parent (id INTEGER PRIMARY KEY);
CREATE TABLE fixture_guard (
    id INTEGER REFERENCES fixture_parent(id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TRIGGER fixture_fail_commit AFTER INSERT ON receipts BEGIN
    INSERT INTO fixture_guard (id) VALUES (1);
END;
"""


def connected(db, device, start_handover):
    session = start_handover(db, "day", setup(db), device)
    advance(session)
    send(device, "exposure", 10, trigger_id="a")
    complete(device, 20, "a", 0, 10, 5)
    advance(session)
    check_a(by_order(db, "A"))
    assert [item["trigger_id"] for item in commands(device, "arm")] == ["a", "b"]
    assert commands(device, "request_range") == []
    return session


def case(database, mode):
    from control.dispatch import resume, start_handover

    device = FixtureDevice()
    with closing(open_store(database)) as db:
        session = connected(db, device, start_handover)
        saved_a = by_order(db, "A")
        if mode != "offline":
            send(device, "exposure", 35, trigger_id="b")
            advance(session)
            assert device.delivery_state()["confirmed"] == 3
            raises(ValueError, device.read_notices, 2)
            assert not any(item.get("step") == 35 and item.get("trigger_id") == "b" for item in device.trace)
            complete(device, 50, "b", 1, 35, 17)
            if mode == "before":
                db.executescript(FAIL_AT_COMMIT)
                raises(sqlite3.IntegrityError, advance, session)
            else:
                device.cut_at = 4
                raises(Cutover, advance, session)
                device.cut_at = None
            assert device.delivery_state()["confirmed"] == 3
            assert commands(device, "request_range") == []

    if mode == "offline":
        send(device, "exposure", 35, trigger_id="b")
        complete(device, 50, "b", 1, 35, 17)
        send(device, "replay", 55, of_step=50)
        notices = device.read_notices(2)
        assert [(position, event.step) for position, event in notices] == [(3, 35), (4, 50), (5, 50)]

    with closing(open_store(database)) as db:
        saved_b = by_order(db, "B")
        if mode == "after":
            check_b(saved_b)
        else:
            assert saved_b is None
        if mode == "before":
            db.execute("DROP TRIGGER fixture_fail_commit")
        if mode == "offline":
            wrong = FixtureDevice("take-2")
            wrong.enable_handover()
            wrong_trace = wrong.trace
            raises(ValueError, resume, db, "day", wrong)
            assert wrong.trace == wrong_trace
            device.omit_position = 4
            raises(ValueError, resume, db, "day", device)
            device.omit_position = None
            assert commands(device, "request_range") == []
            assert device.delivery_state()["confirmed"] == 2
            assert list_shift_samples(db, "day") == (saved_a,)
        resumed = resume(db, "day", device)
        received_b = by_order(db, "B")
        check_b(received_b)
        assert saved_b is None or received_b == saved_b
        assert list_shift_samples(db, "day") == (saved_a, received_b)
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 2
        state = device.delivery_state()
        assert state["confirmed"] == state["tail"]
        assert [item["trigger_id"] for item in commands(device, "arm")] == ["a", "b"]
        assert commands(device, "request_range") == []
        if mode == "offline":
            advance(resumed)
            switch, = commands(device, "request_range")
            assert switch["target"] == "high"
            assert device.delivery_state()["pending_range"] == (switch["command_id"], "high")

    if mode == "offline":
        send(device, "range_applied", 70, command_id=switch["command_id"], range_name="high")
    with closing(open_store(database)) as db:
        resumed = resume(db, "day", device)
        assert list_shift_samples(db, "day") == (saved_a, received_b)
        if mode == "offline":
            advance(resumed)
            assert commands(device, "request_range") == [switch]
            assert [item["trigger_id"] for item in commands(device, "arm")] == ["a", "b", "c"]
            send(device, "exposure", 80, trigger_id="c")
            complete(device, 90, "c", 0, 80, 4)
            advance(resumed)
            check_sample(by_order(db, "C"), "C", "c", 0, 80, 4, "0", "H1", "high")
        return list_shift_samples(db, "day"), device.trace


def stopped(database):
    from control.dispatch import resume, start_handover

    device = FixtureDevice()
    with closing(open_store(database)) as db:
        session = connected(db, device, start_handover)
        session.cancel("B", stop=True)
    send(device, "stop_accepted", 35, trigger_id="b", slot=1)
    with closing(open_store(database)) as db:
        resumed = resume(db, "day", device)
        advance(resumed)
        assert commands(device, "request_range") == []
        assert len(commands(device, "request_stop")) == 1
        send(device, "exposure", 40, trigger_id="b")
        send(device, "stopped", 50, trigger_id="b", slot=1)
        advance(resumed)
        assert [item["target"] for item in commands(device, "request_range")] == ["high"]
        assert by_order(db, "B") is None


def main(mode):
    if mode == "legacy":
        legacy()
        return 0
    if mode == "verify":
        from check_project import main as verify_v1

        if verify_v1():
            return 1
    with TemporaryDirectory(prefix="voltage-handover-") as directory:
        root = Path(directory)
        samples, trace = case(root / "offline.sqlite3", "offline")
        if mode == "verify":
            case(root / "before.sqlite3", "before")
            case(root / "after.sqlite3", "after")
            stopped(root / "stop.sqlite3")
        for record in trace:
            print(record)
        for item in samples:
            sample = item.sample
            print(sample.order_id, sample.raw_code, sample.voltage, sample.exposure, item.receipt.receipt_id)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("legacy", "verify", "demo"):
        raise SystemExit("usage: python3 -B handover_case.py legacy|verify|demo")
    raise SystemExit(main(sys.argv[1]))
