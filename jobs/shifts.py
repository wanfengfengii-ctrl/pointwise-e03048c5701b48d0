import sqlite3


def initialize_shift_catalog(connection: sqlite3.Connection) -> None:
    connection.execute('''
        CREATE TABLE IF NOT EXISTS shifts (
            shift_id TEXT PRIMARY KEY NOT NULL
        )
    ''')


class ShiftCatalog:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def ensure_shift(self, shift_id: str) -> str:
        if not isinstance(shift_id, str) or not shift_id.strip():
            raise ValueError('shift_id must be a nonempty string')
        self.connection.execute(
            'INSERT INTO shifts (shift_id) VALUES (?) ON CONFLICT(shift_id) DO NOTHING',
            (shift_id,),
        )
        return shift_id

    def require_shift(self, shift_id: str) -> str:
        row = self.connection.execute(
            'SELECT shift_id FROM shifts WHERE shift_id = ?', (shift_id,)
        ).fetchone()
        if row is None:
            raise KeyError(shift_id)
        return row['shift_id']
