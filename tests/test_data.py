from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from polytrader.cli import main
from polytrader.data.client import DataError, PolymarketClient
from polytrader.data.discovery import event_slug, parse_market, select_market, select_token
from polytrader.data.history import MAX_WINDOW_SECONDS, UTC, fetch_history, resolve_window


def page(points=(), more=False, cursor=None):
    return {"data": list(points), "pagination": {"has_more": more, "next_cursor": cursor}}


def point(timestamp, price=0.5, resolution=300):
    return {"timestamp": timestamp, "price": price, "resolution_seconds": resolution}


class DiscoveryTests(unittest.TestCase):
    def test_event_url_with_tracking_and_trailing_slash(self):
        self.assertEqual(event_slug("https://polymarket.com/event/example/?x=1#chart"), "example")
        for source in ("https://example.org/event/example", "https://polymarket.com/event/a/b", "../a", ""):
            with self.subTest(source=source), self.assertRaises(ValueError):
                event_slug(source)

    def test_mapping_accepts_json_and_arrays_without_assuming_yes_first(self):
        for labels in ('["No", "Yes"]', ["No", "Yes"]):
            market = parse_market({"outcomes": labels, "clobTokenIds": '["111", "222"]'})
            self.assertEqual(select_token(market, "yEs"), ("Yes", "222"))

    def test_malformed_mapping_rejected(self):
        for raw in (
            {"outcomes": '["Yes", "No"]', "clobTokenIds": '["1"]'},
            {"outcomes": '["Yes", "yes"]', "clobTokenIds": '["1", "2"]'},
            {"outcomes": "broken"},
        ):
            with self.subTest(raw=raw), self.assertRaises(DataError):
                parse_market(raw)

    def test_no_tokens_and_ambiguous_market_selection(self):
        market = parse_market({"id": "12", "slug": "sample", "outcomes": '["Yes", "No"]'})
        self.assertEqual(market.tokens, {})
        self.assertIs(select_market([market], "12"), market)
        with self.assertRaises(ValueError):
            select_market([market, market], None)
        with self.assertRaises(ValueError):
            select_token(market, "Yes")


class WindowTests(unittest.TestCase):
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)

    def test_relative_days(self):
        start, end = resolve_window(days=0.5, now=self.now)
        self.assertEqual(end - start, 43200)
        self.assertEqual(end, int(self.now.timestamp()))

    def test_date_end_is_exclusive_midnight_and_offsets_use_utc(self):
        start, end = resolve_window(start="2026-09-01", end="2026-09-02", now=self.now)
        self.assertEqual(end - start, 86400)
        shifted, _ = resolve_window(start="2026-09-01T01:00:00+01:00", end="2026-09-02", now=self.now)
        self.assertEqual(shifted, start)

    def test_start_only_ends_now(self):
        self.assertEqual(resolve_window(start="2026-09-01", now=self.now)[1], int(self.now.timestamp()))

    def test_invalid_windows(self):
        cases = [
            {}, {"days": 0}, {"days": -1}, {"days": float("nan")},
            {"days": float("inf")}, {"days": 1e100},
            {"days": 2, "end": "2026-09-02"},
            {"start": "2026-09-02", "end": "2026-09-02"},
            {"start": "2026-09-03", "end": "2026-09-02"},
            {"start": "2026-09-01", "end": "2026-09-30"},
            {"start": "2026-02-30"}, {"start": "1960-01-01"},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                resolve_window(**values, now=self.now)


class HistoryTests(unittest.TestCase):
    def test_chunking_pagination_sorting_and_deduplication(self):
        client = Mock()
        boundary = 100 + MAX_WINDOW_SECONDS
        client.get_history_page.side_effect = [
            page([point(120), point(110)], True, "next"),
            page([point(120), point(boundary)]),
            page([point(boundary), point(boundary + 1), point(boundary + 2)]),
        ]
        points = fetch_history(client, "123", 100, boundary + 2, 300)
        self.assertEqual([p["timestamp"] for p in points], [110, 120, boundary, boundary + 1])
        calls = client.get_history_page.call_args_list
        self.assertEqual(calls[0].kwargs, {"start": 100, "end": boundary, "bucket_seconds": 300, "cursor": None})
        self.assertEqual(calls[1].kwargs["cursor"], "next")
        self.assertEqual(calls[1].kwargs["end"], boundary)
        self.assertEqual(calls[2].kwargs["start"], boundary)

    def test_missing_or_repeated_cursor_fails(self):
        for pages in ([page(more=True)], [page(more=True, cursor="same"), page(more=True, cursor="same")]):
            client = Mock()
            client.get_history_page.side_effect = pages
            with self.assertRaises(DataError):
                fetch_history(client, "1", 100, 200)

    def test_missing_schema_and_invalid_prices_fail(self):
        for response in ({}, page([point(110, float("nan"))]), page([point(110, 1.5)])):
            client = Mock()
            client.get_history_page.return_value = response
            with self.assertRaises(DataError):
                fetch_history(client, "1", 100, 200)

    def test_empty_and_settlement_points(self):
        client = Mock()
        client.get_history_page.return_value = page()
        self.assertEqual(fetch_history(client, "1", 100, 200), [])
        client.get_history_page.return_value = page([point(110), point(110, 1, 0)])
        self.assertEqual(len(fetch_history(client, "1", 100, 200)), 2)


class HttpTests(unittest.TestCase):
    @patch("polytrader.data.client.urlopen")
    def test_errors_are_actionable(self, urlopen):
        for error in (
            HTTPError("https://example.com", 429, "Too many requests", {}, io.BytesIO(b"slow down")),
            URLError("offline"), TimeoutError("timed out"),
        ):
            urlopen.side_effect = error
            with self.subTest(error=error), self.assertRaises(DataError):
                PolymarketClient().get_event("test")

    @patch("polytrader.data.client.urlopen")
    def test_query_encoding_and_timeout(self, urlopen):
        urlopen.return_value.__enter__.return_value = io.BytesIO(b'{"data": []}')
        PolymarketClient().get_history_page("123", cursor="a+b=", start=100, end=200)
        request = urlopen.call_args.args[0]
        self.assertIn("cursor=a%2Bb%3D", request.full_url)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 20)


class CliTests(unittest.TestCase):
    @patch("polytrader.cli.PolymarketClient")
    def test_history_file_includes_metadata(self, client_class):
        client = client_class.return_value
        client.get_event.return_value = {"title": "Example", "slug": "example", "markets": [
            {"id": "1", "slug": "question", "question": "Question?", "description": "Rules",
             "outcomes": '["Yes", "No"]', "clobTokenIds": '["111", "222"]'}
        ]}
        stamp = int(datetime(2026, 9, 1, 1, tzinfo=UTC).timestamp())
        client.get_history_page.return_value = page([point(stamp)])
        with tempfile.TemporaryDirectory() as folder, redirect_stderr(io.StringIO()):
            path = Path(folder) / "history.json"
            args = ["history", "example", "--start", "2026-09-01", "--end", "2026-09-02", "--output", str(path)]
            self.assertEqual(main(args), 0)
            result = json.loads(path.read_text())
            self.assertEqual(result["token_id"], "111")
            self.assertEqual(result["market"]["description"], "Rules")
            self.assertEqual(result["data"], [point(stamp)])
            self.assertEqual(main(args), 1)  # Existing files are not overwritten.

    @patch("polytrader.cli.PolymarketClient")
    def test_bad_arguments_do_not_make_requests(self, client_class):
        cases = [
            ["history", "event", "--token-id", "123", "--days", "7"],
            ["history", "--token-id", "123", "--outcome", "No", "--days", "7"],
            ["history", "event", "--days", "7", "--end", "2026-09-02"],
            ["history", "event", "--days", "7", "--bucket-seconds", "1"],
        ]
        for args in cases:
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 1)
        client_class.return_value.get_event.assert_not_called()
        client_class.return_value.get_history_page.assert_not_called()

    @patch("polytrader.cli.PolymarketClient")
    def test_empty_history_still_outputs_valid_json(self, client_class):
        client_class.return_value.get_history_page.return_value = page()
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(["history", "--token-id", "123", "--days", "1"]), 0)
        self.assertEqual(json.loads(output.getvalue())["data"], [])
        self.assertIn("No prices returned", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
