"""Bulk research dataset: resolved markets, their full trade history, volume bars.

Layout under ``out_dir`` (default ``data/research``, git-ignored):

    markets.parquet             one row per resolved market (outcome, tags, dates, volume)
    trades/<condition_id>.parquet   every taker trade of that market
    failures.jsonl              markets whose trades could not be fetched; rerun to retry
    volume_<freq>.parquet       per-market volume bars built from the trade files

Every step is resumable: existing trade files are skipped, and each file is
written atomically, so an interrupted pull never leaves a partial market.
Requires the sandbox extra (pandas + pyarrow).
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import time
from urllib.error import HTTPError, URLError

import pandas as pd

from polytrader.data.client import GAMMA_URL, DataError, PolymarketClient
from polytrader.data.discovery import parse_market
from polytrader.data.trades import fetch_trades

EVENTS_URL = f"{GAMMA_URL}/events/keyset"
TRADE_COLUMNS = {"timestamp": "int64", "asset": "string", "outcome_index": "int8", "side": "string",
                 "size": "float64", "price": "float64", "wallet": "string", "tx": "string"}


def _retrying(call, *, attempts=6):
    """Retry rate limits, server errors and network failures with exponential backoff."""
    for attempt in range(attempts):
        try:
            return call()
        except DataError as exc:
            cause = exc.__cause__
            transient = (isinstance(cause, HTTPError) and (cause.code == 429 or cause.code >= 500)
                         or isinstance(cause, (URLError, TimeoutError, ConnectionError)))
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(min(60, 2 ** attempt) + random.random())


def _json_list(value):
    return json.loads(value) if isinstance(value, str) else (value or [])


def _market_row(event: dict, raw: dict) -> dict | None:
    try:
        market = parse_market(raw)
        prices = [float(p) for p in _json_list(raw.get("outcomePrices"))]
    except (DataError, ValueError, TypeError):
        return None
    if not market.tokens or len(prices) != len(market.tokens):
        return None
    labels = list(market.tokens)
    winners = [label for label, price in zip(labels, prices) if price == 1]
    tokens = {label.casefold(): token for label, token in market.tokens.items()}
    return {
        "market_id": market.id, "condition_id": raw.get("conditionId"), "slug": market.slug,
        "question": market.question, "group_item": raw.get("groupItemTitle"),
        "event_id": str(event.get("id")), "event_slug": event.get("slug"), "event_title": event.get("title"),
        "tags": [t.get("label") for t in event.get("tags") or [] if t.get("label")],
        "outcomes": labels, "tokens": list(market.tokens.values()), "outcome_prices": prices,
        # Recurring crypto markets use Up/Down; Up plays the YES role.
        "yes_token": tokens.get("yes", tokens.get("up")), "no_token": tokens.get("no", tokens.get("down")),
        "resolved_outcome": winners[0] if len(winners) == 1 else None,
        "volume_shares": float(raw.get("volumeNum") or 0),
        "created_at": raw.get("createdAt"), "start_date": raw.get("startDate"),
        "end_date": raw.get("endDate"), "closed_time": raw.get("closedTime"),
        "neg_risk": bool(raw.get("negRisk")), "uma_status": raw.get("umaResolutionStatus"),
        "is_updown": "up or down" in market.question.casefold(),
    }


def pull_markets(out_dir="data/research", *, since="2025-01-01", min_volume=10_000,
                 client=None, log=print) -> pd.DataFrame:
    """List resolved markets closed on/after ``since`` with at least ``min_volume`` shares traded."""
    client = client or PolymarketClient(timeout=60)
    rows, cursor, pages = {}, None, 0
    while True:
        page = _retrying(lambda: client.get_json(
            EVENTS_URL, closed="true", limit=100, end_date_min=since,
            volume_min=min_volume, after_cursor=cursor))
        for event in page.get("events") or []:
            for raw in event.get("markets") or []:
                row = _market_row(event, raw)
                if row and row["condition_id"] and row["volume_shares"] >= min_volume:
                    rows[row["condition_id"]] = row
        pages += 1
        if pages % 20 == 0:
            log(f"  {pages} event pages, {len(rows):,} markets so far")
        cursor = page.get("next_cursor")
        if not cursor or not page.get("events"):
            break
    frame = pd.DataFrame(list(rows.values()))
    if frame.empty:
        raise DataError("No markets matched")
    for column in ("created_at", "start_date", "end_date", "closed_time"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce", format="mixed")
    # The API filters on end date only; also require the actual close to be in range.
    frame = frame[frame["closed_time"] >= pd.Timestamp(since, tz="UTC")].reset_index(drop=True)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _atomic_parquet(frame, out / "markets.parquet")
    log(f"Saved {len(frame):,} markets to {out / 'markets.parquet'}")
    return frame


def _atomic_parquet(frame: pd.DataFrame, path: Path) -> None:
    temporary = path.with_name(path.name + ".tmp")
    frame.to_parquet(temporary, index=False)
    os.replace(temporary, path)


def _trade_frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame({
        "timestamp": [r["timestamp"] for r in rows],
        "asset": [r.get("asset") for r in rows],
        "outcome_index": [r.get("outcomeIndex", -1) for r in rows],
        "side": [r.get("side") for r in rows],
        "size": [r.get("size") for r in rows],
        "price": [r.get("price") for r in rows],
        "wallet": [r.get("proxyWallet") for r in rows],
        "tx": [r.get("transactionHash") for r in rows],
    }, columns=list(TRADE_COLUMNS))
    return frame.astype(TRADE_COLUMNS).sort_values("timestamp", kind="stable").reset_index(drop=True)


def pull_trades(out_dir="data/research", *, workers=4, limit=None, sample=None, seed=0,
                include_updown=True, client=None, log=print) -> dict:
    """Download every selected market's trades; already-saved markets are skipped."""
    out = Path(out_dir)
    markets = pd.read_parquet(out / "markets.parquet")
    if not include_updown:
        markets = markets[~markets["is_updown"]]
    ids = list(markets["condition_id"])
    if sample:
        ids = random.Random(seed).sample(ids, min(sample, len(ids)))
    if limit:
        ids = ids[:limit]
    folder = out / "trades"
    folder.mkdir(parents=True, exist_ok=True)
    todo = [c for c in ids if not (folder / f"{c}.parquet").exists()]
    log(f"{len(ids):,} markets selected, {len(ids) - len(todo):,} already saved, {len(todo):,} to fetch "
        f"with {workers} workers")
    client = client or PolymarketClient(timeout=60)

    def job(condition_id):
        rows = _retrying(lambda: fetch_trades(client, condition_id))
        _atomic_parquet(_trade_frame(rows), folder / f"{condition_id}.parquet")
        return len(rows)

    done = failed = trades = 0
    started = time.monotonic()
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {executor.submit(job, c): c for c in todo}
        for future in as_completed(futures):
            try:
                trades += future.result()
                done += 1
            except (DataError, OSError, ValueError) as exc:
                failed += 1
                with open(out / "failures.jsonl", "a", encoding="utf-8") as sink:
                    sink.write(json.dumps({"condition_id": futures[future], "error": str(exc),
                                           "at": datetime.now(timezone.utc).isoformat()}) + "\n")
            finished = done + failed
            if finished % 25 == 0 or finished == len(todo):
                elapsed = time.monotonic() - started
                eta = elapsed / finished * (len(todo) - finished)
                log(f"  {finished:,}/{len(todo):,} markets, {trades:,} trades, {failed} failed, "
                    f"{elapsed / 60:.1f} min elapsed, ~{eta / 60:.1f} min left")
    finally:
        # On Ctrl+C, drop queued markets; saved files remain and are skipped next run.
        executor.shutdown(wait=True, cancel_futures=True)
    return {"fetched": done, "failed": failed, "trades": trades, "skipped": len(ids) - len(todo)}


def load_trades(out_dir="data/research", condition_ids=None) -> pd.DataFrame:
    """Load saved trades (optionally only some markets) with a condition_id column."""
    folder = Path(out_dir) / "trades"
    paths = ([folder / f"{c}.parquet" for c in condition_ids] if condition_ids is not None
             else sorted(folder.glob("*.parquet")))
    frames = [pd.read_parquet(p).assign(condition_id=p.stem) for p in paths if p.exists()]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=[*TRADE_COLUMNS, "condition_id"])


def volume_bars(trades: pd.DataFrame, yes_token: str | None, freq="1h") -> pd.DataFrame:
    """Aggregate one market's trades into bars.

    ``shares``/``usd`` are traded volume; ``yes_vwap`` is the size-weighted price
    expressed as the YES probability (NO fills at p count as YES at 1-p), and
    ``net_yes_shares`` is taker flow towards YES (buy YES / sell NO) minus away.
    Only bars containing trades are returned; no gaps are filled.
    """
    if trades.empty:
        return pd.DataFrame(columns=["time", "trades", "shares", "usd", "yes_vwap", "net_yes_shares"])
    is_yes = trades["asset"] == yes_token
    yes_price = trades["price"].where(is_yes, 1 - trades["price"])
    towards_yes = (trades["side"] == "BUY") == is_yes
    frame = pd.DataFrame({
        "time": pd.to_datetime(trades["timestamp"], unit="s", utc=True).dt.floor(freq),
        "shares": trades["size"], "usd": trades["size"] * trades["price"],
        "weighted": trades["size"] * yes_price,
        "net": trades["size"].where(towards_yes, -trades["size"]),
    })
    bars = frame.groupby("time").agg(trades=("shares", "size"), shares=("shares", "sum"),
                                     usd=("usd", "sum"), weighted=("weighted", "sum"),
                                     net_yes_shares=("net", "sum")).reset_index()
    bars["yes_vwap"] = bars.pop("weighted") / bars["shares"]
    if yes_token is None:
        bars[["yes_vwap", "net_yes_shares"]] = float("nan")  # Not a Yes/No market.
    return bars


def build_volume_bars(out_dir="data/research", freq="1h", log=print) -> pd.DataFrame:
    """Build bars for every saved trade file and save them as volume_<freq>.parquet."""
    out = Path(out_dir)
    markets = pd.read_parquet(out / "markets.parquet", columns=["condition_id", "yes_token"])
    yes = dict(zip(markets["condition_id"], markets["yes_token"]))
    parts = []
    for path in sorted((out / "trades").glob("*.parquet")):
        bars = volume_bars(pd.read_parquet(path), yes.get(path.stem), freq)
        if not bars.empty:
            parts.append(bars.assign(condition_id=path.stem))
    result = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    _atomic_parquet(result, out / f"volume_{freq}.parquet")
    log(f"Saved {len(result):,} bars for {len(parts):,} markets to {out / f'volume_{freq}.parquet'}")
    return result
