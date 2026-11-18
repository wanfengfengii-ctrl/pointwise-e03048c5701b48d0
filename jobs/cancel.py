from jobs.orders import OrderBook, OrderStatus


def cancel(book: OrderBook, shift_id: str, order_id: str) -> OrderStatus:
    changed = book.change_status(
        shift_id, order_id, (OrderStatus.PENDING, OrderStatus.CAPTURING), OrderStatus.CANCELED
    )
    if not changed:
        order = book.get_order(shift_id, order_id)
        raise ValueError(f'cannot cancel order {order_id!r} in status {order.status}')
    return OrderStatus.CANCELED
