"""Run with python -m polytrader.bot.time_arbitrage."""

import argparse
import asyncio
import math
import sys
from polytrader.ops.runtime import until_stopped

from polytrader.data import DataError
from .config import load_config
from .runner import describe, prepare, run


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("duration must be finite and positive")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only, fee-aware ordered-market time-arbitrage scanner")
    parser.add_argument("--config", required=True, help="TOML file; relative output paths use its directory")
    parser.add_argument("--validate", action="store_true", help="Resolve metadata and print all pairs without a stream")
    parser.add_argument("--duration", type=positive_seconds, help="Stop live scanning after this many seconds")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        universe, metadata, errors = prepare(config)
        describe(config, universe, metadata)
        for key, error in errors.items():
            print(f"Metadata fetch failed for {key}: {error}", file=sys.stderr)
    except (ValueError, OSError, DataError) as exc:
        print(f"Configuration/discovery error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    if args.validate:
        print("Validation does not prove the chain's logical implication. No stream opened.")
        return 0 if all(m.supported for m in metadata.values()) else 2
    try:
        directory = asyncio.run(until_stopped(run(config, universe, metadata, duration=args.duration)))
        print(f"Session saved: {directory}")
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Scanner runtime/persistence error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
