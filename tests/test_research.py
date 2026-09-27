from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from datetime import datetime, timezone

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

    def test_bars_include_yes_equivalent_ohlc(self):
        import pandas as pd
        from polytrader.research.pull import volume_bars
        trades = pd.DataFrame({
            "timestamp": [3, 1, 2], "asset": ["Y", "Y", "N"],
            "side": ["BUY", "BUY", "SELL"], "size": [1.0, 1.0, 1.0],
            "price": [0.6, 0.2, 0.7],
        })
        bar = volume_bars(trades, "Y", "1h").iloc[0]
        self.assertEqual((bar.open, bar.high, bar.low, bar.close), (0.2, 0.6, 0.2, 0.6))

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


def market(condition_id, *, closed="2026-09-01T00:00:00Z", volume=5, question="Will it happen?",
           outcomes='["Yes","No"]'):
    return {
        "id": condition_id, "conditionId": condition_id, "slug": f"market-{condition_id}",
        "question": question, "outcomes": outcomes, "clobTokenIds": '["11","12"]',
        "outcomePrices": '["1","0"]', "volumeNum": volume,
        "createdAt": "2026-01-01T00:00:00Z", "closedTime": closed,
    }


class FakeEvents:
    def __init__(self):
        self.calls = []

    def get_json(self, url, **params):
        self.calls.append(params)
        if params.get("after_cursor") is None:
            return {"events": [{"id": "e1", "tags": [{"label": "Politics"}], "markets": [
                market("keep"), market("zero", volume=0),
                market("old", closed="2024-01-01T00:00:00Z"),
            ]}], "next_cursor": "page-2"}
        return {"events": [{"id": "e2", "tags": [{"label": "Crypto"}], "markets": [
            market("updown", question="BTC Up or Down?", outcomes='["Up","Down"]'),
        ]}], "next_cursor": None}


class UniverseTests(unittest.TestCase):
    def test_keyset_pagination_filters_and_keeps_updown_flag(self):
        from polytrader.research.pull import enumerate_universe
        with tempfile.TemporaryDirectory() as folder:
            api = FakeEvents()
            frame = enumerate_universe(
                folder, client=api, rps=1_000_000,
                now=datetime(2026, 9, 27, tzinfo=timezone.utc), log=lambda *_: None,
            )
            self.assertEqual(set(frame.condition_id), {"keep", "updown"})
            selected = dict(zip(frame.condition_id, frame.selected))
            self.assertTrue(selected["keep"])
            self.assertFalse(selected["updown"])
            self.assertEqual([call["after_cursor"] for call in api.calls], [None, "page-2"])
            self.assertTrue((Path(folder) / "raw" / "events-000001.jsonl.zst").exists())
            self.assertTrue((Path(folder) / "universe.parquet").exists())

    def test_stratified_sampling_is_deterministic_and_weighted(self):
        import pandas as pd
        from polytrader.research.pull import stratified_sample
        rows = pd.DataFrame({
            "condition_id": [f"c{i:03}" for i in range(100)],
            "closed_time": ["2026-09-01T00:00:00Z"] * 100,
            "volume_shares": list(range(1, 101)),
            "top_category": ["A" if i % 2 else "B" for i in range(100)],
            "is_updown": [False] * 100,
        })
        first = stratified_sample(rows, cap=30, seed=7)
        second = stratified_sample(rows.sample(frac=1, random_state=9), cap=30, seed=7)
        self.assertEqual(set(first.loc[first.selected, "condition_id"]),
                         set(second.loc[second.selected, "condition_id"]))
        self.assertEqual(int(first.selected.sum()), 30)
        self.assertTrue((first.loc[first.selected, "weight"] >= 1).all())
        self.assertEqual(set(first.stratum), set(first.loc[first.selected, "stratum"]))


class PriceAndCompactTests(unittest.TestCase):
    def _dataset(self, folder):
        import pandas as pd
        from polytrader.research.storage import atomic_parquet
        out = Path(folder)
        universe = pd.DataFrame([{
            "condition_id": "c1", "selected": True, "selection_order": 0,
            "close_month": "2026-09", "event_id": "e1", "yes_token": "11",
            "outcomes": ["Yes", "No"], "tokens": ["11", "12"],
            "outcome_prices": [1.0, 0.0], "resolved_outcome": "Yes", "cancelled": False,
            "created_at": pd.Timestamp("2026-09-01", tz="UTC"),
            "closed_time": pd.Timestamp("2026-09-02", tz="UTC"),
            "volume_shares": 3.0, "top_category": "Test",
        }])
        atomic_parquet(universe, out / "universe.parquet")
        atomic_parquet(pd.DataFrame([{"event_id": "e1", "event_title": "Event"}]),
                       out / ".staging" / "metadata" / "events.parquet")
        atomic_parquet(pd.DataFrame([{"condition_id": "c1", "tag": "Test"}]),
                       out / ".staging" / "metadata" / "market_tags.parquet")
        return out

    def test_price_resolution_fallback_and_resume(self):
        import json
        from polytrader.research.pull import pull_prices
        with tempfile.TemporaryDirectory() as folder:
            out = self._dataset(folder)
            calls = []

            def fake_history(client, token, start, end, bucket):
                calls.append((token, bucket))
                return [] if bucket == 60 else [{
                    "timestamp": start + 1, "price": 0.5, "resolution_seconds": 300,
                }]

            with patch("polytrader.research.pull.fetch_history", side_effect=fake_history):
                result = pull_prices(out, workers=1, rps=1_000_000, log=lambda *_: None)
                resumed = pull_prices(out, workers=1, rps=1_000_000, log=lambda *_: None)
            self.assertEqual(calls, [("11", 60), ("11", 300), ("12", 60), ("12", 300)])
            self.assertEqual((result["fetched"], resumed["skipped"]), (1, 1))
            status = json.loads((out / ".staging" / "price_status" / "c1.json").read_text())
            self.assertEqual(status["tokens"][0]["selected_bucket_seconds"], 300)
            self.assertFalse(list(out.rglob("*.tmp")))

    def test_compaction_writes_hive_partitions_and_three_bar_sets(self):
        import pandas as pd
        from polytrader.research.pull import PRICE_COLUMNS, TRADE_COLUMNS, compact_dataset
        from polytrader.research.storage import atomic_parquet
        with tempfile.TemporaryDirectory() as folder:
            out = self._dataset(folder)
            trades = pd.DataFrame({
                "condition_id": ["c1", "c1"], "timestamp": [1_777_000_000, 1_777_000_030],
                "asset": ["11", "12"], "outcome_index": [0, 1], "side": ["BUY", "BUY"],
                "size": [1.0, 2.0], "price": [0.4, 0.6], "wallet": ["w", "w"],
                "tx": ["a", "b"],
            }).astype(TRADE_COLUMNS)
            prices = pd.DataFrame({
                "condition_id": ["c1"], "token_id": ["11"], "outcome": ["Yes"],
                "timestamp": [1_777_000_000], "price": [0.4], "resolution_seconds": [300],
                "requested_bucket_seconds": [300],
            }).astype(PRICE_COLUMNS)
            atomic_parquet(trades, out / ".staging" / "trades" / "c1.parquet")
            atomic_parquet(prices, out / ".staging" / "prices" / "c1.parquet")
            counts = compact_dataset(out, log=lambda *_: None)
            self.assertEqual(counts["trades"], 2)
            for table in ("trades", "prices", "bars_1min", "bars_1h", "bars_1d"):
                self.assertTrue((out / table / "close_month=2026-09" / "part-00000.parquet").exists())


if __name__ == "__main__":
    unittest.main()
