"""python -m polytrader.bot.lead_follower --help"""

import argparse
import asyncio
from dataclasses import fields
import math
from pathlib import Path
import sys

from polytrader.data import DataError
from .config import Config, Group, Settings, load_config
from .discovery import prepare
from .replay import replay
from .reporting import dumps
from .runner import describe, run


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("duration must be finite and positive")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lead/follower paper trading; slippage included, fees excluded")
    parser.add_argument("event", nargs="?", help="Related deadline event URL or slug")
    parser.add_argument("--leader", help="Explicit leader market slug or ID")
    parser.add_argument("--follower", action="append", help="Follower slug/ID; repeat; default: other YES/NO markets")
    parser.add_argument("--config", type=Path, help="TOML with settings and multiple [[groups]]")
    parser.add_argument("--output", type=Path, help="Session root (quick mode default: data/lead_follower)")
    parser.add_argument("--duration", type=positive_seconds, help="Run seconds; omit to stop with Ctrl+C")
    parser.add_argument("--validate", action="store_true", help="Resolve and print selection without streaming")
    parser.add_argument("--replay", type=Path, help="Offline replay of a session directory")
    for f in fields(Settings):
        parser.add_argument("--" + f.name.replace("_", "-"), dest=f.name,
                            type=(int if f.name in {"min_trades", "candidate_min_trades"} else
                                  float if isinstance(f.default, (float, int)) else str),
                            help=("Optional aligned price confirmation; 0 disables (default)."
                                  if f.name == "min_move_pp" else f"Default: {f.default}"))
    args = parser.parse_args(argv)
    try:
        overrides = {f.name: getattr(args, f.name) for f in fields(Settings) if getattr(args, f.name) is not None}
        if args.replay:
            if args.event or args.config or args.leader or args.follower or overrides or args.validate or args.duration or args.output:
                raise ValueError("--replay must be used alone")
            print(dumps(replay(args.replay)))
            return 0
        if args.config:
            if args.event or args.leader or args.follower or overrides or args.output:
                raise ValueError("With --config, put selection/settings/output_dir in the TOML file")
            config = load_config(args.config)
        else:
            if not args.event or not args.leader:
                raise ValueError("Provide event and --leader, or --config")
            config = Config((Group("default", args.event, args.leader, tuple(args.follower or [])),),
                            Settings(**overrides), args.output or Path("data/lead_follower"))
        families, collection = prepare(config)
        describe(families)
        if args.validate:
            return 0
    except (ValueError, OSError, DataError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        asyncio.run(run(config, families, collection, duration=args.duration))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Lead/follower runtime error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
