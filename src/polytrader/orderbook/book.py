"""Network-independent book engine. Quantities are replacements, not deltas."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from polytrader.data import DataError
from polytrader.orderbook.models import BookSnapshot, Level


def decimal_value(value, *, price: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise DataError(f"Invalid book number: {value!r}") from exc
    if not number.is_finite() or number < 0 or (price and number > 1):
        raise DataError(f"Invalid {'price' if price else 'size'}: {value!r}")
    return number


def levels(values) -> dict[Decimal, Decimal]:
    if not isinstance(values, list):
        raise DataError("Book levels must be an array")
    result = {}
    for item in values:
        if not isinstance(item, dict):
            raise DataError("Invalid book level")
        price = decimal_value(item.get("price"), price=True)
        size = decimal_value(item.get("size"))
        if price in result:
            raise DataError("Duplicate price in book snapshot")
        result[price] = size
    return {price: size for price, size in result.items() if size}


class OrderBook:
    def __init__(self, token_id: str):
        self.token_id = token_id
        self._bids = {}
        self._asks = {}
        self.snapshot = BookSnapshot(token_id)

    def invalidate(self, reason: str, *, status: str = "stale") -> BookSnapshot:
        self.snapshot = replace(self.snapshot, status=status, reason=reason)
        return self.snapshot

    def _publish(self, bids, asks, updated_at, status):
        if bids and asks and max(bids) > min(asks):
            raise DataError("Crossed order book; a fresh snapshot is required")
        self._bids, self._asks = bids, asks
        self.snapshot = BookSnapshot(
            self.token_id,
            tuple(Level(p, bids[p]) for p in sorted(bids, reverse=True)),
            tuple(Level(p, asks[p]) for p in sorted(asks)),
            updated_at, datetime.now(timezone.utc), status,
        )
        return self.snapshot

    def replace(self, bids: list, asks: list, updated_at: datetime, *, status="live"):
        if (self.snapshot.status == "live" and self.snapshot.updated_at is not None
                and updated_at < self.snapshot.updated_at):
            raise DataError("Out-of-order snapshot; resynchronization required")
        return self._publish(levels(bids), levels(asks), updated_at, status)

    def apply(self, changes: list[dict], updated_at: datetime):
        if self.snapshot.status != "live":
            raise DataError("Cannot update a book before its fresh snapshot")
        if self.snapshot.updated_at is not None and updated_at < self.snapshot.updated_at:
            raise DataError("Out-of-order update; resynchronization required")
        bids, asks = self._bids.copy(), self._asks.copy()
        for change in changes:
            side = change.get("side")
            if side not in {"BUY", "SELL"}:
                raise DataError(f"Unknown book side: {side!r}")
            price = decimal_value(change.get("price"), price=True)
            size = decimal_value(change.get("size"))
            target = bids if side == "BUY" else asks
            if size:
                target[price] = size
            else:
                target.pop(price, None)
        return self._publish(bids, asks, updated_at, "live")
