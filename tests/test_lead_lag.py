import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import pandas as pd

from polytrader.data.client import DataError
from polytrader.research.lead_lag import fetch_trade_window, historical_panels, load_historical_inputs

CONDITION = "0x" + "a" * 64


def trade(ts, token="123"):
    return dict(timestamp=ts, token_id=token, condition_id=CONDITION,
                side="BUY", price=0.5, size=10)


def page(rows, more=False, cursor=None):
    return dict(data=rows, pagination=dict(has_more=more, next_cursor=cursor))


class HistoricalInputsTests(unittest.TestCase):
    def test_window_filter_keeps_identical_fills_and_resumes_cached_pages(self):
        api = Mock()
        api.get_json.side_effect = [page([trade(200), trade(150), trade(150)], True, "next"),
                                   page([trade(100), trade(99)], True, "unused")]
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(DataError, "incomplete"):
                fetch_trade_window(CONDITION, 100, 200, cache_dir=folder, client=api, max_pages=1, pause_s=0)
            result = fetch_trade_window(CONDITION, 100, 200, cache_dir=folder, client=api, pause_s=0)
            self.assertEqual(result.timestamp.tolist(), [150, 150, 100])
            self.assertEqual(api.get_json.call_count, 2)
            api.get_json.reset_mock()
            again = fetch_trade_window(CONDITION, 100, 200, cache_dir=folder, client=api, pause_s=0)
            pd.testing.assert_frame_equal(result, again)
            api.get_json.assert_not_called()

    def test_bad_pagination_and_wrong_market_fail(self):
        bad = trade(150)
        bad["condition_id"] = "wrong"
        for payload in [page([trade(150)], True, None), page([], True, "next"), page([bad])]:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as folder:
                api = Mock()
                api.get_json.return_value = payload
                with self.assertRaises(DataError):
                    fetch_trade_window(CONDITION, 100, 200, cache_dir=folder, client=api, pause_s=0)

    def test_repeated_cursor_and_time_reversal_fail(self):
        for second in [page([trade(120)], True, "next"), page([trade(160)])]:
            with self.subTest(second=second), tempfile.TemporaryDirectory() as folder:
                api = Mock()
                api.get_json.side_effect = [page([trade(150)], True, "next"), second]
                with self.assertRaises(DataError):
                    fetch_trade_window(CONDITION, 100, 200, cache_dir=folder, client=api, pause_s=0)

    def test_token_filter_and_settlement_exclusion(self):
        api = Mock()
        api.get_history_page.return_value = page([
            dict(timestamp=120, price=.4, resolution_seconds=60),
            dict(timestamp=180, price=1, resolution_seconds=0)])
        api.get_json.return_value = page([trade(150, "456"), trade(140)])
        with tempfile.TemporaryDirectory() as folder:
            prices, trades = load_historical_inputs({"123": CONDITION}, 100, 200,
                                                    cache_dir=folder, client=api, log=lambda _: None)
        self.assertEqual(prices.price.tolist(), [.4])
        self.assertEqual(trades.token_id.tolist(), ["123"])
        self.assertEqual(trades.notional_usdc.tolist(), [5])

    def test_missing_prices_not_filled_and_future_tick_not_backdated(self):
        times = pd.to_datetime([0, 65, 180], unit="s", utc=True)
        prices = pd.DataFrame(dict(timestamp=times, token_id=["123"] * 3,
                                   price=[.4, .9, .6], resolution_seconds=[60] * 3))
        panel = historical_panels(prices, ["123"])["123"]
        self.assertTrue(pd.isna(panel.price.iloc[1]))
        self.assertTrue(pd.isna(panel.price.iloc[2]))
        self.assertEqual(panel.price.iloc[-1], .6)
        with self.assertRaisesRegex(ValueError, "finer"):
            historical_panels(prices, ["123"], grid="5s")

    def test_empty_price_history_is_explicit_failure(self):
        api = Mock()
        api.get_history_page.return_value = page([])
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(DataError, "No price coverage"):
                load_historical_inputs({"123": CONDITION}, 100, 200,
                                       cache_dir=folder, client=api, log=lambda _: None)
        api.get_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
