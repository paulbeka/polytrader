"""Normalize the public market-channel trade tape without inferring depth trades."""

from datetime import datetime, timezone

from polytrader.data import DataError
from polytrader.orderbook.book import decimal_value
from polytrader.orderbook.client import timestamp


def parse_trade(message, leader_tokens, received_at, max_delay):
    if not message or message.get("event_type") != "last_trade_price":
        return None
    token = message.get("asset_id")
    if token not in leader_tokens:
        return None
    side = message.get("side")
    if side not in {"BUY", "SELL"}:
        raise DataError("Trade missing BUY/SELL side")
    size = decimal_value(message.get("size"))
    price = decimal_value(message.get("price"), price=True)
    if not size:
        raise DataError("Trade size must be positive")
    source = timestamp(message.get("timestamp"))
    delay = (received_at - source).total_seconds()
    if delay > max_delay or delay < -max_delay:
        raise DataError("Trade timestamp outside feed delay tolerance")
    # Only canonical leader YES tokens are aggregated. Do not deduplicate by
    # transaction hash: one transaction can contain multiple legitimate fills.
    return {"token": token, "price": price, "size": size, "side": side,
            "signed_size": size if side == "BUY" else -size,
            "source_time": source, "received_time": received_at,
            "transaction_hash": message.get("transaction_hash")}


def utc_now():
    return datetime.now(timezone.utc)
