import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from capture.convert import Calibration
from control.range import RangeState
from jobs.shifts import ShiftCatalog, initialize_shift_catalog


class OrderStatus(StrEnum):
    PENDING = 'pending'
    CAPTURING = 'capturing'
    CANCELED = 'canceled'
    SUCCEEDED = 'succeeded'


@dataclass(frozen=True, slots=True)
class Order:
    shift_id: str
    order_id: str
    sequence: int
    requested_range: str
    calibration: Calibration
    status: OrderStatus


def initialize_orders(connection: sqlite3.Connection) -> None:
    initialize_shift_catalog(connection)
    connection.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
            order_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK (sequence > 0),
            requested_range TEXT NOT NULL,
            revision TEXT NOT NULL,
            gain_n TEXT NOT NULL,
            gain_d TEXT NOT NULL,
            offset_n TEXT NOT NULL,
            offset_d TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'capturing', 'canceled', 'succeeded')),
            PRIMARY KEY (shift_id, order_id),
            UNIQUE (shift_id, sequence)
        )
    ''')


def _order_from_row(row: sqlite3.Row) -> Order:
    calibration = Calibration(
        row['requested_range'], row['revision'],
        Fraction(int(row['gain_n']), int(row['gain_d'])),
        Fraction(int(row['offset_n']), int(row['offset_d'])),
    )
    return Order(
        row['shift_id'], row['order_id'], row['sequence'],
        row['requested_range'], calibration, OrderStatus(row['status']),
    )


class OrderBook:
    def __init__(self, connection: sqlite3.Connection, ranges: RangeState | None = None):
        self.connection = connection
        self.ranges = ranges

    def register(self, shift_id: str, order_id: str, requested_range: str,
                 calibration: Calibration) -> Order:
        if not isinstance(order_id, str) or not order_id.strip():
            raise ValueError('order_id must be a nonempty string')
        if self.ranges is None or not self.ranges.supports(requested_range):
            raise ValueError(f'unsupported range: {requested_range!r}')
        if not isinstance(calibration, Calibration) or calibration.range_name != requested_range:
            raise ValueError('calibration must match the requested range')

        self.connection.execute('BEGIN IMMEDIATE')
        with self.connection:
            ShiftCatalog(self.connection).require_shift(shift_id)
            duplicate = self.connection.execute(
                'SELECT 1 FROM orders WHERE shift_id = ? AND order_id = ?',
                (shift_id, order_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError(f'duplicate order: {order_id!r} in {shift_id!r}')
            sequence = self.connection.execute(
                'SELECT COALESCE(MAX(sequence), 0) + 1 FROM orders WHERE shift_id = ?',
                (shift_id,),
            ).fetchone()[0]
            self.connection.execute('''
                INSERT INTO orders (
                    shift_id, order_id, sequence, requested_range, revision,
                    gain_n, gain_d, offset_n, offset_d, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                shift_id, order_id, sequence, requested_range, calibration.revision,
                str(calibration.gain.numerator), str(calibration.gain.denominator),
                str(calibration.offset.numerator), str(calibration.offset.denominator),
                OrderStatus.PENDING,
            ))
        return Order(shift_id, order_id, sequence, requested_range, calibration, OrderStatus.PENDING)

    def get_order(self, shift_id: str, order_id: str) -> Order:
        row = self.connection.execute(
            'SELECT * FROM orders WHERE shift_id = ? AND order_id = ?', (shift_id, order_id)
        ).fetchone()
        if row is None:
            raise KeyError((shift_id, order_id))
        return _order_from_row(row)

    def list_shift_orders(self, shift_id: str, *, status: OrderStatus | None = None) -> tuple[Order, ...]:
        if status is None:
            rows = self.connection.execute(
                'SELECT * FROM orders WHERE shift_id = ? ORDER BY sequence', (shift_id,)
            )
        else:
            rows = self.connection.execute(
                'SELECT * FROM orders WHERE shift_id = ? AND status = ? ORDER BY sequence',
                (shift_id, status),
            )
        return tuple(_order_from_row(row) for row in rows)

    def change_status(self, shift_id: str, order_id: str, expected: tuple[OrderStatus, ...],
                      target: OrderStatus) -> bool:
        allowed = {
            OrderStatus.CAPTURING: (OrderStatus.PENDING,),
            OrderStatus.CANCELED: (OrderStatus.PENDING, OrderStatus.CAPTURING),
            OrderStatus.SUCCEEDED: (OrderStatus.CAPTURING,),
        }
        if not expected or target not in allowed or any(status not in allowed[target] for status in expected):
            raise ValueError('invalid order transition')
        if target == OrderStatus.SUCCEEDED and not self.connection.in_transaction:
            raise RuntimeError('success requires an archive transaction')
        placeholders = ', '.join('?' for _ in expected)
        updated = self.connection.execute(f'''
            UPDATE orders SET status = ? WHERE shift_id = ? AND order_id = ?
              AND status IN ({placeholders})
        ''', (target, shift_id, order_id, *expected))
        return updated.rowcount == 1
