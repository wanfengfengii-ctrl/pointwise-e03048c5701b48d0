import json

from archive.receipts import issue_receipt, save_receipt
from archive.records import ArchivedSample, SampleRecord, encode_sample
from capture.accept import SampleCandidate
from capture.convert import convert_voltage
from jobs.orders import OrderBook, OrderStatus
from jobs.status import mark_succeeded


def sample_id_for(shift_id: str, order_id: str) -> str:
    return 'sample:' + json.dumps([shift_id, order_id], ensure_ascii=True, separators=(',', ':'))


def publish_sample(book: OrderBook, candidate: SampleCandidate) -> ArchivedSample | None:
    connection = book.connection
    order = candidate.order
    connection.execute('BEGIN IMMEDIATE')
    with connection:
        current = book.get_order(order.shift_id, order.order_id)
        if current.status != OrderStatus.CAPTURING:
            return None
        if current != order:
            raise ValueError('candidate does not match the current frozen order')
        exposure = candidate.exposure
        checked_voltage = convert_voltage(candidate.raw_code, current.calibration, exposure.range_name)
        if checked_voltage != candidate.voltage:
            raise ValueError('candidate voltage does not match the frozen calibration')
        sample_id = sample_id_for(current.shift_id, current.order_id)
        sample = SampleRecord(
            sample_id, current.shift_id, current.order_id, current.sequence,
            candidate.raw_code, current.calibration, checked_voltage, exposure,
        )
        receipt = issue_receipt(sample_id, current.shift_id, current.order_id)
        connection.execute('''
            INSERT INTO samples (
                sample_id, shift_id, order_id, trigger_id, raw_code, actual_range, revision,
                gain_n, gain_d, offset_n, offset_d, voltage_n, voltage_d, recording_id, exposure_step, slot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', encode_sample(sample))
        save_receipt(connection, receipt)
        if not mark_succeeded(book, current.shift_id, current.order_id):
            raise RuntimeError('order lost publication eligibility during the transaction')
    return ArchivedSample(sample, receipt)
