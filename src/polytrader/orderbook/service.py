"""Maintain many token books on one feed, independently of consumer speed."""

import asyncio
from collections import OrderedDict
from contextlib import aclosing, suppress
import math

from polytrader.data import DataError
from polytrader.orderbook.book import OrderBook
from polytrader.orderbook.client import OrderBookClient, timestamp
from polytrader.orderbook.models import BookUpdate, OrderBooks


class OrderBookService:
    """Async context manager. `collection` always contains the latest snapshots.

    `updates()` supports one consumer, with at most one pending notification per
    token. It is a current-state feed, not a lossless historical event recorder.
    Restart with a newly resolved collection to change the selected markets.
    """

    def __init__(self, collection: OrderBooks, *, client=None, max_retries=5,
                 retry_delay=1.0, snapshot_timeout=30.0, allow_missing_snapshots=False,
                 on_event=None):
        if not collection.books:
            raise ValueError("At least one outcome token is required")
        if max_retries is not None and (not isinstance(max_retries, int) or max_retries < 0):
            raise ValueError("max_retries must be a nonnegative integer or None")
        if not math.isfinite(retry_delay) or retry_delay < 0:
            raise ValueError("retry_delay must be finite and nonnegative")
        if not math.isfinite(snapshot_timeout) or snapshot_timeout <= 0:
            raise ValueError("snapshot_timeout must be finite and positive")
        self.collection = collection
        self.client = client if client is not None else OrderBookClient()
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.snapshot_timeout = snapshot_timeout
        self.allow_missing_snapshots = allow_missing_snapshots
        # Optional synchronous observer: called after each complete wire message
        # is applied, before notifications can coalesce. None denotes a heartbeat
        # or invalidation; inspect healthy/continuity. DataError requests resync;
        # other errors follow the service's existing exception handling.
        self.on_event = on_event
        self._engines = {token: OrderBook(token) for token in collection.books}
        self._pending = OrderedDict()
        self._wake = asyncio.Event()
        self._task = None
        self._error = None
        self._iterating = False
        self.continuity = 0
        self._connected = False

    @property
    def healthy(self):
        """Transport receiving messages/heartbeats; check each book's status as well.

        Quote timestamps are deliberately not a freshness test. ``continuity``
        increases whenever the feed is invalidated, even if notifications coalesce.
        """
        return (self._task is not None and not self._task.done()
                and self._error is None and self._connected)

    def _publish(self, token):
        snapshot = self._engines[token].snapshot
        self.collection.books[token] = snapshot
        self._pending[token] = BookUpdate(self.collection.markets[token], snapshot)
        self._wake.set()

    def _invalidate(self, reason):
        self.continuity += 1
        self._connected = False
        for token, book in self._engines.items():
            if book.snapshot.status != "unavailable":
                book.invalidate(reason)
                self._publish(token)
        if self.on_event is not None:
            self.on_event(None, self)

    async def __aenter__(self):
        if self._task is not None:
            raise RuntimeError("OrderBookService cannot be started twice")
        for token in self._engines:
            self._publish(token)
        self._task = asyncio.create_task(self._run(), name="polytrader-orderbooks")
        return self

    async def __aexit__(self, *exc):
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._invalidate("Orderbook service stopped")
        self._wake.set()

    async def updates(self):
        if self._task is None:
            raise RuntimeError("Start the service with 'async with' first")
        if self._iterating:
            raise RuntimeError("Only one updates consumer is supported")
        self._iterating = True
        try:
            while True:
                if self._pending:
                    _, update = self._pending.popitem(last=False)
                    yield update
                elif self._task.done():
                    if self._error is not None:
                        raise self._error
                    return
                else:
                    self._wake.clear()
                    await self._wake.wait()
        finally:
            self._iterating = False

    def _process(self, message, ready):
        kind = message.get("event_type")
        if kind == "book":
            token = message.get("asset_id")
            if token not in self._engines:
                return
            if self._engines[token].snapshot.status == "unavailable":
                return
            self._engines[token].replace(message.get("bids"), message.get("asks"),
                                         timestamp(message.get("timestamp")))
            ready.add(token)
            self._publish(token)
        elif kind == "price_change":
            changes = message.get("price_changes")
            if not isinstance(changes, list) or any(not isinstance(c, dict) for c in changes):
                raise DataError("Invalid price_changes array")
            grouped = {}
            for change in changes:
                token = change.get("asset_id")
                if token in ready and self._engines[token].snapshot.status == "live":
                    grouped.setdefault(token, []).append(change)
            if grouped:
                updated = timestamp(message.get("timestamp"))
                for token, changes in grouped.items():
                    self._engines[token].apply(changes, updated)
                    self._publish(token)
        elif kind == "market_resolved":
            tokens = message.get("assets_ids", [])
            if not isinstance(tokens, list):
                raise DataError("Invalid resolved market token list")
            for token in tokens:
                if token in self._engines:
                    self._engines[token].invalidate("Market resolved", status="unavailable")
                    ready.add(token)
                    self._publish(token)
        # Trades and best_bid_ask aren't depth updates; never infer quantities from them.

    async def _run(self):
        failures = 0
        loop = asyncio.get_running_loop()
        try:
            while True:
                tokens = [t for t, b in self._engines.items() if b.snapshot.status != "unavailable"]
                if not tokens:
                    return
                ready = set()
                started = loop.time()
                try:
                    async with aclosing(self.client.stream(tokens)) as stream:
                        while True:
                            missing = set(tokens) - ready
                            remaining = self.snapshot_timeout - (loop.time() - started)
                            if missing and remaining <= 0:
                                if not self.allow_missing_snapshots:
                                    raise DataError(f"Timed out waiting for snapshots: {sorted(missing)}")
                                # Some configured markets may no longer have a book. Keep
                                # unrelated markets live; a later full snapshot recovers these.
                                for token in missing:
                                    self._engines[token].invalidate("Initial snapshot unavailable")
                                    self._publish(token)
                                ready.update(missing)
                                missing = set()
                            try:
                                message = await asyncio.wait_for(
                                    anext(stream), remaining if missing and not self.allow_missing_snapshots else None)
                            except TimeoutError as exc:
                                raise DataError(f"Timed out waiting for snapshots: {sorted(missing)}") from exc
                            except StopAsyncIteration as exc:
                                raise DataError("Market stream closed") from exc
                            self._connected = True
                            if message is not None:
                                if not isinstance(message, dict):
                                    raise DataError("Expected a market stream object")
                                self._process(message, ready)
                            if self.on_event is not None:
                                self.on_event(message, self)
                            if all(b.snapshot.status == "unavailable" for b in self._engines.values()):
                                return
                            if not (set(tokens) - ready) and loop.time() - started >= 60:
                                failures = 0
                except (DataError, OSError) as exc:
                    self._invalidate(str(exc))
                    if self.max_retries is not None and failures >= self.max_retries:
                        raise DataError(f"Orderbook stream retries exhausted: {exc}") from exc
                    delay = min(30.0, self.retry_delay * 2 ** min(failures, 10))
                    failures += 1
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = exc
            try:
                self._invalidate(str(exc))
            except Exception as observer_error:
                # An observer/persistence failure must not hide the terminal
                # error and make consumers mistake this for normal completion.
                self._error = observer_error
        finally:
            self._wake.set()
