import asyncio
from contextlib import aclosing, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
import io
import importlib.util
import json
import unittest
from unittest.mock import Mock, patch

from polytrader.cli import main
from polytrader.data import DataError
from polytrader.orderbook import (
    OrderBook, OrderBookClient, OrderBookService, fetch_orderbooks, resolve_books,
    watch_orderbooks,
)
from polytrader.orderbook.client import messages, timestamp


STAMP = "1788220800000"


def snapshot(token="yes-nov", stamp=STAMP, size="100"):
    return {"event_type": "book", "asset_id": token, "timestamp": stamp,
            "bids": [{"price": "0.4", "size": size}, {"price": "0.3", "size": "20"}],
            "asks": [{"price": "0.6", "size": "30"}, {"price": "0.5", "size": "50"}]}


def change(token="yes-nov", price="0.4", size="7", side="BUY", stamp=STAMP):
    return {"event_type": "price_change", "timestamp": stamp,
            "price_changes": [{"asset_id": token, "price": price, "size": size, "side": side}]}


def event():
    return {"id": "event", "slug": "ceasefire", "title": "Ceasefire deadlines", "markets": [
        {"id": str(i), "slug": month, "question": f"Ceasefire by {month}?",
         "description": "By deadline, not during month", "endDate": "not-a-contract-deadline",
         "outcomes": '["No", "Yes"]', "groupItemTitle": month,
         "clobTokenIds": json.dumps([f"no-{month}", f"yes-{month}"])}
        for i, month in enumerate(["nov", "dec", "jan"])
    ]}


class BookTests(unittest.TestCase):
    def setUp(self):
        self.book = OrderBook("yes-nov")
        raw = snapshot()
        self.book.replace(raw["bids"], raw["asks"], timestamp(STAMP))

    def test_sorted_depth_decimal_spread_and_immutable_observations(self):
        first = self.book.snapshot
        self.assertEqual([x.price for x in first.asks], [Decimal(".5"), Decimal(".6")])
        self.assertEqual(first.spread, Decimal(".1"))
        self.assertEqual(first.midpoint, Decimal(".45"))
        self.book.apply(change()["price_changes"], timestamp(STAMP))
        self.assertEqual(self.book.snapshot.best_bid.size, Decimal(7))  # Not 107.
        self.assertEqual(first.best_bid.size, Decimal(100))
        self.assertEqual(first.to_dict()["spread"], "0.1")

    def test_zero_removes_level_and_empty_side_has_no_spread(self):
        self.book.apply(change(size="0")["price_changes"], timestamp(STAMP))
        self.assertEqual(self.book.snapshot.best_bid.price, Decimal(".3"))
        self.book.apply(change(price=".3", size="0")["price_changes"], timestamp(STAMP))
        self.assertIsNone(self.book.snapshot.best_bid)
        self.assertIsNone(self.book.snapshot.spread)
        self.assertIsNone(self.book.snapshot.midpoint)

    def test_snapshot_replaces_old_levels_and_allows_empty_book(self):
        self.book.replace([], [], timestamp(STAMP))
        self.assertEqual(self.book.snapshot.bids, ())
        self.assertEqual(self.book.snapshot.asks, ())

    def test_bad_updates_are_atomic(self):
        original = self.book.snapshot
        for kwargs in ({"size": "NaN"}, {"size": "-1"}, {"price": "1.1"},
                       {"side": "OTHER"}, {"price": ".7"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(DataError):
                self.book.apply(change()["price_changes"] + change(**kwargs)["price_changes"],
                                timestamp(STAMP))
            self.assertIs(self.book.snapshot, original)

    def test_old_updates_and_updates_without_snapshot_are_rejected(self):
        with self.assertRaises(DataError):
            self.book.apply(change()["price_changes"], timestamp(int(STAMP) - 1))
        self.book.invalidate("Disconnected")
        with self.assertRaises(DataError):
            self.book.apply(change()["price_changes"], timestamp(STAMP))
        self.book.replace([], [], timestamp(STAMP))
        self.assertEqual(self.book.snapshot.status, "live")

    def test_bad_snapshot_does_not_replace_state(self):
        before = self.book.snapshot
        for bids in (None, [None], [{"price": "NaN", "size": "1"}],
                     [{"price": ".4", "size": "1"}] * 2):
            with self.subTest(bids=bids), self.assertRaises(DataError):
                self.book.replace(bids, [], timestamp(STAMP))
            self.assertIs(before, self.book.snapshot)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.get_event.return_value = event()
        self.client.get_book.side_effect = snapshot

    def test_all_dates_and_outcomes_preserve_mapping_and_rules(self):
        books = fetch_orderbooks("ceasefire", client=self.client)
        self.assertEqual(len(books.books), 6)
        self.assertEqual(books.markets["yes-nov"].outcome, "Yes")
        self.assertEqual(books.markets["yes-nov"].raw["description"], "By deadline, not during month")
        self.assertIsNone(books.markets["yes-nov"].deadline)
        self.assertEqual(books.books["yes-jan"].status, "snapshot")
        json.dumps(books.to_dict(), allow_nan=False)
        self.assertEqual(len(books.summary()), 6)

    def test_selection_order_deadlines_and_nonbinary_labels(self):
        books = fetch_orderbooks("ceasefire", markets=["jan", "nov"], outcomes="yes",
                                deadlines={"jan": "2027-01-31"}, client=self.client)
        self.assertEqual(list(books.books), ["yes-jan", "yes-nov"])
        self.assertEqual(books.markets["yes-jan"].deadline, "2027-01-31")
        raw = event()
        raw["markets"][0]["outcomes"] = '["Team A", "Team B"]'
        self.client.get_event.return_value = raw
        books = resolve_books("ceasefire", markets="nov", outcomes="team b", client=self.client)
        self.assertEqual(books.markets["yes-nov"].outcome, "Team B")

    def test_closed_disabled_and_missing_tokens_have_reasons(self):
        raw = event()
        raw["markets"][0]["closed"] = True
        raw["markets"][1]["enableOrderBook"] = False
        raw["markets"][2]["clobTokenIds"] = "[]"
        self.client.get_event.return_value = raw
        books = fetch_orderbooks("ceasefire", client=self.client)
        self.assertEqual(len(books.excluded), 3)
        self.assertFalse(books.books)
        self.client.get_book.assert_not_called()

    def test_partial_failure_and_wrong_token_are_explicit(self):
        self.client.get_book.side_effect = [snapshot("yes-nov"), DataError("HTTP 404")]
        books = fetch_orderbooks(token_ids=["yes-nov", "no-nov"], client=self.client)
        self.assertEqual(books.books["yes-nov"].status, "snapshot")
        self.assertEqual(books.books["no-nov"].reason, "HTTP 404")
        self.client.get_book.side_effect = lambda token: snapshot("wrong")
        books = fetch_orderbooks(token_ids="yes-nov", client=self.client)
        self.assertEqual(books.books["yes-nov"].status, "unavailable")
        self.client.get_event.assert_not_called()

    def test_invalid_inputs_do_not_call_network(self):
        for kwargs in ({}, {"event": "e", "token_ids": ["x"]},
                       {"token_ids": "x", "outcomes": "Yes"}, {"token_ids": []},
                       {"token_ids": [""]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                fetch_orderbooks(**kwargs, client=self.client)
        self.client.get_event.assert_not_called()
        self.client.get_book.assert_not_called()

    def test_unknown_market_or_outcome_is_not_silently_ignored(self):
        for kwargs in ({"markets": "wrong"}, {"outcomes": "wrong"},
                       {"deadlines": {"wrong": "2027-01-01"}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                resolve_books("ceasefire", **kwargs, client=self.client)


class FakeFeed:
    def __init__(self, sessions):
        self.sessions = sessions
        self.calls = []
        self.closed = 0

    async def stream(self, tokens):
        self.calls.append(tokens)
        index = len(self.calls) - 1
        try:
            for message in self.sessions[index]:
                await asyncio.sleep(0)
                if isinstance(message, Exception):
                    raise message
                yield message
            await asyncio.Event().wait()
        finally:
            self.closed += 1


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def next_matching(self, iterator, predicate):
        async with asyncio.timeout(2):
            while True:
                update = await anext(iterator)
                if predicate(update):
                    return update

    async def test_reconnect_requires_new_snapshot_and_old_views_remain_unchanged(self):
        feed = FakeFeed([
            [snapshot(), DataError("offline")],
            [change(size="999"), snapshot(size="40"), change(size="9")],
        ])
        collection = resolve_books(token_ids="yes-nov")
        service = OrderBookService(collection, client=feed, retry_delay=.01)
        async with service, aclosing(service.updates()) as updates:
            first = await self.next_matching(updates, lambda u: u.book.status == "live")
            stale = await self.next_matching(updates, lambda u: u.book.status == "stale")
            last = await self.next_matching(updates, lambda u: u.book.status == "live"
                                            and u.book.best_bid.size == 9)
            self.assertEqual(first.book.best_bid.size, 100)
            self.assertEqual(stale.book.reason, "offline")
            self.assertEqual(last.book.best_bid.size, 9)
            self.assertEqual(feed.calls, [["yes-nov"], ["yes-nov"]])
        self.assertEqual(feed.closed, 2)
        self.assertEqual(collection.books["yes-nov"].status, "stale")

    async def test_multiple_tokens_batched_updates_and_slow_consumer(self):
        batch = change()
        batch["price_changes"] += change("yes-dec", size="12")["price_changes"]
        feed = FakeFeed([[snapshot(), snapshot("yes-dec"), batch]])
        collection = resolve_books(token_ids=["yes-nov", "yes-dec"])
        async with OrderBookService(collection, client=feed) as service:
            async with asyncio.timeout(2):
                while collection.books["yes-dec"].best_bid is None or collection.books["yes-dec"].best_bid.size != 12:
                    await asyncio.sleep(.001)
            self.assertEqual(collection.books["yes-nov"].best_bid.size, 7)
            self.assertLessEqual(len(service._pending), 2)
            async with aclosing(service.updates()) as updates:
                first, second = await anext(updates), await anext(updates)
                self.assertEqual({first.book.token_id, second.book.token_id}, {"yes-nov", "yes-dec"})
                self.assertEqual(first.book.status, "live")

    async def test_missing_snapshot_timeout_and_retry_exhaustion(self):
        feed = FakeFeed([[]])
        collection = resolve_books(token_ids="yes-nov")
        async with OrderBookService(collection, client=feed, snapshot_timeout=.01, max_retries=0) as service:
            async with aclosing(service.updates()) as updates:
                with self.assertRaisesRegex(DataError, "retries exhausted"):
                    async for _ in updates:
                        pass
            self.assertEqual(collection.books["yes-nov"].status, "stale")
        self.assertEqual(feed.closed, 1)

    async def test_old_message_causes_resync_and_resolved_market_is_unavailable(self):
        feed = FakeFeed([[snapshot(), change(stamp=str(int(STAMP) - 1))],
                         [snapshot(), {"event_type": "market_resolved", "assets_ids": ["yes-nov"]}]])
        collection = resolve_books(token_ids="yes-nov")
        async with OrderBookService(collection, client=feed, retry_delay=0) as service:
            async with aclosing(service.updates()) as updates:
                async for _ in updates:
                    pass
            self.assertEqual(collection.books["yes-nov"].status, "unavailable")
            self.assertEqual(len(feed.calls), 2)

    async def test_watch_generator_close_stops_background_feed(self):
        feed = FakeFeed([[snapshot()]])
        async with aclosing(watch_orderbooks(token_ids="yes-nov", client=feed)) as updates:
            await self.next_matching(updates, lambda u: u.book.status == "live")
        self.assertEqual(feed.closed, 1)

    async def test_missing_live_dependency_fails_with_install_hint(self):
        with patch.dict("sys.modules", {"websockets.asyncio.client": None}):
            async with aclosing(OrderBookClient().stream(["x"])) as stream:
                with self.assertRaisesRegex(ImportError, "live"):
                    await anext(stream)


class TransportAndCliTests(unittest.TestCase):
    def test_message_arrays_and_malformed_payloads(self):
        self.assertEqual(len(messages(json.dumps([snapshot(), snapshot("no-nov")]))), 2)
        for raw in ("not json", "null", "[1]"):
            with self.assertRaises(DataError):
                messages(raw)
        self.assertEqual(timestamp(STAMP), datetime(2026, 9, 1, tzinfo=timezone.utc))

    @patch("polytrader.data.client.urlopen")
    def test_rest_request_is_public_and_token_addressed(self, urlopen):
        urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps(snapshot()).encode())
        OrderBookClient().get_book("yes-nov")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://clob.polymarket.com/book?token_id=yes-nov")
        self.assertNotIn("Authorization", request.headers)

    @patch("polytrader.cli.OrderBookClient")
    def test_cli_snapshot_and_partial_failure_exit_code(self, client_class):
        client_class.return_value.get_book.side_effect = snapshot
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["orderbook", "--token-id", "yes-nov"]), 0)
        self.assertEqual(json.loads(output.getvalue())["books"][0]["best_ask"]["size"], "50")
        client_class.return_value.get_book.side_effect = DataError("unavailable")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["orderbook", "--token-id", "yes-nov"]), 1)

    @patch("polytrader.cli.OrderBookClient")
    def test_cli_rejects_conflicting_selectors_without_network(self, client_class):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["orderbook", "event", "--token-id", "x"]), 1)
        client_class.return_value.get_event.assert_not_called()


class FakeSocket:
    def __init__(self, *, pong=True):
        self.sent = []
        self.incoming = asyncio.Queue()
        self.pong = pong
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def send(self, message):
        self.sent.append(message)
        if message == "PING" and self.pong:
            self.incoming.put_nowait("PONG")

    async def recv(self):
        return await self.incoming.get()


@unittest.skipUnless(importlib.util.find_spec("websockets"), "Install live extra for transport tests")
class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscription_initial_batch_and_application_heartbeat(self):
        socket = FakeSocket()
        socket.incoming.put_nowait(json.dumps([snapshot(), snapshot("yes-dec")]))
        with patch("websockets.asyncio.client.connect", return_value=socket), \
                patch("polytrader.orderbook.client.HEARTBEAT_INTERVAL", .005):
            async with aclosing(OrderBookClient().stream(["yes-nov", "yes-dec"])) as stream:
                self.assertEqual((await anext(stream))["asset_id"], "yes-nov")
                self.assertEqual((await anext(stream))["asset_id"], "yes-dec")
                self.assertIsNone(await anext(stream))  # Ping timer.
                self.assertIsNone(await anext(stream))  # Pong receipt.
                request = json.loads(socket.sent[0])
                self.assertEqual(request["assets_ids"], ["yes-nov", "yes-dec"])
                self.assertTrue(request["custom_feature_enabled"])
                self.assertIn("PING", socket.sent)
        self.assertTrue(socket.closed)

    async def test_missing_pong_terminates_connection(self):
        socket = FakeSocket(pong=False)
        with patch("websockets.asyncio.client.connect", return_value=socket), \
                patch("polytrader.orderbook.client.HEARTBEAT_INTERVAL", .005), \
                patch("polytrader.orderbook.client.HEARTBEAT_TIMEOUT", .02):
            async with aclosing(OrderBookClient().stream(["yes-nov"])) as stream:
                with self.assertRaisesRegex(DataError, "heartbeat timed out"):
                    async with asyncio.timeout(2):
                        async for _ in stream:
                            pass
        self.assertTrue(socket.closed)


if __name__ == "__main__":
    unittest.main()
