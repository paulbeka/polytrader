"""Bounded, sampled best-quote recordings and offline replay (stdlib only)."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
from pathlib import Path

from polytrader.data import DataError
from polytrader.orderbook.api import resolve_books
from polytrader.orderbook.book import decimal_value
from polytrader.orderbook.models import Level, OrderBooks
from polytrader.orderbook.service import OrderBookService

FORMAT = "polytrader.quotes"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
FOOTER_RESERVE = 512
STATUSES = {"initializing", "live", "stale", "unavailable", "snapshot"}


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def _settings(interval, duration, max_bytes):
    _positive(interval, "interval")
    _positive(duration, "duration")
    if interval < .1:
        raise ValueError("interval must be at least 0.1 seconds")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1024:
        raise ValueError("max_bytes must be an integer of at least 1024")


def _encode(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _date(value):
    if not isinstance(value, str):
        raise ValueError("Expected an ISO timestamp")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamp must have a timezone")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class QuoteSnapshot:
    """Recorded best quotes only; no full-depth book is implied."""

    token_id: str
    best_bid: Level | None
    best_ask: Level | None
    status: str
    updated_at: datetime | None
    reason: str | None = None

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask.price - self.best_bid.price
        return None

    def to_dict(self):
        return {"token_id": self.token_id,
                "best_bid": self.best_bid.to_dict() if self.best_bid else None,
                "best_ask": self.best_ask.to_dict() if self.best_ask else None,
                "spread": str(self.spread) if self.spread is not None else None,
                "status": self.status, "reason": self.reason,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None}


@dataclass(frozen=True)
class QuoteFrame:
    elapsed_seconds: float
    recorded_at: datetime
    quotes: dict[str, QuoteSnapshot]
    end_reason: str | None = None

    def to_dict(self):
        return {"elapsed_seconds": self.elapsed_seconds, "recorded_at": self.recorded_at.isoformat(),
                "quotes": [q.to_dict() for q in self.quotes.values()], "end_reason": self.end_reason}


@dataclass(frozen=True)
class RecordingResult:
    path: Path
    frames: int
    quote_changes: int
    bytes_written: int
    elapsed_seconds: float
    reason: str


class QuoteRecorder:
    """Incremental writer for an existing collection; sample() performs no network I/O.

    A new file is opened exclusively and flushed per frame. Unchanged best quotes
    are omitted, even if deeper levels changed. finish() always fits the byte cap.
    Use as a context manager so interruptions leave a readable end marker.
    """

    def __init__(self, collection: OrderBooks, output, *, interval=1.0, duration=3600.0,
                 max_bytes=DEFAULT_MAX_BYTES):
        _settings(interval, duration, max_bytes)
        if not collection.books:
            raise ValueError("No outcome books to record")
        self.path = Path(output)
        self.max_bytes = max_bytes
        self.tokens = list(collection.books)
        self.previous = {}
        self.frames = self.quote_changes = self.bytes_written = 0
        self.elapsed = 0.0
        self.result = None
        self.started_at = datetime.now(timezone.utc)
        markets = [{k: v for k, v in collection.markets[t].to_dict().items() if k != "raw"}
                   for t in self.tokens]
        header = _encode({"type": "header", "format": FORMAT, "version": 1,
                          "started_at": self.started_at.isoformat(), "interval_seconds": interval,
                          "duration_limit_seconds": duration, "max_bytes": max_bytes,
                          "changes_only": True, "event": collection.event,
                          "markets": markets, "excluded": collection.excluded})
        if len(header) + FOOTER_RESERVE > max_bytes:
            raise ValueError("Recording metadata exceeds max_bytes; increase the cap or select fewer markets")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("xb")
        try:
            self.file.write(header)
            self.file.flush()
            self.bytes_written = len(header)
        except BaseException:
            self.file.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.result is None:
            self.finish("interrupted" if exc_type else "stopped", self.elapsed)

    def sample(self, collection: OrderBooks, elapsed_seconds: float) -> bool:
        """Return False if the byte cap is reached. No partial frame is written."""
        if self.result is not None:
            raise ValueError("Recording is already finished")
        if not math.isfinite(elapsed_seconds) or elapsed_seconds < self.elapsed:
            raise ValueError("Sample times must be finite and nondecreasing")
        if list(collection.books) != self.tokens:
            raise ValueError("Recording selection changed; start a new file for a different selection")
        self.elapsed = elapsed_seconds
        changes, fingerprints = [], {}
        for index, token in enumerate(self.tokens):
            book = collection.books[token]
            bid = [str(book.best_bid.price), str(book.best_bid.size)] if book.best_bid else None
            ask = [str(book.best_ask.price), str(book.best_ask.size)] if book.best_ask else None
            # Decimal equality avoids saving formatting-only changes (e.g. .50 vs .5).
            fingerprint = (book.best_bid, book.best_ask, book.status, book.reason)
            fingerprints[token] = fingerprint
            if fingerprint != self.previous.get(token):
                changes.append({"i": index, "b": bid, "a": ask, "s": book.status,
                                "u": book.updated_at.isoformat() if book.updated_at else None,
                                "r": book.reason})
        if not changes:
            return True
        frame = _encode({"type": "sample", "t": round(elapsed_seconds, 6), "q": changes})
        if self.bytes_written + len(frame) + FOOTER_RESERVE > self.max_bytes:
            return False
        self.file.write(frame)
        self.file.flush()
        self.bytes_written += len(frame)
        self.frames += 1
        self.quote_changes += len(changes)
        self.previous = fingerprints
        return True

    def finish(self, reason="stopped", elapsed_seconds=None):
        if self.result is not None:
            return self.result
        elapsed = self.elapsed if elapsed_seconds is None else elapsed_seconds
        if not math.isfinite(elapsed) or elapsed < self.elapsed:
            raise ValueError("End time must be finite and not precede samples")
        footer = _encode({"type": "end", "t": round(elapsed, 6), "reason": reason,
                          "frames": self.frames, "quote_changes": self.quote_changes})
        try:
            if len(footer) > FOOTER_RESERVE:
                raise ValueError("Recording end marker is too large")
            self.file.write(footer)
            self.file.flush()
            self.bytes_written += len(footer)
        finally:
            self.file.close()
        self.result = RecordingResult(self.path, self.frames, self.quote_changes,
                                      self.bytes_written, elapsed, reason)
        return self.result


async def record_quotes(collection, output, *, interval=1.0, duration=3600.0,
                        max_bytes=DEFAULT_MAX_BYTES, source_task=None):
    """Sample an already-maintained collection; reuse a bot's service connection.

    source_task may be a task consuming service.updates(); errors are propagated
    after saving available quotes. Cancellation saves an interrupted end marker.
    """
    loop = asyncio.get_running_loop()
    with QuoteRecorder(collection, output, interval=interval, duration=duration, max_bytes=max_bytes) as writer:
        started = loop.time()
        next_sample = started
        try:
            while True:
                elapsed = loop.time() - started
                if not writer.sample(collection, elapsed):
                    return writer.finish("size_limit", elapsed)
                if source_task is not None and source_task.done():
                    await source_task
                    return writer.finish("source_complete", elapsed)
                if elapsed >= duration:
                    return writer.finish("duration", elapsed)
                # No catch-up bursts after a slow write or a busy event loop.
                next_sample = max(next_sample + interval, loop.time())
                delay = max(0, min(next_sample, started + duration) - loop.time())
                if source_task is None:
                    await asyncio.sleep(delay)
                else:
                    await asyncio.wait({source_task}, timeout=delay)
        except asyncio.CancelledError:
            writer.finish("interrupted", loop.time() - started)
            raise
        except Exception:
            writer.finish("error", loop.time() - started)
            raise


async def record_orderbooks(event=None, *, output, markets=None, outcomes=None, token_ids=None,
                           deadlines=None, interval=1.0, duration=3600.0,
                           max_bytes=DEFAULT_MAX_BYTES, client=None, max_retries=5):
    """Maintain and record best quotes. Duration includes stream initialization."""
    _settings(interval, duration, max_bytes)
    if Path(output).exists():
        raise FileExistsError(f"Recording already exists: {output}")
    collection = await asyncio.to_thread(resolve_books, event, markets=markets, outcomes=outcomes,
                                         token_ids=token_ids, deadlines=deadlines, client=client)
    async with OrderBookService(collection, client=client, max_retries=max_retries) as service:
        async def consume():
            async for _ in service.updates():
                pass
        source = asyncio.create_task(consume())
        try:
            return await record_quotes(collection, output, interval=interval, duration=duration,
                                       max_bytes=max_bytes, source_task=source)
        finally:
            source.cancel()
            with suppress(asyncio.CancelledError, DataError, ImportError):
                await source


class QuoteRecording:
    """Lazy file reader. Iteration reconstructs quote state without any API calls.

    Missing footer or a truncated final line yields an 'incomplete' terminal frame;
    malformed complete lines fail with DataError. Only one line and current quotes
    are held in memory. Inspection is independent of replay speed.
    """

    def __init__(self, path):
        self.path = Path(path)
        try:
            with self.path.open("rb") as source:
                self.metadata = json.loads(source.readline())
            header = self.metadata
            if (not isinstance(header, dict) or header.get("type") != "header"
                    or header.get("format") != FORMAT or header.get("version") != 1):
                raise ValueError("Unsupported recording format/version")
            self.started_at = _date(header["started_at"])
            if not isinstance(header.get("event"), dict) or not isinstance(header.get("excluded"), list):
                raise ValueError("Invalid event/exclusion metadata")
            markets = header["markets"]
            if not isinstance(markets, list) or not markets:
                raise ValueError("Missing markets")
            tokens = [m["token_id"] for m in markets]
            if any(not isinstance(t, str) or not t for t in tokens) or len(set(tokens)) != len(tokens):
                raise ValueError("Invalid or duplicate token IDs")
            self.markets = {m["token_id"]: m for m in markets}
        except (ValueError, KeyError, TypeError) as exc:
            raise DataError(f"Invalid recording header: {exc}") from exc

    def frames(self):
        quotes = {}
        tokens = list(self.markets)
        elapsed = 0.0
        frame_count = change_count = 0
        with self.path.open("rb") as source:
            source.readline()
            for line_no, line in enumerate(source, 2):
                if not line.endswith(b"\n"):
                    break  # Abrupt shutdown can leave an incomplete last write.
                try:
                    row = json.loads(line)
                    value = row["t"]
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise ValueError("Invalid elapsed time")
                    if not math.isfinite(value) or value < elapsed:
                        raise ValueError("Nonmonotonic elapsed time")
                    elapsed = float(value)
                    recorded_at = self.started_at + timedelta(seconds=elapsed)
                    if row["type"] == "end":
                        if (not isinstance(row.get("reason"), str) or row.get("frames") != frame_count
                                or row.get("quote_changes") != change_count):
                            raise ValueError("Invalid end marker or frame counts")
                        if source.read(1):
                            raise ValueError("Unexpected data after end marker")
                        yield QuoteFrame(elapsed, recorded_at, quotes.copy(), row["reason"])
                        return
                    if row["type"] != "sample" or not isinstance(row["q"], list) or not row["q"]:
                        raise ValueError("Expected nonempty sample")
                    changed = set()
                    for item in row["q"]:
                        index = item["i"]
                        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(tokens):
                            raise ValueError("Unknown token index")
                        if index in changed:
                            raise ValueError("Duplicate quote in sample")
                        changed.add(index)
                        def level(value):
                            if value is None:
                                return None
                            if not isinstance(value, list) or len(value) != 2:
                                raise ValueError("Invalid best quote")
                            result = Level(decimal_value(value[0], price=True), decimal_value(value[1]))
                            if not result.size:
                                raise ValueError("Best quote must have positive size")
                            return result
                        bid, ask = level(item["b"]), level(item["a"])
                        if bid and ask and bid.price > ask.price:
                            raise ValueError("Crossed best quotes")
                        if item["s"] not in STATUSES or (item.get("r") is not None and not isinstance(item["r"], str)):
                            raise ValueError("Invalid quote status/reason")
                        quotes[tokens[index]] = QuoteSnapshot(tokens[index], bid, ask, item["s"],
                                                             _date(item["u"]) if item["u"] else None,
                                                             item.get("r"))
                    if frame_count == 0 and len(quotes) != len(tokens):
                        raise ValueError("First sample must cover every token")
                    frame_count += 1
                    change_count += len(row["q"])
                    yield QuoteFrame(elapsed, recorded_at, quotes.copy())
                except (ValueError, KeyError, TypeError, OverflowError, DataError) as exc:
                    raise DataError(f"Invalid recording at line {line_no}: {exc}") from exc
        yield QuoteFrame(elapsed, self.started_at + timedelta(seconds=elapsed), quotes.copy(), "incomplete")


def load_recording(path) -> QuoteRecording:
    return QuoteRecording(path)


async def replay_orderbooks(recording, *, speed=1.0):
    """Replay sampled quote states offline, respecting elapsed gaps; speed=10 is 10x.

    For immediate analysis with no waits, use load_recording(path).frames().
    """
    _positive(speed, "speed")
    recording = recording if isinstance(recording, QuoteRecording) else load_recording(recording)
    loop = asyncio.get_running_loop()
    started = loop.time()
    for frame in recording.frames():
        delay = frame.elapsed_seconds / speed - (loop.time() - started)
        if delay > 0:
            await asyncio.sleep(delay)
        yield frame
