"""Command-line access to event metadata and historical prices."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from polytrader.data.client import DataError, HISTORY_URL, PolymarketClient
from polytrader.data.discovery import discover, select_market, select_token
from polytrader.data.history import UTC, fetch_history, resolve_window


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

    if bool(args.event) == bool(args.token_id):
        raise ValueError("Provide an event URL/slug OR --token-id.")
    if args.token_id and (args.market is not None or args.outcome is not None):
        raise ValueError("--market and --outcome apply only when an event is provided.")
    start, end = resolve_window(start=args.start, end=args.end, days=args.days)
    if args.bucket_seconds is not None and not 60 <= args.bucket_seconds <= 86400:
        raise ValueError("--bucket-seconds must be between 60 and 86400.")
    if args.output and args.output.exists():
        raise ValueError(f"Output already exists: {args.output}; choose a new filename.")
    metadata = {}
    token_id = args.token_id
    if args.event:
        event, markets = discover(client, args.event)
        market = select_market(markets, args.market)
        label, token_id = select_token(market, args.outcome or "Yes")
        metadata = {
            "event": {key: event.get(key) for key in ("id", "slug", "title")},
            "market": market.raw, "outcome": label,
        }
    points = fetch_history(client, token_id, start, end, args.bucket_seconds)
    result = {
        "source": HISTORY_URL,
        "fetched_at": datetime.now(UTC).isoformat(),
        "token_id": token_id,
        "window": {
            "start": datetime.fromtimestamp(start, UTC).isoformat(),
            "end": datetime.fromtimestamp(end, UTC).isoformat(),
            "start_inclusive": True, "end_exclusive": True,
        },
        "requested_bucket_seconds": args.bucket_seconds,
        **metadata,
        "data": points,
    }
    encoded = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as destination:
            destination.write(encoded)
        print(f"Saved {len(points)} points to {args.output}", file=sys.stderr)
    else:
        print(encoded, end="")
    if not points:
        print("No prices returned. Check the market's lifetime and requested resolution; older fine-grained data may be unavailable.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args, PolymarketClient())
    except (DataError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0
