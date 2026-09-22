"""Command-line access to event metadata and historical prices."""

import argparse
import json
from pathlib import Path
import sys

from polytrader.data import DataError, PolymarketClient, discover, fetch_price_history


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch public Polymarket data.")
    commands = parser.add_subparsers(dest="command", required=True)
    markets = commands.add_parser("markets", help="List markets and outcome token IDs in an event")
    markets.add_argument("event", help="Polymarket event URL or event slug")
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args, PolymarketClient())
    except (DataError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0
