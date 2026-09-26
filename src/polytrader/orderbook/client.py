"""Public Polymarket transport; WebSocket dependency is loaded only on use."""

import asyncio
import json
from datetime import datetime, timezone

from polytrader.data import DataError, PolymarketClient

BOOK_URL = "https://clob.polymarket.com/book"
STREAM_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
HEARTBEAT_INTERVAL = 10.0
HEARTBEAT_TIMEOUT = 30.0


def timestamp(value) -> datetime:
    try:
        # CLOB book and market-channel timestamps are Unix milliseconds.
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise DataError(f"Invalid orderbook timestamp: {value!r}") from exc


def messages(raw) -> list[dict]:
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise DataError("Invalid market stream JSON") from exc
    payload = payload if isinstance(payload, list) else [payload]
    if any(not isinstance(item, dict) for item in payload):
        raise DataError("Expected market stream objects")
    return payload


class OrderBookClient(PolymarketClient):
    def get_book(self, token_id: str) -> dict:
        return self.get_json(BOOK_URL, token_id=token_id)

    async def stream(self, token_ids: list[str]):
        try:
            from websockets.asyncio.client import connect
            from websockets.exceptions import WebSocketException
        except ImportError as exc:
            raise ImportError('Live books require: pip install -e ".[live]"') from exc
        try:
            async with connect(STREAM_URL, open_timeout=self.timeout, close_timeout=5,
                               ping_interval=None, max_size=8 * 1024 * 1024) as socket:
                await socket.send(json.dumps({"type": "market", "assets_ids": token_ids,
                                              "custom_feature_enabled": True}))
                loop = asyncio.get_running_loop()
                next_ping = loop.time() + HEARTBEAT_INTERVAL
                last_pong = loop.time()
                while True:
                    now = loop.time()
                    if now - last_pong > HEARTBEAT_TIMEOUT:
                        raise DataError("Market stream heartbeat timed out")
                    if now >= next_ping:
                        await socket.send("PING")
                        next_ping = now + HEARTBEAT_INTERVAL
                        yield None  # Lets the service check snapshot deadlines.
                    try:
                        raw = await asyncio.wait_for(socket.recv(), max(.001, next_ping - loop.time()))
                    except TimeoutError:
                        continue
                    if raw in ("PONG", b"PONG"):
                        last_pong = loop.time()
                        yield None
                    else:
                        for item in messages(raw):
                            yield item
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise DataError(f"Market stream disconnected: {exc}") from exc
