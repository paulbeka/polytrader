"""Exclusive session directories and durable, replayable observations."""

from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import uuid

from .feed import utc_now


def json_default(value):
    if isinstance(value, (Decimal, Path)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Cannot serialize {type(value)}")


def dumps(value):
    return json.dumps(value, default=json_default, allow_nan=False)


class Session:
    def __init__(self, root, config, families, collection):
        self.directory = Path(root) / (utc_now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        self.directory.mkdir(parents=True, exist_ok=False)
        (self.directory / "metadata.json").write_text(dumps({
            "version": 1, "strategy_version": 2, "config": config, "families": families,
            "markets": {t: r.to_dict() for t, r in collection.markets.items()},
            "excluded": collection.excluded, "hypothetical": True, "fees": "0",
            "trade_scope": "leader YES token only; feed-reported BUY/SELL direction",
            "trade_source": "https://docs.polymarket.com/market-data/realtime-data",
        }), encoding="utf-8")
        self.events = (self.directory / "events.jsonl").open("x", encoding="utf-8", buffering=1)
        try:
            self.inputs = (self.directory / "inputs.jsonl").open("x", encoding="utf-8", buffering=1)
        except BaseException:
            self.events.close()
            raise

    def emit(self, row):
        self.events.write(dumps(row) + "\n")
        kind = row["type"]
        if kind in {"signal", "entry", "exit", "entry_rejected", "signals_paused"}:
            detail = f" pnl={row['pnl']}" if "pnl" in row else ""
            print(f"[{row['utc']}] {kind} {row.get('follower', '')} {row.get('direction', '')}"
                  f"{detail} {row.get('reason', '')}", flush=True)

    def input(self, row):
        self.inputs.write(dumps(row) + "\n")

    def finish(self, summary):
        temporary = self.directory / "summary.tmp"
        temporary.write_text(dumps(summary), encoding="utf-8")
        temporary.replace(self.directory / "summary.json")

    def close(self):
        self.events.close()
        self.inputs.close()
