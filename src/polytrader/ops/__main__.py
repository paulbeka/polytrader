"""python -m polytrader.ops --help"""

import argparse
from datetime import date
import os
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Polytrader research operations")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="Index sessions and generate reports continuously")
    collect.add_argument("--root", type=Path, default=Path(os.environ.get("POLYTRADER_DATA", "data")))
    collect.add_argument("--database", type=Path)
    collect.add_argument("--once", action="store_true")
    collect.add_argument("--interval", type=float, default=5)
    collect.add_argument("--timezone", default="Europe/London")
    worker = sub.add_parser("worker", help="Run an allowlisted strategy instance")
    worker.add_argument("strategy", choices=["lead_follower", "time_arbitrage"])
    worker.add_argument("--config", type=Path, required=True)
    worker.add_argument("--output", type=Path, default=Path("/data"))
    worker.add_argument("--validate", action="store_true")
    worker.add_argument("--offline", action="store_true", help="Only validate TOML, no public discovery")
    for name in ("backup", "retain"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--root", type=Path, default=Path("data"))
        if name == "backup":
            cmd.add_argument("--destination", type=Path, required=True)
            cmd.add_argument("--bucket", default=os.environ.get("POLYTRADER_BACKUP_BUCKET"))
    restore = sub.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("destination", type=Path)
    health = sub.add_parser("health")
    health.add_argument("--root", type=Path, default=Path("/data"))
    health.add_argument("--collector", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "collect":
        if not 0 < args.interval <= 30:
            parser.error("--interval must be between 0 and 30 seconds")
        from .service import run_collector
        run_collector(args.root, args.database or args.root / "ops" / "index.sqlite3", once=args.once,
                      interval=args.interval, timezone_name=args.timezone)
    elif args.command == "worker":
        from .worker import worker
        worker(args.strategy, args.config, args.output, validate=args.validate, offline=args.offline)
    elif args.command == "backup":
        from .backup import backup
        print(backup(args.root, args.root / "ops" / "index.sqlite3", args.destination, bucket=args.bucket))
    elif args.command == "restore":
        from .backup import restore
        print(restore(args.archive, args.destination))
    elif args.command == "retain":
        from .backup import retain
        print("Expired files:", len(retain(args.root)))
    elif args.command == "health":
        from datetime import datetime, timezone
        from .collector import read_json
        paths = [args.root / "ops" / "collector_status.json"] if args.collector else sorted(args.root.glob("*/status.json"), reverse=True)[:1]
        if not paths:
            return 1
        status = read_json(paths[0], {})
        stamp = status.get("heartbeat_at")
        # Health checks intentionally ignore source timestamps and market inactivity.
        return int(not stamp or status.get("process_state") == "stopped" or
                   (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() > 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
