from jobs.orders import OrderBook, OrderStatus


def get_order_status(book: OrderBook, shift_id: str, order_id: str) -> OrderStatus:
    return book.get_order(shift_id, order_id).status


def mark_started(book: OrderBook, shift_id: str, order_id: str) -> bool:
    return book.change_status(shift_id, order_id, (OrderStatus.PENDING,), OrderStatus.CAPTURING)


def mark_succeeded(book: OrderBook, shift_id: str, order_id: str) -> bool:
    return book.change_status(shift_id, order_id, (OrderStatus.CAPTURING,), OrderStatus.SUCCEEDED)
