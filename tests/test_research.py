from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from polytrader.data.client import DataError
from polytrader.data.trades import PAGE_SIZE, fetch_trades


class FakeTrades:
    """Serves trades like the data API: newest first, inclusive start/end, 500 per page."""

    def __init__(self, timestamps):
        self.rows = [{"timestamp": t, "id": i} for i, t in enumerate(timestamps)]
        self.rows.sort(key=lambda r: -r["timestamp"])
        self.calls = []

    def get_list(self, url, *, market, limit, takerOnly, start=None, end=None, offset=None):
        self.calls.append((start, end, offset))
        rows = [r for r in self.rows if (start is None or r["timestamp"] >= start)
                and (end is None or r["timestamp"] <= end)]
        return rows[offset or 0:(offset or 0) + limit]


class FetchTradesTests(unittest.TestCase):
    def check(self, timestamps, **window):
        api = FakeTrades(timestamps)
        result = fetch_trades(api, "0xabc", **window)
        start, end = window.get("start"), window.get("end")
        expected = sorted(r["id"] for r in api.rows if (start is None or r["timestamp"] >= start)
                          and (end is None or r["timestamp"] <= end))
        self.assertEqual(sorted(r["id"] for r in result), expected)  # Complete, no duplicates.
        return api

    def test_single_page(self):
        self.check(range(10))

    def test_many_pages_with_distinct_seconds(self):
        self.check(range(1_750))

    def test_page_boundary_inside_a_crowded_second(self):
        # Many trades share seconds, so page edges fall mid-second.
        self.check([t // 7 for t in range(3_000)])

    def test_more_than_a_page_in_one_second(self):
        self.check([5] * 1_234 + list(range(6, 900)) + [1, 2])

    def test_exactly_one_full_page(self):
        self.check(range(PAGE_SIZE))

    def test_start_and_end_bounds(self):
        self.check(range(2_000), start=300, end=1_600)

    def test_refuses_partial_second_beyond_offset_cap(self):
        with self.assertRaises(DataError):
            fetch_trades(FakeTrades([9] * 10_600), "0xabc")


class VolumeBarTests(unittest.TestCase):
    def test_yes_equivalent_price_and_flow(self):
        import pandas as pd
        from polytrader.research.pull import volume_bars
        trades = pd.DataFrame({
            "timestamp": [0, 10, 3_700], "asset": ["Y", "N", "Y"], "side": ["BUY", "BUY", "SELL"],
            "size": [10.0, 30.0, 5.0], "price": [0.4, 0.5, 0.45]})
        bars = volume_bars(trades, "Y", "1h")
        first, second = bars.iloc[0], bars.iloc[1]
        self.assertEqual((first.trades, first.shares), (2, 40.0))
        self.assertAlmostEqual(first.usd, 10 * 0.4 + 30 * 0.5)
        self.assertAlmostEqual(first.yes_vwap, (10 * 0.4 + 30 * 0.5) / 40)  # NO at .5 = YES at .5.
        self.assertEqual(first.net_yes_shares, 10 - 30)  # Buying NO is flow away from YES.
        self.assertEqual(second.net_yes_shares, -5)

    def test_market_rows_record_winner_and_tokens(self):
        from polytrader.research.pull import _market_row
        row = _market_row({"id": 1, "slug": "e", "tags": [{"label": "Politics"}]}, {
            "id": "7", "conditionId": "0xc", "slug": "m", "question": "Will BTC go Up or Down?",
            "outcomes": '["No", "Yes"]', "clobTokenIds": '["n", "y"]', "outcomePrices": '["0", "1"]',
            "volumeNum": 5})
        self.assertEqual((row["resolved_outcome"], row["yes_token"], row["no_token"]), ("Yes", "y", "n"))
        self.assertEqual(row["tags"], ["Politics"])
        self.assertTrue(row["is_updown"])
        updown = _market_row({"id": 2}, {
            "id": "8", "conditionId": "0xd", "question": "Bitcoin Up or Down?", "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["u", "d"]', "outcomePrices": '["1", "0"]', "volumeNum": 5})
        self.assertEqual((updown["yes_token"], updown["no_token"], updown["resolved_outcome"]), ("u", "d", "Up"))


if __name__ == "__main__":
    unittest.main()
