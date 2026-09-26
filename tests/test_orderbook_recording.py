import asyncio
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from polytrader.cli import main
from polytrader.data import DataError
from polytrader.orderbook import (
    BookSnapshot, Level, QuoteRecorder, load_recording, record_orderbooks,
    record_quotes, replay_orderbooks, resolve_books,
)
from polytrader.orderbook.viewer import FeedHub, ReplayController


NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)


def collection():
    result = resolve_books(token_ids=["nov-yes", "dec-yes"])
    for token in result.books:
        result.books[token] = BookSnapshot(token, (Level(Decimal(".4"), Decimal("10")),),
                                           (Level(Decimal(".5"), Decimal("20")),), NOW, NOW, "live")
    return result


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "quotes.jsonl"

    def test_changes_only_roundtrip_preserves_decimal_sizes_and_stale_state(self):
        books = collection()
        with QuoteRecorder(books, self.path) as writer:
            self.assertTrue(writer.sample(books, 0))
            # A depth-only update shouldn't write another best-quote row.
            books.books["nov-yes"] = replace(books.books["nov-yes"], bids=(
                Level(Decimal(".40"), Decimal("10.0")), Level(Decimal(".3"), Decimal("200"))))
            writer.sample(books, 1)
            self.assertEqual(writer.frames, 1)
            books.books["nov-yes"] = replace(books.books["nov-yes"],
                asks=(Level(Decimal(".51"), Decimal("12.3456")),))
            writer.sample(books, 2)
            books.books["dec-yes"] = replace(books.books["dec-yes"], status="stale", reason="offline")
            writer.sample(books, 3)
            result = writer.finish("duration", 10)
        self.assertEqual(result.frames, 3)
        self.assertEqual(result.quote_changes, 4)
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(rows[0]["format"], "polytrader.quotes")
        self.assertNotIn("raw", rows[0]["markets"][0])
        self.assertEqual(len(rows[2]["q"]), 1)
        frames = list(load_recording(self.path).frames())
        self.assertEqual([f.elapsed_seconds for f in frames], [0, 2, 3, 10])
        self.assertEqual(frames[1].quotes["nov-yes"].spread, Decimal(".11"))
        self.assertEqual(frames[1].quotes["nov-yes"].best_ask.size, Decimal("12.3456"))
        self.assertEqual(frames[0].quotes["nov-yes"].best_ask.size, Decimal(20))
        self.assertEqual(frames[-1].quotes["dec-yes"].status, "stale")
        self.assertEqual(frames[-1].end_reason, "duration")
        self.assertEqual(result.bytes_written, self.path.stat().st_size)

    def test_empty_side_and_reconnection_are_recorded(self):
        books = collection()
        with QuoteRecorder(books, self.path) as writer:
            writer.sample(books, 0)
            books.books["nov-yes"] = replace(books.books["nov-yes"], asks=(), status="stale")
            writer.sample(books, 1)
            books.books["nov-yes"] = replace(books.books["nov-yes"], status="live")
            writer.sample(books, 2)
        frames = list(load_recording(self.path).frames())
        self.assertIsNone(frames[1].quotes["nov-yes"].spread)
        self.assertIsNone(frames[1].quotes["nov-yes"].best_ask)
        self.assertEqual(frames[2].quotes["nov-yes"].status, "live")

    def test_byte_limit_is_hard_including_metadata_and_footer(self):
        books = collection()
        with QuoteRecorder(books, self.path, max_bytes=2048) as writer:
            for index in range(100):
                books.books["nov-yes"] = replace(books.books["nov-yes"],
                    bids=(Level(Decimal(".4"), Decimal(index + 1)),))
                if not writer.sample(books, index):
                    result = writer.finish("size_limit", index)
                    break
            else:
                self.fail("Expected the recording to reach its byte limit")
        self.assertLessEqual(self.path.stat().st_size, 2048)
        self.assertEqual(result.reason, "size_limit")
        self.assertEqual(list(load_recording(self.path).frames())[-1].end_reason, "size_limit")

    def test_never_overwrites_and_rejects_bad_limits_before_opening(self):
        for values in ({"interval": .01}, {"interval": float("nan")}, {"duration": 0},
                       {"max_bytes": 2}, {"duration": float("inf")}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                QuoteRecorder(collection(), self.path, **values)
            self.assertFalse(self.path.exists())
        self.path.write_text("keep")
        with self.assertRaises(FileExistsError):
            QuoteRecorder(collection(), self.path)
        self.assertEqual(self.path.read_text(), "keep")

    def test_selection_changes_and_reversed_times_are_rejected(self):
        books = collection()
        with QuoteRecorder(books, self.path) as writer:
            writer.sample(books, 1)
            with self.assertRaises(ValueError):
                writer.sample(books, 0)
            books.books.pop("dec-yes")
            with self.assertRaises(ValueError):
                writer.sample(books, 2)

    def test_partial_last_line_recovers_previous_complete_frames(self):
        with QuoteRecorder(collection(), self.path) as writer:
            writer.sample(collection(), 0)
        lines = self.path.read_bytes().splitlines(keepends=True)
        self.path.write_bytes(b"".join(lines[:-1]) + b'{"type":"sample","t":2')
        frames = list(load_recording(self.path).frames())
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[-1].end_reason, "incomplete")
        self.assertEqual(len(frames[-1].quotes), 2)

    def test_corrupt_complete_lines_and_versions_are_rejected(self):
        with QuoteRecorder(collection(), self.path) as writer:
            writer.sample(collection(), 0)
        content = self.path.read_text().splitlines(keepends=True)
        for bad in ('not json\n', '{"type":"sample","t":-1,"q":[]}\n',
                    '{"type":"sample","t":2,"q":[{"i":99}]}\n'):
            self.path.write_text(content[0] + bad)
            with self.assertRaises(DataError):
                list(load_recording(self.path).frames())
        header = json.loads(content[0])
        header["version"] = 99
        self.path.write_text(json.dumps(header) + "\n")
        with self.assertRaises(DataError):
            load_recording(self.path)

    def test_cli_instant_replay_uses_no_network(self):
        with QuoteRecorder(collection(), self.path) as writer:
            writer.sample(collection(), 0)
            writer.finish("duration", 100)
        output = io.StringIO()
        with patch("polytrader.data.client.urlopen", side_effect=AssertionError("Network")), redirect_stdout(output):
            self.assertEqual(main(["replay", str(self.path), "--instant"]), 0)
        rows = [json.loads(row) for row in output.getvalue().splitlines()]
        self.assertEqual(rows[-1]["end_reason"], "duration")
        self.assertEqual(rows[0]["quotes"][0]["spread"], "0.1")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["replay", str(self.path), "--speed", "0"]), 1)


class RecordingAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "recording.jsonl"

    async def test_duration_limit_and_cancellation_leave_readable_files(self):
        result = await record_quotes(collection(), self.path, duration=.025)
        self.assertEqual(result.reason, "duration")
        self.assertEqual(result.frames, 1)  # Quiet quotes stay compact.
        self.assertGreaterEqual(result.elapsed_seconds, .025)
        other = self.path.with_name("interrupted.jsonl")
        task = asyncio.create_task(record_quotes(collection(), other, interval=.1))
        await asyncio.sleep(.015)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(list(load_recording(other).frames())[-1].end_reason, "interrupted")

    async def test_service_recording_and_shutdown(self):
        class Client:
            closed = False
            async def stream(self, tokens):
                try:
                    yield {"event_type": "book", "asset_id": tokens[0], "timestamp": "1788220800000",
                           "bids": [{"price": ".4", "size": "10"}], "asks": [{"price": ".5", "size": "20"}]}
                    await asyncio.Event().wait()
                finally:
                    self.closed = True
        client = Client()
        result = await record_orderbooks(token_ids="x", output=self.path, duration=.025, client=client)
        self.assertEqual(result.reason, "duration")
        self.assertTrue(client.closed)
        frames = list(load_recording(self.path).frames())
        self.assertEqual(frames[-1].quotes["x"].spread, Decimal(".1"))

    async def test_source_failure_is_saved_and_propagated(self):
        async def fail():
            await asyncio.sleep(.005)
            raise DataError("Disconnected permanently")
        source = asyncio.create_task(fail())
        with self.assertRaisesRegex(DataError, "permanently"):
            await record_quotes(collection(), self.path, source_task=source)
        self.assertEqual(list(load_recording(self.path).frames())[-1].end_reason, "error")

    async def test_replay_pacing_and_offline_viewer(self):
        with QuoteRecorder(collection(), self.path) as writer:
            writer.sample(collection(), 0)
            writer.finish("duration", .1)
        loop = asyncio.get_running_loop()
        start = loop.time()
        frames = [f async for f in replay_orderbooks(self.path, speed=2)]
        self.assertGreaterEqual(loop.time() - start, .045)
        self.assertEqual(frames[-1].elapsed_seconds, .1)
        hub = FeedHub()
        controller = ReplayController(hub, load_recording(self.path))
        with patch("polytrader.data.client.urlopen", side_effect=AssertionError("Network")):
            controller.play(100)
            await controller.task
            payload = hub.read()
            self.assertEqual(payload["mode"], "replay")
            self.assertEqual(payload["state"], "replay_finished")
            self.assertEqual(len(payload["books"][0]["bids"]), 1)
            controller.play(100)  # Restart from disk.
            await controller.task
        await controller.close()


if __name__ == "__main__":
    unittest.main()
