"""Replay recorded normalized inputs without network access or sleeping."""

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

from polytrader.orderbook.models import BookSnapshot, Level
from .config import Settings
from .discovery import Family, Market
from .engine import Engine


def read_book(raw):
    def dt(v):
        return datetime.fromisoformat(v) if v else None
    def levels(side):
        return tuple(Level(Decimal(v["price"]), Decimal(v["size"])) for v in raw[side])
    return BookSnapshot(raw["token_id"], levels("bids"), levels("asks"),
                        dt(raw["updated_at"]), dt(raw["received_at"]), raw["status"], raw["reason"])


def replay(directory, emit=lambda row: None):
    directory = Path(directory)
    meta = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    if meta.get("version") != 1:
        raise ValueError("Unsupported session version")
    families = tuple(Family(f["id"], f["event"], Market(**f["leader"]),
                            tuple(Market(**m) for m in f["followers"])) for f in meta["families"])
    strategy_version = meta.get("strategy_version", 1)
    if strategy_version == 1:
        from .legacy_price_engine import Engine as LegacyEngine
        # v1 metadata stored the complete settings, with monetary values as strings.
        settings = SimpleNamespace(**{k: Decimal(v) if isinstance(v, str) else v
                                      for k, v in meta["config"]["settings"].items()})
        engine = LegacyEngine(families, settings, emit)
    elif strategy_version == 2:
        engine = Engine(families, Settings(**meta["config"]["settings"]), emit)
    else:
        raise ValueError(f"Unsupported strategy version: {strategy_version}")
    books, now, utc, healthy = {}, 0, None, False
    # Complete JSON lines only. A truncated file fails visibly rather than quietly
    # presenting an incomplete session as a completed experiment.
    with (directory / "inputs.jsonl").open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            books.update({t: read_book(b) for t, b in row["books"].items()})
            trade = row["trade"]
            if trade:
                for name in ("price", "size", "signed_size"):
                    trade[name] = Decimal(trade[name])
            now, utc, healthy = row["elapsed_seconds"], row["utc"], row["healthy"]
            engine.observe(now, utc, books, trade=trade, healthy=healthy, reason=row["reason"])
    engine.finish(now, utc)
    return engine.summary(books, healthy=healthy)
