"""Persistent handover evidence for re-opening a duty session.

A bench keeps running while the duty session may close its working store. The
tables below hold the minimum evidence required to reconstruct the in-memory
range state, trigger ledger and occupancy of the previous session, plus the
device delivery position that has durably been consumed. Every checkpoint is
written inside the same SQLite transaction as the business publication it
belongs to; only after the transaction commits may the device be asked to
recycle the corresponding notices.
"""
import json
import sqlite3

from capture.events import Exposure


def initialize_handover(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE IF NOT EXISTS handover_states (
            shift_id TEXT PRIMARY KEY NOT NULL REFERENCES shifts(shift_id),
            recording_id TEXT NOT NULL,
            ranges_text TEXT NOT NULL,
            initial_range TEXT NOT NULL,
            next_command_id INTEGER NOT NULL CHECK (next_command_id > 0),
            current_range TEXT NOT NULL,
            pending_command_id INTEGER,
            pending_target TEXT,
            confirmed_position INTEGER NOT NULL CHECK (confirmed_position >= 0),
            settled_position INTEGER NOT NULL CHECK (settled_position >= 0),
            next_name_index INTEGER NOT NULL CHECK (next_name_index >= 0),
            CHECK (settled_position >= confirmed_position)
        )
    ''')
    connection.execute('''
        CREATE TABLE IF NOT EXISTS handover_bindings (
            shift_id TEXT NOT NULL,
            trigger_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            slot INTEGER NOT NULL CHECK (slot >= 0),
            range_name TEXT NOT NULL,
            exposure_step INTEGER,
            exposure_slot INTEGER,
            exposure_recording TEXT,
            stop_requested INTEGER NOT NULL CHECK (stop_requested IN (0, 1)),
            stop_accepted INTEGER NOT NULL CHECK (stop_accepted IN (0, 1)),
            terminal_reason TEXT CHECK (terminal_reason IS NULL OR terminal_reason IN ('completed', 'stopped')),
            PRIMARY KEY (shift_id, trigger_id),
            FOREIGN KEY (shift_id, order_id) REFERENCES orders(shift_id, order_id)
        )
    ''')


def begin_handover(connection: sqlite3.Connection, shift_id: str, recording_id: str,
                   ranges) -> None:
    try:
        connection.execute('''
            INSERT INTO handover_states (
                shift_id, recording_id, ranges_text, initial_range, next_command_id, current_range,
                pending_command_id, pending_target, confirmed_position, settled_position, next_name_index
            ) VALUES (?, ?, ?, ?, 1, ?, NULL, NULL, 0, 0, 0)
        ''', (
            shift_id, recording_id, json.dumps(sorted(ranges.supported)),
            ranges.current, ranges.current,
        ))
    except sqlite3.IntegrityError as error:
        raise ValueError(f'handover already initialized for shift {shift_id!r}') from error


def load_handover(connection: sqlite3.Connection, shift_id: str) -> sqlite3.Row | None:
    return connection.execute(
        'SELECT * FROM handover_states WHERE shift_id = ?', (shift_id,)
    ).fetchone()


def load_bindings(connection: sqlite3.Connection, shift_id: str) -> tuple[sqlite3.Row, ...]:
    rows = connection.execute(
        'SELECT * FROM handover_bindings WHERE shift_id = ? ORDER BY rowid', (shift_id,)
    )
    return tuple(rows)


def _store_range(connection: sqlite3.Connection, shift_id: str, ranges, next_name_index: int,
                 confirmed_position: int, settled_position: int) -> None:
    pending = ranges.pending
    connection.execute('''
        UPDATE handover_states
           SET next_command_id = ?, current_range = ?, pending_command_id = ?, pending_target = ?,
               confirmed_position = ?, settled_position = ?, next_name_index = ?
         WHERE shift_id = ?
    ''', (
        ranges._next_command_id, ranges.current,
        pending.command_id if pending is not None else None,
        pending.target if pending is not None else None,
        confirmed_position, settled_position, next_name_index, shift_id,
    ))


def _store_binding(connection: sqlite3.Connection, shift_id: str, binding) -> None:
    exposure: Exposure | None = binding.exposure
    connection.execute('''
        INSERT INTO handover_bindings (
            shift_id, trigger_id, order_id, slot, range_name, exposure_step, exposure_slot,
            exposure_recording, stop_requested, stop_accepted, terminal_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(shift_id, trigger_id) DO UPDATE SET
            order_id = excluded.order_id,
            slot = excluded.slot,
            range_name = excluded.range_name,
            exposure_step = excluded.exposure_step,
            exposure_slot = excluded.exposure_slot,
            exposure_recording = excluded.exposure_recording,
            stop_requested = excluded.stop_requested,
            stop_accepted = excluded.stop_accepted,
            terminal_reason = excluded.terminal_reason
    ''', (
        shift_id, binding.trigger_id, binding.order_id, binding.slot, binding.range_name,
        exposure.step if exposure is not None else None,
        exposure.slot if exposure is not None else None,
        exposure.recording_id if exposure is not None else None,
        1 if binding.stop_requested else 0,
        1 if binding.stop_accepted else 0,
        binding.terminal.reason if binding.terminal is not None else None,
    ))


def checkpoint(connection: sqlite3.Connection, shift_id: str, session,
               confirmed_position: int, settled_position: int | None = None) -> None:
    """Persist range, trigger and delivery evidence inside the open transaction."""
    if not connection.in_transaction:
        raise RuntimeError('handover checkpoint requires an open transaction')
    if settled_position is None:
        settled_position = confirmed_position
    _store_range(
        connection, shift_id, session.ranges, session.device._next_name,
        confirmed_position, settled_position,
    )
    for binding in session.ledger._bindings.values():
        _store_binding(connection, shift_id, binding)


def mark_confirmed(connection: sqlite3.Connection, shift_id: str, confirmed_position: int) -> None:
    """Advance the durable confirmation marker after the bench recycled notices."""
    if not connection.in_transaction:
        raise RuntimeError('handover confirmation requires an open transaction')
    updated = connection.execute('''
        UPDATE handover_states
           SET confirmed_position = ?
         WHERE shift_id = ? AND settled_position >= ?
    ''', (confirmed_position, shift_id, confirmed_position))
    if updated.rowcount != 1:
        raise ValueError('cannot confirm a position without settled evidence')
