"""Command-line access to event metadata, historical prices, and order books."""

import argparse
import asyncio
from contextlib import aclosing
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
    book.add_argument("--watch", action="store_true", help="Stream JSON lines until Ctrl+C")
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
    if args.command == "orderbook":
        book_client = OrderBookClient(timeout=client.timeout)
        options = dict(markets=args.market, outcomes=args.outcome, token_ids=args.token_id,
                       client=book_client)
        if args.watch:
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
