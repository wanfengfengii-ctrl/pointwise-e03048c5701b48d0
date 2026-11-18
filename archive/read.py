import sqlite3

from archive.receipts import receipt_from_row
from archive.records import ArchivedSample, decode_sample


_READ_SAMPLES = '''
    SELECT s.*, o.sequence, r.receipt_id
    FROM samples AS s
    JOIN orders AS o ON o.shift_id = s.shift_id AND o.order_id = s.order_id
    JOIN receipts AS r ON r.sample_id = s.sample_id
'''


def _archived_sample(row: sqlite3.Row) -> ArchivedSample:
    return ArchivedSample(decode_sample(row), receipt_from_row(row))


def read_sample(connection: sqlite3.Connection, sample_id: str) -> ArchivedSample | None:
    row = connection.execute(_READ_SAMPLES + ' WHERE s.sample_id = ?', (sample_id,)).fetchone()
    return _archived_sample(row) if row is not None else None


def list_shift_samples(connection: sqlite3.Connection, shift_id: str) -> tuple[ArchivedSample, ...]:
    rows = connection.execute(_READ_SAMPLES + ' WHERE s.shift_id = ? ORDER BY o.sequence', (shift_id,))
    return tuple(_archived_sample(row) for row in rows)
