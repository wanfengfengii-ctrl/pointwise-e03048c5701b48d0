from jobs.orders import Order, OrderBook, OrderStatus


def pending_orders(book: OrderBook, shift_id: str) -> tuple[Order, ...]:
    return book.list_shift_orders(shift_id, status=OrderStatus.PENDING)


def next_eligible_order(book: OrderBook, shift_id: str, *,
                        required_range: str | None = None) -> Order | None:
    for order in pending_orders(book, shift_id):
        if required_range is None or order.requested_range == required_range:
            return order
    return None
