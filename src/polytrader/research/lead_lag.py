"""Historical inputs for lead/lag research; no orders or invented quotes.

Trade pages are cached before advancing the cursor. Re-running the same window
reuses completed pages; a page-limit failure never returns a partial dataset.
"""

import hashlib
import json
import math
from pathlib import Path
import re
import time

import pandas as pd

from polytrader.data.client import DataError, PolymarketClient
from polytrader.data.history import fetch_history

TRADES_URL = "https://data-api.polymarket.com/v2/trades"
TRADE_FIELDS = ("timestamp", "condition_id", "token_id", "side", "price", "size")


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def fetch_trade_window(condition_id, start, end, *, cache_dir, client=None,
                       max_pages=20000, pause_s=0.5):
    """Fetch taker fills in [start, end), preserving identical legitimate fills.

The market feed ignores start/end: walk newest-first cursors until the oldest
returned second precedes start, then filter locally. A fixed end makes cached
requests reproducible. Use only one writer per cache directory.
"""
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition_id):
        raise ValueError("condition_id must be a 0x-prefixed 32-byte ID")
    if not 0 <= start < end or max_pages < 1:
        raise ValueError("Require 0 <= start < end and max_pages >= 1")
    api = client or PolymarketClient(timeout=30)
    params = dict(condition=condition_id, limit=1000, taker_only="true",
                  filter_type="TOKENS", filter_amount="0.000000000001")
    key = hashlib.sha256(json.dumps([params, start, end], sort_keys=True).encode()).hexdigest()
    folder = Path(cache_dir) / "trades" / key
    rows, seen = [], set()
    cursor, previous = None, None
    for index in range(max_pages):
        path = folder / f"{index:06d}.json"
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached["cursor"] != cursor:
                raise DataError("Cached cursor mismatch; use a fresh cache directory")
            payload = cached["response"]
        else:
            time.sleep(pause_s)
            payload = api.get_json(TRADES_URL, **params, cursor=cursor)
        data, pagination = payload.get("data"), payload.get("pagination")
        if not isinstance(data, list) or not isinstance(pagination, dict):
            raise DataError("Trade response missing data or pagination")
        more = pagination.get("has_more")
        next_cursor = pagination.get("next_cursor")
        if type(more) is not bool or (more and (
                not data or not isinstance(next_cursor, str) or not next_cursor
                or next_cursor == cursor or next_cursor in seen)):
            raise DataError("Trade pagination missing or repeated; refusing partial data")
        clean = []
        for row in data:
            if not isinstance(row, dict) or type(row.get("timestamp")) is not int:
                raise DataError("Invalid trade timestamp")
            ts = row["timestamp"]
            if previous is not None and ts > previous:
                raise DataError("Trade feed is not newest-first")
            previous = ts
            if (row.get("condition_id") != condition_id
                    or not str(row.get("token_id", "")).isdecimal()
                    or row.get("side") not in {"BUY", "SELL"}):
                raise DataError("Trade has wrong condition/token/side")
            for field in ("price", "size"):
                if type(row.get(field)) not in (int, float) or not math.isfinite(row[field]):
                    raise DataError("Invalid trade price/size")
            if not 0 <= row["price"] <= 1 or row["size"] <= 0:
                raise DataError("Invalid trade price/size")
            item = {field: row[field] for field in TRADE_FIELDS}
            clean.append(item)
            if start <= ts < end:
                rows.append(item)
        # Retain only analysis fields, not wallet/profile details.
        if not path.exists():
            _save(path, {"cursor": cursor, "response": {"data": clean, "pagination": pagination}})
        if not more or (data and data[-1]["timestamp"] < start):
            return pd.DataFrame(rows, columns=TRADE_FIELDS)
        seen.add(next_cursor)
        cursor = next_cursor
    raise DataError(f"Reached {max_pages} trade pages; incomplete cache retained. Increase max_pages and rerun.")


def load_historical_inputs(token_conditions, start, end, *, bucket_seconds=60,
                           cache_dir, client=None, max_pages=20000, log=print):
    """Return separate price observations and trade fills for selected tokens.

No interpolation, quote substitution or zero-volume claims are made here.
Resolution-zero settlement points are excluded from the research price table.
"""
    if bucket_seconds < 60:
        raise ValueError("Historical price buckets must be at least 60 seconds")
    api = client or PolymarketClient(timeout=30)
    prices, trades = [], []
    for token, condition in token_conditions.items():
        signature = json.dumps([str(token), start, end, bucket_seconds])
        key = hashlib.sha256(signature.encode()).hexdigest()
        path = Path(cache_dir) / "prices" / f"{key}.json"
        log(f"Prices: {token}")
        if path.exists():
            points = json.loads(path.read_text(encoding="utf-8"))
        else:
            points = fetch_history(api, str(token), start, end, bucket_seconds)
            _save(path, points)
        usable = [dict(point, token_id=str(token)) for point in points
                  if start <= point["timestamp"] < end and point["resolution_seconds"] > 0]
        if not usable:
            raise DataError(f"No price coverage for token {token}. Choose a recent window or coarser bucket; no data was fabricated.")
        prices.extend(usable)
    for condition in dict.fromkeys(token_conditions.values()):
        log(f"Trades: {condition} (cached pages resume automatically)")
        frame = fetch_trade_window(condition, start, end, cache_dir=cache_dir,
                                   client=api, max_pages=max_pages)
        wanted = {str(t) for t, c in token_conditions.items() if c == condition}
        trades.append(frame[frame.token_id.isin(wanted)])
    price_frame = pd.DataFrame(prices).sort_values(["token_id", "timestamp"])
    trade_frame = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame(columns=TRADE_FIELDS)
    for frame in (price_frame, trade_frame):
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="s", utc=True)
    trade_frame["notional_usdc"] = trade_frame["price"] * trade_frame["size"]
    return price_frame, trade_frame


def historical_panels(prices, tokens, grid="60s"):
    """Use only exact grid observations. Missing bars remain missing.

No forward filling: a last trade from an inactive market is not a current quote.
"""
    if pd.Timedelta(grid).total_seconds() < prices.resolution_seconds.max():
        raise ValueError("Grid is finer than returned price resolution; increase GRID")
    start = prices.timestamp.min().ceil(grid)
    end = prices.timestamp.max().floor(grid)
    index = pd.date_range(start, end, freq=grid, tz="UTC")
    result = {}
    for token in tokens:
        g = prices[prices.token_id.eq(str(token))].set_index("timestamp")
        if g.index.duplicated().any():
            raise DataError("Multiple price resolutions at one timestamp; select a single resolution")
        result[str(token)] = g[["price", "resolution_seconds"]].reindex(index)
    return result
