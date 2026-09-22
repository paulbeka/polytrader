from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from polytrader.data import DataError, PriceHistory, fetch_price_history, load_history


POINT = {"timestamp": 1788220800, "price": 0.3, "resolution_seconds": 300}


class PythonApiTests(unittest.TestCase):
    def test_fetch_in_memory_has_same_envelope_and_does_not_print(self):
        client = Mock()
        client.get_history_page.return_value = {"data": [POINT], "pagination": {"has_more": False}}
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            history = fetch_price_history(
                token_id="123", start="2026-09-01", end="2026-09-02", client=client,
            )
        self.assertIsInstance(history, PriceHistory)
        self.assertEqual(history.data, [POINT])
        self.assertEqual(history.metadata["token_id"], "123")
        self.assertEqual(history.metadata["window"]["end"], "2026-09-02T00:00:00+00:00")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_relative_window_and_event_outcome_selection(self):
        client = Mock()
        client.get_event.return_value = {"id": "12", "markets": [
            {"slug": "choice", "outcomes": '["No", "Yes"]', "clobTokenIds": '["321", "123"]'},
            {"slug": "other", "outcomes": '["Yes", "No"]', "clobTokenIds": '["456", "654"]'},
        ]}
        client.get_history_page.return_value = {"data": [], "pagination": {"has_more": False}}
        history = fetch_price_history("example", market="choice", outcome="No", days=7, client=client)
        self.assertEqual(history.metadata["token_id"], "321")
        self.assertEqual(history.metadata["market"]["slug"], "choice")
        args = client.get_history_page.call_args.kwargs
        self.assertEqual(args["end"] - args["start"], 7 * 86400)
        self.assertEqual(history.data, [])

    def test_load_and_save_preserve_existing_json_and_unknown_metadata(self):
        payload = {"token_id": "123", "market": {"description": "Rules"}, "extra": "keep", "data": [POINT]}
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "existing.json"
            source.write_text(json.dumps(payload), encoding="utf-8-sig")
            with patch("polytrader.data.client.urlopen", side_effect=AssertionError("Unexpected network")):
                history = load_history(source)
            self.assertEqual(history.to_dict(), payload)
            destination = history.save(Path(folder) / "nested" / "copy.json")
            self.assertEqual(json.loads(destination.read_text()), payload)
            with self.assertRaises(FileExistsError):
                history.save(destination)
            self.assertEqual(json.loads(destination.read_text()), payload)

    def test_bad_local_files_fail_clearly(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            for content in ('oops', '[]', '{}', '{"data": [null]}', '{"data": [{"timestamp": 123}]}'):
                with self.subTest(content=content):
                    path.write_text(content)
                    with self.assertRaises(DataError):
                        load_history(path)
            with self.assertRaises(FileNotFoundError):
                load_history(Path(folder) / "absent.json")

    def test_dataframe_dependency_is_optional(self):
        history = PriceHistory(data=[POINT], metadata={})
        with patch.dict("sys.modules", {"pandas": None}):
            self.assertEqual(history.to_dict()["data"], [POINT])
            with self.assertRaisesRegex(ImportError, "sandbox"):
                history.to_frame()


@unittest.skipUnless(importlib.util.find_spec("pandas"), "Install sandbox extras for DataFrame tests")
class DataFrameTests(unittest.TestCase):
    def test_frame_sorts_utc_without_losing_shared_timestamps(self):
        later = {**POINT, "timestamp": POINT["timestamp"] + 300}
        settlement = {**POINT, "price": 1, "resolution_seconds": 0}
        history = PriceHistory(data=[later, POINT, settlement], metadata={"token_id": "123"})
        frame = history.to_frame()
        self.assertEqual(str(frame.index.tz), "UTC")
        self.assertEqual(frame.index.name, "datetime")
        self.assertTrue(frame.index.is_monotonic_increasing)
        self.assertEqual(len(frame), 3)
        self.assertEqual(frame.index[0].to_pydatetime(), datetime.fromtimestamp(POINT["timestamp"], timezone.utc))
        self.assertEqual(frame["resolution_seconds"].tolist(), [300, 0, 300])
        frame.iloc[0, frame.columns.get_loc("price")] = 0.9
        self.assertEqual(history.data[1]["price"], 0.3)

    def test_empty_frame_has_expected_columns_and_utc_index(self):
        frame = PriceHistory(data=[], metadata={}).to_frame()
        self.assertTrue(frame.empty)
        self.assertEqual(list(frame.columns), ["timestamp", "price", "resolution_seconds"])
        self.assertEqual(str(frame.index.tz), "UTC")


if __name__ == "__main__":
    unittest.main()
