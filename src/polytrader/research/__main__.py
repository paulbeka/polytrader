"""CLI for the resumable research dataset pipeline."""

import argparse

from polytrader.research.pull import (
    DEFAULT_OUT, DEFAULT_SEED, MAX_MARKETS, compact_dataset, enumerate_universe,
    pull_prices, pull_trades, validate_dataset,
)


def _network_options(parser):
    parser.add_argument("--workers", type=int, default=4, help="Concurrent markets; default: 4")
    parser.add_argument("--rps", type=float, default=4.0, help="Global requests/second; default: 4")
    parser.add_argument("--limit", type=int, help="Only process the first N selected markets")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the one-year resolved-market research dataset.")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"Dataset directory; default: {DEFAULT_OUT}")
    steps = parser.add_subparsers(dest="step", required=True)

    universe = steps.add_parser("universe", aliases=["markets"],
                                help="Enumerate all eligible markets and select the sample")
    universe.add_argument("--days", type=int, default=365, help="Close-time lookback; default: 365")
    universe.add_argument("--cap", type=int, default=MAX_MARKETS, help="Maximum selected markets")
    universe.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Sampling seed")
    universe.add_argument("--rps", type=float, default=4.0, help="Global requests/second; default: 4")
    universe.add_argument("--retry-attempts", type=int, default=20,
                          help="Retries for transient Gamma errors; default: 20")
    universe.add_argument("--limit", type=int,
                          help="Smoke-test selection size; enumeration still covers the full universe")

    trades = steps.add_parser("trades", help="Download complete taker-only trade histories")
    _network_options(trades)
    prices = steps.add_parser("prices", help="Download outcome price histories with resolution fallback")
    _network_options(prices)

    compact = steps.add_parser("compact", aliases=["bars"],
                               help="Build public tables and 1min/1h/1d trade bars")
    compact.add_argument("--limit", type=int, help="Only compact the first N selected markets")
    validate = steps.add_parser("validate", help="Report dataset quality checks without aborting")
    validate.add_argument("--limit", type=int, help="Only validate the first N selected markets")

    args = parser.parse_args(argv)
    if args.step in {"universe", "markets"}:
        enumerate_universe(args.out, days=args.days, cap=args.cap, seed=args.seed,
                           limit=args.limit, rps=args.rps, retry_attempts=args.retry_attempts)
    elif args.step == "trades":
        print(pull_trades(args.out, workers=args.workers, limit=args.limit, rps=args.rps))
    elif args.step == "prices":
        print(pull_prices(args.out, workers=args.workers, limit=args.limit, rps=args.rps))
    elif args.step in {"compact", "bars"}:
        compact_dataset(args.out, limit=args.limit)
    else:
        validate_dataset(args.out, limit=args.limit)


if __name__ == "__main__":
    main()
