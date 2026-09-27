"""Run with: python -m polytrader.research {markets,trades,bars} [options]."""

import argparse

from polytrader.research.pull import build_volume_bars, pull_markets, pull_trades


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the resolved-market research dataset.")
    parser.add_argument("--out", default="data/research", help="Dataset folder; default: data/research")
    steps = parser.add_subparsers(dest="step", required=True)
    markets = steps.add_parser("markets", help="List resolved markets (fast; rerun to refresh)")
    markets.add_argument("--since", default="2025-01-01", help="Earliest close date; default: 2025-01-01")
    markets.add_argument("--min-volume", type=float, default=10_000, help="Minimum shares traded; default: 10000")
    trades = steps.add_parser("trades", help="Download trades; resumable, skips saved markets")
    trades.add_argument("--workers", type=int, default=4, help="Parallel requests; default: 4")
    trades.add_argument("--limit", type=int, help="Only the first N selected markets")
    trades.add_argument("--sample", type=int, help="A random sample of N markets (reproducible with --seed)")
    trades.add_argument("--seed", type=int, default=0)
    trades.add_argument("--no-updown", action="store_true", help="Skip recurring 'Up or Down' crypto markets")
    bars = steps.add_parser("bars", help="Aggregate saved trades into volume bars")
    bars.add_argument("--freq", default="1h", help="pandas frequency, e.g. 15min, 1h, 1D; default: 1h")
    args = parser.parse_args(argv)
    if args.step == "markets":
        pull_markets(args.out, since=args.since, min_volume=args.min_volume)
    elif args.step == "trades":
        print(pull_trades(args.out, workers=args.workers, limit=args.limit, sample=args.sample,
                          seed=args.seed, include_updown=not args.no_updown))
    else:
        build_volume_bars(args.out, args.freq)


if __name__ == "__main__":
    main()
