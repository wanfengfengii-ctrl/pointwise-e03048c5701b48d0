import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SuccessReceipt:
    receipt_id: str
    sample_id: str
    shift_id: str
    order_id: str


def issue_receipt(sample_id: str, shift_id: str, order_id: str) -> SuccessReceipt:
    return SuccessReceipt(f'receipt:{sample_id}', sample_id, shift_id, order_id)


def save_receipt(connection: sqlite3.Connection, receipt: SuccessReceipt) -> None:
    if not connection.in_transaction:
        raise RuntimeError('receipt requires an archive transaction')
    connection.execute('''
        INSERT INTO receipts (receipt_id, sample_id, shift_id, order_id) VALUES (?, ?, ?, ?)
    ''', (receipt.receipt_id, receipt.sample_id, receipt.shift_id, receipt.order_id))


def receipt_from_row(row: sqlite3.Row) -> SuccessReceipt:
    return SuccessReceipt(row['receipt_id'], row['sample_id'], row['shift_id'], row['order_id'])
