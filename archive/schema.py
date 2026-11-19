import sqlite3
from pathlib import Path

from control.handover_store import initialize_handover
from jobs.orders import initialize_orders


def open_store(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
    except BaseException:
        connection.close()
        raise
    return connection


def initialize_archive(connection: sqlite3.Connection) -> None:
    connection.execute('BEGIN IMMEDIATE')
    with connection:
        initialize_orders(connection)
        connection.execute('''
            CREATE TABLE IF NOT EXISTS samples (
                sample_id TEXT PRIMARY KEY NOT NULL,
                shift_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                trigger_id TEXT NOT NULL,
                raw_code TEXT NOT NULL,
                actual_range TEXT NOT NULL,
                revision TEXT NOT NULL,
                gain_n TEXT NOT NULL,
                gain_d TEXT NOT NULL,
                offset_n TEXT NOT NULL,
                offset_d TEXT NOT NULL,
                voltage_n TEXT NOT NULL,
                voltage_d TEXT NOT NULL,
                recording_id TEXT NOT NULL,
                exposure_step INTEGER NOT NULL CHECK (exposure_step > 0),
                slot INTEGER NOT NULL CHECK (slot >= 0),
                UNIQUE (shift_id, order_id),
                UNIQUE (sample_id, shift_id, order_id),
                FOREIGN KEY (shift_id, order_id) REFERENCES orders(shift_id, order_id)
            )
        ''')
        connection.execute('''
            CREATE TABLE IF NOT EXISTS receipts (
                receipt_id TEXT PRIMARY KEY NOT NULL,
                sample_id TEXT NOT NULL UNIQUE,
                shift_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                FOREIGN KEY (sample_id, shift_id, order_id) REFERENCES samples(sample_id, shift_id, order_id)
            )
        ''')
        initialize_handover(connection)
