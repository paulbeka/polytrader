"""Command-line access to event metadata, historical prices, and order books."""

import argparse
import asyncio
from contextlib import aclosing
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from polytrader.data import DataError, PolymarketClient, discover, fetch_price_history
from polytrader.orderbook import OrderBookClient, OrderBookService, fetch_orderbooks, resolve_books


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch public Polymarket data.")
    commands = parser.add_subparsers(dest="command", required=True)
    markets = commands.add_parser("markets", help="List markets and outcome token IDs in an event")
    markets.add_argument("event", help="Polymarket event URL or event slug")
    book = commands.add_parser("orderbook", help="Fetch or watch outcome order books")
    book.add_argument("event", nargs="?", help="Event URL or slug; selects all eligible markets")
    book.add_argument("--market", action="append", help="Market slug or ID; repeat to select several")
    book.add_argument("--outcome", action="append", help="Outcome label; repeat as needed; default: all")
    book.add_argument("--token-id", action="append", help="Token ID instead of an event; repeat as needed")
    mode = book.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="Stream JSON lines until Ctrl+C")
    mode.add_argument("--serve", action="store_true", help="Serve a local dashboard and shared HTTP/SSE feed")
    mode.add_argument("--record", nargs="?", const="", metavar="FILE", help="Record compact best quotes; default file: data/orderbooks/quotes-<UTC>.jsonl")
    book.add_argument("--interval", type=float, default=1.0, help="Recording sample interval in seconds; default: 1")
    book.add_argument("--duration", type=float, default=3600.0, help="Recording duration in seconds; default: 3600")
    book.add_argument("--max-mb", type=int, default=10, help="Recording file limit in MiB; default: 10")
    book.add_argument("--port", type=int, default=8765, help="Local dashboard port; default: 8765")
    replay = commands.add_parser("replay", help="Replay saved best quotes offline")
    replay.add_argument("file", type=Path, help="Orderbook .jsonl recording")
    replay.add_argument("--speed", type=float, default=1.0, help="Playback speed multiplier; default: 1")
    playback = replay.add_mutually_exclusive_group()
    playback.add_argument("--instant", action="store_true", help="Print all reconstructed frames without waits")
    playback.add_argument("--serve", action="store_true", help="Replay in the local viewer")
    replay.add_argument("--port", type=int, default=8765, help="Local replay viewer port; default: 8765")
    history = commands.add_parser("history", help="Fetch historical prices for one outcome")
    history.add_argument("event", nargs="?", help="Polymarket event URL or event slug")
    history.add_argument("--market", help="Market slug or ID; required for events with multiple markets")
    history.add_argument("--outcome", help="Outcome label (case-insensitive); defaults to Yes")
    history.add_argument("--token-id", help="Fetch directly by token ID instead of an event")
    window = history.add_mutually_exclusive_group(required=True)
    window.add_argument("--days", type=float, help="Number of days before now, e.g. 7 or 0.5")
    window.add_argument("--start", help="Inclusive date/time; UTC if no offset is supplied")
    history.add_argument("--end", help="Exclusive date/time; defaults to now")
    history.add_argument("--bucket-seconds", type=int, help="Requested resolution, 60–86400 seconds; default: API chooses")
    history.add_argument("--output", type=Path, help="Write JSON to a new file instead of stdout")
    return parser


def run(args: argparse.Namespace, client: PolymarketClient) -> None:
    if args.command == "replay":
        from polytrader.orderbook import load_recording
        recording = load_recording(args.file)
        if args.serve:
            from polytrader.orderbook.viewer import serve_replay
            asyncio.run(serve_replay(recording, speed=args.speed, port=args.port))
        elif args.instant:
            for frame in recording.frames():
                print(json.dumps(frame.to_dict(), allow_nan=False))
        else:
            asyncio.run(_replay_books(recording, args.speed))
        return
    if args.command == "orderbook":
        book_client = OrderBookClient(timeout=client.timeout)
        options = dict(markets=args.market, outcomes=args.outcome, token_ids=args.token_id,
                       client=book_client)
        if args.record is not None:
            from polytrader.orderbook import record_orderbooks
            output = args.record or datetime.now(timezone.utc).strftime("data/orderbooks/quotes-%Y%m%dT%H%M%S.%fZ.jsonl")
            print(f"Recording best quotes to {output}", file=sys.stderr)
            result = asyncio.run(record_orderbooks(args.event, output=output, interval=args.interval,
                                                   duration=args.duration, max_bytes=args.max_mb * 1024 * 1024,
                                                   **options))
            print(f"Saved {result.quote_changes} quote changes in {result.bytes_written} bytes "
                  f"to {result.path} ({result.reason})", file=sys.stderr)
        elif args.serve:
            from polytrader.orderbook.viewer import serve_orderbooks
            asyncio.run(serve_orderbooks(args.event, port=args.port, **options))
        elif args.watch:
            collection = resolve_books(args.event, **options)
            for excluded in collection.excluded:
                print(f"Excluded {excluded['market_slug']}: {excluded['reason']}", file=sys.stderr)
            asyncio.run(_watch_books(collection, book_client))
        else:
            collection = fetch_orderbooks(args.event, **options)
            print(json.dumps(collection.to_dict(), indent=2, allow_nan=False))
            failed = [b for b in collection.books.values() if b.status == "unavailable"]
            if failed or not collection.books:
                raise DataError(f"{len(failed)} unavailable books; {len(collection.excluded)} excluded markets")
        return
    if args.command == "markets":
        event, markets = discover(client, args.event)
        print(event.get("title", args.event))
        if not markets:
            print("No markets found.")
        for market in markets:
            print(f"\n{market.question}\n  slug: {market.slug}\n  id: {market.id}")
            for outcome, token in market.tokens.items():
                print(f"  {outcome}: {token}")
            if not market.tokens:
                print("  No CLOB outcome tokens available.")
        return

    if args.output and args.output.exists():
        raise ValueError(f"Output already exists: {args.output}; choose a new filename.")
    history = fetch_price_history(
        args.event, market=args.market, outcome=args.outcome, token_id=args.token_id,
        start=args.start, end=args.end, days=args.days,
        bucket_seconds=args.bucket_seconds, client=client,
    )
    if args.output:
        history.save(args.output)
        print(f"Saved {len(history.data)} points to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(history.to_dict(), indent=2, allow_nan=False))
    if not history.data:
        print("No prices returned. Check the market's lifetime and requested resolution; older fine-grained data may be unavailable.", file=sys.stderr)


async def _watch_books(collection, client):
    async with OrderBookService(collection, client=client) as service:
        async with aclosing(service.updates()) as updates:
            async for update in updates:
                print(json.dumps(update.to_dict(), allow_nan=False), flush=True)


async def _replay_books(recording, speed):
    from polytrader.orderbook import replay_orderbooks
    async with aclosing(replay_orderbooks(recording, speed=speed)) as frames:
        async for frame in frames:
            print(json.dumps(frame.to_dict(), allow_nan=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args, PolymarketClient())
    except (DataError, ValueError, OSError, ImportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
