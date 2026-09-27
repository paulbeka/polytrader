"""Build the versioned resolved-market research dataset.

Download stages write one atomic file per market under ``.staging``. The
``compact`` stage creates the documented Hive-style Parquet layout. Network
access always goes through :class:`PolymarketClient`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import random

import pandas as pd

from polytrader.data.client import GAMMA_URL, DataError, PolymarketClient
from polytrader.data.discovery import parse_market
from polytrader.data.history import fetch_history
from polytrader.data.trades import fetch_trades
from polytrader.research.storage import (
    LimitedClient, Progress, RateLimiter, append_failure, atomic_json,
    atomic_jsonl_zstd, atomic_parquet, retrying, update_manifest,
)

UTC = timezone.utc
EVENTS_URL = f"{GAMMA_URL}/events/keyset"
DEFAULT_OUT = "data/research/v1"
MAX_MARKETS = 50_000
DEFAULT_SEED = 1729
PRICE_BUCKETS = (60, 300, 3600, 86400, None)
TRADE_COLUMNS = {
    "condition_id": "string", "timestamp": "int64", "asset": "string",
    "outcome_index": "int16", "side": "string", "size": "float64",
    "price": "float64", "wallet": "string", "tx": "string",
}
PRICE_COLUMNS = {
    "condition_id": "string", "token_id": "string", "outcome": "string",
    "timestamp": "int64", "price": "float64", "resolution_seconds": "int64",
    "requested_bucket_seconds": "Int64",
}


def _retrying(call, *, attempts=6):
    """Backward-compatible name for the shared retry helper."""
    return retrying(call, attempts=attempts)


def _json_list(value):
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, list) else []


def _as_list(value):
    """Normalize Parquet list scalars (often numpy arrays) back to lists."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if hasattr(value, "tolist"):
        converted = value.tolist()
        return converted if isinstance(converted, list) else [converted]
    return list(value) if isinstance(value, tuple) else []


def _float(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _raw_columns(raw: dict, prefix: str) -> dict:
    """Preserve every API field in a Parquet-safe prefixed column."""
    result = {}
    for key, value in raw.items():
        name = f"{prefix}{key}"
        if isinstance(value, (dict, list)):
            result[name] = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        elif value is None or isinstance(value, (str, int, float, bool)):
            result[name] = value
        else:
            result[name] = str(value)
    return result


def _tag_labels(event: dict) -> list[str]:
    labels = []
    for tag in event.get("tags") or []:
        label = (tag.get("label") or tag.get("slug")) if isinstance(tag, dict) else str(tag)
        if label and label not in labels:
            labels.append(label)
    return labels


def _event_row(event: dict) -> dict:
    tags = _tag_labels(event)
    row = {
        "event_id": str(event.get("id") or ""), "event_slug": event.get("slug"),
        "event_title": event.get("title"), "event_description": event.get("description"),
        "event_start_date": event.get("startDate"), "event_end_date": event.get("endDate"),
        "event_closed_time": event.get("closedTime"),
        "top_category": tags[0] if tags else "Uncategorized",
        "tags_json": json.dumps(tags, ensure_ascii=False),
    }
    # Embedded markets dominate memory and are already preserved losslessly in
    # raw/events-*.jsonl.zst and normalized in the market table.
    row.update(_raw_columns({key: value for key, value in event.items() if key != "markets"},
                            "gamma_event_"))
    return row


def _market_row(event: dict, raw: dict) -> dict | None:
    """Normalize one Gamma market while retaining its complete raw payload."""
    try:
        market = parse_market(raw)
    except DataError:
        return None
    outcomes = list(market.tokens) if market.tokens else _json_list(raw.get("outcomes"))
    tokens = list(market.tokens.values()) if market.tokens else _json_list(raw.get("clobTokenIds"))
    try:
        prices = [_float(value, float("nan")) for value in _json_list(raw.get("outcomePrices"))]
    except (TypeError, ValueError):
        prices = []
    winners = [label for label, price in zip(outcomes, prices)
               if price == 1 and all(other in {0, 1} for other in prices)]
    resolved = winners[0] if len(winners) == 1 else None
    folded = {str(label).casefold(): token for label, token in zip(outcomes, tokens)}
    question = market.question or str(raw.get("question") or "")
    is_updown = ("up or down" in question.casefold()
                 or {str(label).casefold() for label in outcomes} == {"up", "down"})
    tags = _tag_labels(event)
    row = {
        "market_id": market.id, "condition_id": str(raw.get("conditionId") or ""),
        "slug": market.slug, "question": question, "group_item": raw.get("groupItemTitle"),
        "event_id": str(event.get("id") or ""), "event_slug": event.get("slug"),
        "event_title": event.get("title"), "tags": tags,
        "top_category": tags[0] if tags else "Uncategorized",
        "outcomes": outcomes, "tokens": tokens, "outcome_prices": prices,
        "yes_token": folded.get("yes", folded.get("up")),
        "no_token": folded.get("no", folded.get("down")),
        "resolved_outcome": resolved, "cancelled": resolved is None,
        "volume_shares": _float(raw.get("volumeNum", raw.get("volume"))),
        "created_at": raw.get("createdAt"), "start_date": raw.get("startDate"),
        "end_date": raw.get("endDate"), "closed_time": raw.get("closedTime"),
        "description": raw.get("description"), "resolution_source": raw.get("resolutionSource"),
        "neg_risk": bool(raw.get("negRisk")),
        "neg_risk_market_id": raw.get("negRiskMarketID") or raw.get("negRiskMarketId"),
        "neg_risk_request_id": raw.get("negRiskRequestID") or raw.get("negRiskRequestId"),
        "uma_status": raw.get("umaResolutionStatus"),
        "maker_base_fee": raw.get("makerBaseFee"), "taker_base_fee": raw.get("takerBaseFee"),
        "fees_enabled": raw.get("feesEnabled"), "order_min_size": raw.get("orderMinSize"),
        "order_price_min_tick_size": raw.get("orderPriceMinTickSize"),
        "is_updown": is_updown,
    }
    row.update(_raw_columns(raw, "gamma_market_"))
    return row


def _utc_series(values):
    return pd.to_datetime(values, utc=True, errors="coerce", format="mixed")


def stratified_sample(frame: pd.DataFrame, cap: int = MAX_MARKETS,
                      seed: int = DEFAULT_SEED) -> pd.DataFrame:
    """Mark a deterministic sample stratified by month, volume decile, category."""
    if cap <= 0:
        raise ValueError("cap must be positive")
    result = frame.copy().sort_values("condition_id", kind="stable").reset_index(drop=True)
    result["close_month"] = _utc_series(result["closed_time"]).dt.strftime("%Y-%m").fillna("unknown")
    volume = pd.to_numeric(result["volume_shares"], errors="coerce").fillna(0.0)
    order = volume.rank(method="first").astype(int) - 1
    result["volume_decile"] = ((order * 10) // max(len(result), 1)).clip(upper=9).astype("int8")
    category = result.get("top_category", pd.Series("Uncategorized", index=result.index))
    category = category.fillna("Uncategorized").astype(str)
    result["stratum"] = (result["close_month"].astype(str) + "|d"
                         + result["volume_decile"].astype(str) + "|" + category)
    result["selected"] = False
    result["weight"] = float("nan")
    result["selection_order"] = pd.Series([pd.NA] * len(result), dtype="Int64")
    candidates = result.index[~result["is_updown"].fillna(False)].tolist()
    target = min(cap, len(candidates))
    if not target:
        return result
    groups = {name: list(indices) for name, indices
              in result.loc[candidates].groupby("stratum", sort=True).groups.items()}
    if target < len(groups):
        # This can only preserve a subset of strata (principally useful for a
        # deliberately tiny smoke-test limit). The production 50k cap is
        # expected to exceed the number of strata and therefore keeps all.
        names = sorted(groups)
        kept = set(random.Random(seed).sample(names, target))
        groups = {name: groups[name] for name in names if name in kept}
    allocation = {name: 1 for name in groups}
    remaining = target - len(groups)
    capacity = {name: len(indices) - 1 for name, indices in groups.items()}
    capacity_total = sum(capacity.values())
    fractions = {}
    if remaining and capacity_total:
        for name in groups:
            ideal = remaining * capacity[name] / capacity_total
            extra = min(capacity[name], int(math.floor(ideal)))
            allocation[name] += extra
            fractions[name] = ideal - extra
        left = target - sum(allocation.values())
        while left:
            choices = [name for name in groups if allocation[name] < len(groups[name])]
            choices.sort(key=lambda name: (-fractions.get(name, 0.0), name))
            for name in choices:
                if not left:
                    break
                allocation[name] += 1
                fractions[name] = 0.0
                left -= 1
    rng = random.Random(seed)
    selected = []
    for name in sorted(groups):
        indices = sorted(groups[name])
        take = allocation[name]
        chosen = indices if take == len(indices) else rng.sample(indices, take)
        result.loc[chosen, "selected"] = True
        result.loc[chosen, "weight"] = len(indices) / take
        selected.extend(chosen)
    rng.shuffle(selected)
    for number, index in enumerate(selected):
        result.at[index, "selection_order"] = number
    return result


def _write_metadata_staging(out: Path, event_rows: dict, market_rows: list[dict]) -> None:
    metadata = out / ".staging" / "metadata"
    atomic_parquet(pd.DataFrame(list(event_rows.values())), metadata / "events.parquet")
    tags = [{"condition_id": row["condition_id"], "tag": tag}
            for row in market_rows for tag in _as_list(row.get("tags"))]
    atomic_parquet(pd.DataFrame(tags, columns=["condition_id", "tag"]),
                   metadata / "market_tags.parquet")


def enumerate_universe(out_dir=DEFAULT_OUT, *, days: int = 365, cap: int = MAX_MARKETS,
                       seed: int = DEFAULT_SEED, limit: int | None = None,
                       now: datetime | None = None, client=None, rps: float = 4.0,
                       min_volume: float = 0.0, retry_attempts: int = 20,
                       log=print) -> pd.DataFrame:
    """Enumerate the full one-year universe, then mark the stratified selection.

    Normalized page files and the next cursor are checkpointed after every API
    response. This bounds memory and lets an interrupted traversal resume without
    repeating already-durable pages.
    """
    if days <= 0 or cap <= 0 or min_volume < 0 or retry_attempts <= 0:
        raise ValueError("days/cap/retry_attempts must be positive and min_volume cannot be negative")
    started = datetime.now(UTC)
    now = (now or started).astimezone(UTC)
    since = now - timedelta(days=days)
    out = Path(out_dir)
    api = LimitedClient(client or PolymarketClient(timeout=60), RateLimiter(rps))
    page_folder = out / ".staging" / "universe_pages"
    checkpoint_path = out / ".staging" / "universe_checkpoint.json"
    signature = {"days": days, "min_volume_exclusive": min_volume}
    cursor, pages = None, 0
    if checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            checkpoint = {}
        if checkpoint.get("signature") == signature and not checkpoint.get("complete"):
            since = datetime.fromisoformat(checkpoint["since"])
            now = datetime.fromisoformat(checkpoint["through"])
            cursor = checkpoint.get("next_cursor")
            pages = int(checkpoint.get("pages", 0))
            log(f"Resuming universe after {pages:,} completed pages")
    seen_cursors = set()
    while True:
        page = retrying(
            lambda: api.get_json(
                EVENTS_URL, closed="true", limit=100, end_date_min=since.date().isoformat(),
                after_cursor=cursor,
            ),
            attempts=retry_attempts,
            on_retry=lambda attempt, total, delay, exc: log(
                f"  Gamma request failed ({attempt}/{total}); retrying in {delay:.1f}s: {exc}"
            ),
        )
        events = page.get("events")
        if not isinstance(events, list):
            raise DataError("Event keyset response is missing its events array")
        page_number = pages + 1
        atomic_jsonl_zstd(events, out / "raw" / f"events-{page_number:06d}.jsonl.zst")
        page_markets, page_events = [], []
        for event in events:
            if not isinstance(event, dict):
                continue
            page_events.append(_event_row(event))
            for raw_market in event.get("markets") or []:
                if not isinstance(raw_market, dict):
                    continue
                row = _market_row(event, raw_market)
                if not row or not row["condition_id"] or row["volume_shares"] <= min_volume:
                    continue
                closed = pd.to_datetime(row["closed_time"], utc=True, errors="coerce")
                if pd.isna(closed) or closed < pd.Timestamp(since) or closed > pd.Timestamp(now):
                    continue
                page_markets.append(row)
        atomic_parquet(pd.DataFrame(page_events),
                       page_folder / f"events-{page_number:06d}.parquet")
        atomic_parquet(pd.DataFrame(page_markets),
                       page_folder / f"markets-{page_number:06d}.parquet")
        pages = page_number
        if pages % 20 == 0:
            log(f"  {pages:,} event pages checkpointed")
        next_cursor = page.get("next_cursor")
        if not events or not next_cursor:
            atomic_json({"signature": signature, "since": since.isoformat(),
                         "through": now.isoformat(), "pages": pages,
                         "next_cursor": None, "complete": True}, checkpoint_path)
            break
        if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
            raise DataError("Event keyset cursor is invalid or repeated; refusing a partial universe")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        atomic_json({"signature": signature, "since": since.isoformat(),
                     "through": now.isoformat(), "pages": pages,
                     "next_cursor": cursor, "complete": False}, checkpoint_path)
    market_parts = [pd.read_parquet(page_folder / f"markets-{number:06d}.parquet")
                    for number in range(1, pages + 1)
                    if (page_folder / f"markets-{number:06d}.parquet").exists()]
    event_parts = [pd.read_parquet(page_folder / f"events-{number:06d}.parquet")
                   for number in range(1, pages + 1)
                   if (page_folder / f"events-{number:06d}.parquet").exists()]
    if not market_parts:
        raise DataError("No resolved markets with positive volume matched the requested window")
    frame = pd.concat(market_parts, ignore_index=True).drop_duplicates("condition_id", keep="last")
    events_frame = pd.concat(event_parts, ignore_index=True).drop_duplicates("event_id", keep="last")
    for column in ("created_at", "start_date", "end_date", "closed_time"):
        frame[column] = _utc_series(frame[column])
    frame = stratified_sample(frame, min(cap, limit) if limit is not None else cap, seed)
    atomic_parquet(frame, out / "universe.parquet")
    metadata = out / ".staging" / "metadata"
    atomic_parquet(events_frame, metadata / "events.parquet")
    tags = [{"condition_id": row["condition_id"], "tag": tag}
            for _, row in frame.iterrows() for tag in _as_list(row.get("tags"))]
    atomic_parquet(pd.DataFrame(tags, columns=["condition_id", "tag"]),
                   metadata / "market_tags.parquet")
    selected = int(frame["selected"].sum())
    update_manifest(
        out, "universe", started=started,
        parameters={"days": days, "since": since.isoformat(), "through": now.isoformat(),
                    "cap": cap, "limit": limit, "seed": seed, "rps": rps,
                    "min_volume_exclusive": min_volume, "retry_attempts": retry_attempts},
        row_counts={"event_pages": pages, "events": len(events_frame),
                    "eligible_markets": len(frame), "selected_markets": selected,
                    "excluded_updown": int(frame["is_updown"].sum())},
    )
    log(f"Saved {len(frame):,} eligible markets; selected {selected:,} in {out / 'universe.parquet'}")
    return frame


def pull_markets(out_dir=DEFAULT_OUT, *, since=None, min_volume=0.0, client=None, log=print):
    """Compatibility wrapper for the former market-enumeration API."""
    now = datetime.now(UTC)
    start = pd.to_datetime(since, utc=True).to_pydatetime() if since else now - timedelta(days=365)
    days = max(1, math.ceil((now - start).total_seconds() / 86400))
    frame = enumerate_universe(out_dir, days=days, client=client,
                               min_volume=min_volume, log=log)
    return frame[frame["selected"]].reset_index(drop=True)


def _selected_markets(out: Path, limit: int | None = None) -> pd.DataFrame:
    frame = pd.read_parquet(out / "universe.parquet")
    frame = frame[frame["selected"].fillna(False)].sort_values("selection_order", kind="stable")
    return frame.head(limit).reset_index(drop=True) if limit is not None else frame.reset_index(drop=True)


def _atomic_parquet(frame: pd.DataFrame, path: Path) -> None:
    atomic_parquet(frame, path)


def _trade_frame(rows: list[dict], condition_id: str = "") -> pd.DataFrame:
    values = {
        "condition_id": [condition_id] * len(rows), "timestamp": [row["timestamp"] for row in rows],
        "asset": [row.get("asset") for row in rows],
        "outcome_index": [row.get("outcomeIndex", -1) for row in rows],
        "side": [row.get("side") for row in rows], "size": [row.get("size") for row in rows],
        "price": [row.get("price") for row in rows],
        "wallet": [row.get("proxyWallet") for row in rows],
        "tx": [row.get("transactionHash") for row in rows],
    }
    return pd.DataFrame(values, columns=list(TRADE_COLUMNS)).astype(TRADE_COLUMNS).sort_values(
        "timestamp", kind="stable").reset_index(drop=True)


def _parallel_download(markets: pd.DataFrame, job, *, workers: int, label: str,
                       out: Path, log=print) -> dict:
    done = failed = rows = 0
    progress = Progress(len(markets), label=label, log=log)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(job, market): market for _, market in markets.iterrows()}
        for future in as_completed(futures):
            market = futures[future]
            try:
                rows += int(future.result())
                done += 1
            except (DataError, OSError, ValueError, TypeError) as exc:
                failed += 1
                append_failure(out, stage=label, condition_id=str(market["condition_id"]), error=exc)
            progress.report(done + failed, detail=f"{rows:,} rows, {failed} failed")
    return {"fetched": done, "failed": failed, "rows": rows}


def pull_trades(out_dir=DEFAULT_OUT, *, workers=4, limit=None, sample=None, seed=0,
                include_updown=False, client=None, rps=4.0, log=print) -> dict:
    """Download complete taker-only trades to one atomic file per market."""
    if workers <= 0:
        raise ValueError("workers must be positive")
    started = datetime.now(UTC)
    out = Path(out_dir)
    markets = _selected_markets(out)
    if include_updown:
        universe = pd.read_parquet(out / "universe.parquet")
        markets = pd.concat([markets, universe[universe["is_updown"]]], ignore_index=True)
    if sample:
        markets = markets.sample(n=min(sample, len(markets)), random_state=seed)
    if limit is not None:
        markets = markets.head(limit)
    folder = out / ".staging" / "trades"
    pending = markets[~markets["condition_id"].map(lambda cid: (folder / f"{cid}.parquet").exists())]
    skipped = len(markets) - len(pending)
    log(f"Trades: {len(markets):,} selected, {skipped:,} complete, {len(pending):,} pending")
    api = LimitedClient(client or PolymarketClient(timeout=60), RateLimiter(rps))

    def job(market):
        condition_id = str(market["condition_id"])
        rows = retrying(lambda: fetch_trades(api, condition_id))
        atomic_parquet(_trade_frame(rows, condition_id), folder / f"{condition_id}.parquet")
        return len(rows)

    result = _parallel_download(pending, job, workers=workers, label="trades", out=out, log=log)
    result["skipped"] = skipped
    update_manifest(out, "trades", started=started,
                    parameters={"workers": workers, "limit": limit, "rps": rps}, row_counts=result)
    return result


def _price_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(PRICE_COLUMNS)).astype(PRICE_COLUMNS).sort_values(
        ["token_id", "timestamp"], kind="stable").reset_index(drop=True)


def _market_window(market, now: datetime) -> tuple[int, int]:
    created = pd.to_datetime(market.get("created_at"), utc=True, errors="coerce")
    closed = pd.to_datetime(market.get("closed_time"), utc=True, errors="coerce")
    if pd.isna(created) or pd.isna(closed):
        raise ValueError("Market is missing created_at or closed_time")
    ending = min(closed.to_pydatetime() + timedelta(days=7), now)
    beginning = created.to_pydatetime()
    if ending <= beginning:
        ending = closed.to_pydatetime() + timedelta(seconds=1)
    return int(beginning.timestamp()), int(ending.timestamp()) + 1


def pull_prices(out_dir=DEFAULT_OUT, *, workers=4, limit=None, client=None,
                rps=4.0, now: datetime | None = None, log=print) -> dict:
    """Download each outcome token at the finest bucket that returns history."""
    if workers <= 0:
        raise ValueError("workers must be positive")
    started = datetime.now(UTC)
    now = (now or started).astimezone(UTC)
    out = Path(out_dir)
    markets = _selected_markets(out, limit)
    folder = out / ".staging" / "prices"
    status_folder = out / ".staging" / "price_status"
    complete = markets["condition_id"].map(
        lambda cid: (folder / f"{cid}.parquet").exists() and (status_folder / f"{cid}.json").exists())
    pending = markets[~complete]
    skipped = int(complete.sum())
    log(f"Prices: {len(markets):,} selected, {skipped:,} complete, {len(pending):,} pending")
    api = LimitedClient(client or PolymarketClient(timeout=60), RateLimiter(rps))

    def job(market):
        condition_id = str(market["condition_id"])
        outcomes, tokens = _as_list(market.get("outcomes")), _as_list(market.get("tokens"))
        start, end = _market_window(market, now)
        rows, statuses = [], []
        for outcome, token in zip(outcomes, tokens):
            attempts, chosen, points = [], None, []
            for bucket in PRICE_BUCKETS:
                points = retrying(lambda b=bucket: fetch_history(api, str(token), start, end, b))
                attempts.append({"bucket_seconds": bucket, "points": len(points)})
                if points:
                    chosen = bucket
                    break
            statuses.append({
                "token_id": str(token), "outcome": outcome, "attempts": attempts,
                "selected_bucket_seconds": chosen, "status": "ok" if points else "no_history",
                "reason": None if points else "All resolution fallbacks returned no points",
            })
            rows.extend({
                "condition_id": condition_id, "token_id": str(token), "outcome": outcome,
                "timestamp": point["timestamp"], "price": point["price"],
                "resolution_seconds": point["resolution_seconds"],
                "requested_bucket_seconds": chosen,
            } for point in points)
        if not tokens:
            statuses.append({"status": "no_tokens", "reason": "Market has no CLOB outcome tokens"})
        atomic_parquet(_price_frame(rows), folder / f"{condition_id}.parquet")
        atomic_json({"condition_id": condition_id, "tokens": statuses},
                    status_folder / f"{condition_id}.json")
        return len(rows)

    result = _parallel_download(pending, job, workers=workers, label="prices", out=out, log=log)
    result["skipped"] = skipped
    update_manifest(out, "prices", started=started,
                    parameters={"workers": workers, "limit": limit, "rps": rps,
                                "fallback_buckets": list(PRICE_BUCKETS)}, row_counts=result)
    return result


def load_trades(out_dir=DEFAULT_OUT, condition_ids=None) -> pd.DataFrame:
    folder = Path(out_dir) / ".staging" / "trades"
    paths = ([folder / f"{cid}.parquet" for cid in condition_ids] if condition_ids is not None
             else sorted(folder.glob("*.parquet")))
    frames = [pd.read_parquet(path) for path in paths if path.exists()]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(TRADE_COLUMNS))


def volume_bars(trades: pd.DataFrame, yes_token: str | None, freq="1h") -> pd.DataFrame:
    """Create non-gap-filled OHLC, volume, VWAP, and YES-flow trade bars."""
    columns = ["time", "trades", "shares", "usd", "yes_vwap", "net_yes_shares",
               "open", "high", "low", "close"]
    if trades.empty:
        return pd.DataFrame(columns=columns)
    is_yes = trades["asset"].astype(str) == str(yes_token)
    yes_price = trades["price"].where(is_yes, 1 - trades["price"])
    towards_yes = (trades["side"].str.upper() == "BUY") == is_yes
    frame = pd.DataFrame({
        "timestamp": trades["timestamp"],
        "time": pd.to_datetime(trades["timestamp"], unit="s", utc=True).dt.floor(freq),
        "shares": trades["size"], "usd": trades["size"] * trades["price"],
        "yes_price": yes_price, "weighted": trades["size"] * yes_price,
        "net": trades["size"].where(towards_yes, -trades["size"]),
    }).sort_values(["time", "timestamp"], kind="stable")
    bars = frame.groupby("time", sort=True).agg(
        trades=("shares", "size"), shares=("shares", "sum"), usd=("usd", "sum"),
        weighted=("weighted", "sum"), net_yes_shares=("net", "sum"),
        open=("yes_price", "first"), high=("yes_price", "max"),
        low=("yes_price", "min"), close=("yes_price", "last"),
    ).reset_index()
    bars["yes_vwap"] = bars.pop("weighted") / bars["shares"]
    if yes_token is None or pd.isna(yes_token):
        bars[["yes_vwap", "net_yes_shares", "open", "high", "low", "close"]] = float("nan")
    return bars[columns]


def _read_staging(folder: Path, ids: set[str]) -> pd.DataFrame:
    frames = [pd.read_parquet(path) for path in sorted(folder.glob("*.parquet")) if path.stem in ids]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _write_partitioned(frame: pd.DataFrame, root: Path, month_by_id: dict[str, str]) -> int:
    if frame.empty:
        return 0
    data = frame.copy()
    data["close_month"] = data["condition_id"].astype(str).map(month_by_id).fillna("unknown")
    for month, part in data.groupby("close_month", sort=True):
        atomic_parquet(part.reset_index(drop=True), root / f"close_month={month}" / "part-00000.parquet")
    return len(data)


def compact_dataset(out_dir=DEFAULT_OUT, *, limit=None, log=print) -> dict:
    """Compact completed per-market downloads into public partitioned tables."""
    started = datetime.now(UTC)
    out = Path(out_dir)
    markets = _selected_markets(out, limit)
    ids = set(markets["condition_id"].astype(str))
    month_by_id = dict(zip(markets["condition_id"].astype(str), markets["close_month"].astype(str)))
    atomic_parquet(markets, out / "markets.parquet")
    metadata = out / ".staging" / "metadata"
    events = pd.read_parquet(metadata / "events.parquet")
    events = events[events["event_id"].astype(str).isin(set(markets["event_id"].astype(str)))]
    events = events.drop_duplicates("event_id")
    atomic_parquet(events, out / "events.parquet")
    tags = pd.read_parquet(metadata / "market_tags.parquet")
    tags = tags[tags["condition_id"].astype(str).isin(ids)]
    atomic_parquet(tags, out / "market_tags.parquet")
    trades = _read_staging(out / ".staging" / "trades", ids)
    prices = _read_staging(out / ".staging" / "prices", ids)
    counts = {
        "markets": len(markets), "events": len(events), "market_tags": len(tags),
        "trades": _write_partitioned(trades, out / "trades", month_by_id),
        "prices": _write_partitioned(prices, out / "prices", month_by_id),
    }
    yes_tokens = dict(zip(markets["condition_id"].astype(str), markets["yes_token"]))
    for freq, folder_name in (("1min", "bars_1min"), ("1h", "bars_1h"), ("1D", "bars_1d")):
        parts = []
        if not trades.empty:
            for condition_id, group in trades.groupby("condition_id", sort=False):
                bars = volume_bars(group, yes_tokens.get(str(condition_id)), freq)
                if not bars.empty:
                    parts.append(bars.assign(condition_id=str(condition_id)))
        bars_frame = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        counts[folder_name] = _write_partitioned(bars_frame, out / folder_name, month_by_id)
    update_manifest(out, "compact", started=started,
                    parameters={"limit": limit, "bar_frequencies": ["1min", "1h", "1d"]},
                    row_counts=counts)
    log("Compacted dataset: " + ", ".join(f"{key}={value:,}" for key, value in counts.items()))
    return counts


def build_volume_bars(out_dir=DEFAULT_OUT, freq="1h", log=print) -> pd.DataFrame:
    """Compatibility helper that builds one in-memory frequency."""
    out = Path(out_dir)
    markets = _selected_markets(out)
    yes = dict(zip(markets["condition_id"].astype(str), markets["yes_token"]))
    trades = _read_staging(out / ".staging" / "trades", set(yes))
    parts = []
    for condition_id, group in trades.groupby("condition_id", sort=False):
        bars = volume_bars(group, yes.get(str(condition_id)), freq)
        if not bars.empty:
            parts.append(bars.assign(condition_id=str(condition_id)))
    result = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    atomic_parquet(result, out / f"volume_{freq}.parquet")
    log(f"Saved {len(result):,} {freq} bars")
    return result


def _winner_consistent(row) -> bool:
    outcomes, prices = _as_list(row.get("outcomes")), _as_list(row.get("outcome_prices"))
    winners = [outcome for outcome, price in zip(outcomes, prices) if price == 1]
    expected, actual = (winners[0] if len(winners) == 1 else None), row.get("resolved_outcome")
    return (expected is None and (actual is None or pd.isna(actual))) or expected == actual


def validate_dataset(out_dir=DEFAULT_OUT, *, limit=None, log=print) -> dict:
    """Return and record data-quality findings without failing on findings."""
    started = datetime.now(UTC)
    out = Path(out_dir)
    markets = _selected_markets(out, limit)
    trade_folder, price_folder = out / ".staging" / "trades", out / ".staging" / "prices"
    status_folder = out / ".staging" / "price_status"
    mismatches, time_outliers, missing_trades, missing_prices = [], [], [], []
    duplicate_count = 0
    duplicate_columns = ["tx", "asset", "wallet", "side", "size", "price", "timestamp"]
    for _, market in markets.iterrows():
        condition_id = str(market["condition_id"])
        trade_path, price_path = trade_folder / f"{condition_id}.parquet", price_folder / f"{condition_id}.parquet"
        if not trade_path.exists():
            missing_trades.append(condition_id)
            trades = pd.DataFrame()
        else:
            trades = pd.read_parquet(trade_path)
            duplicate_count += int(trades.duplicated(duplicate_columns).sum())
            actual, expected = float(trades["size"].sum()), float(market["volume_shares"])
            relative = abs(actual - expected) / expected if expected else (0.0 if actual == 0 else float("inf"))
            if relative > 0.01:
                mismatches.append({"condition_id": condition_id, "gamma_shares": expected,
                                   "trade_shares": actual, "relative_error": relative})
        if not price_path.exists() or not (status_folder / f"{condition_id}.json").exists():
            missing_prices.append(condition_id)
            prices = pd.DataFrame()
        else:
            prices = pd.read_parquet(price_path)
        lower = pd.to_datetime(market["created_at"], utc=True, errors="coerce") - pd.Timedelta(days=1)
        upper = pd.to_datetime(market["closed_time"], utc=True, errors="coerce") + pd.Timedelta(days=7)
        for kind, frame in (("trade", trades), ("price", prices)):
            if frame.empty or pd.isna(lower) or pd.isna(upper):
                continue
            times = pd.to_datetime(frame["timestamp"], unit="s", utc=True)
            bad = int(((times < lower) | (times > upper)).sum())
            if bad:
                time_outliers.append({"condition_id": condition_id, "kind": kind, "count": bad})
    winner_bad = [str(row["condition_id"]) for _, row in markets.iterrows()
                  if not _winner_consistent(row)]
    report = {
        "selected_markets": len(markets), "volume_mismatches_over_1pct": mismatches,
        "duplicate_trades": duplicate_count, "missing_trade_files": missing_trades,
        "missing_price_or_status_files": missing_prices, "winner_inconsistencies": winner_bad,
        "cancelled_markets": int(markets["cancelled"].sum()),
        "category_coverage": sorted(markets["top_category"].dropna().astype(str).unique().tolist()),
        "month_coverage": sorted(markets["close_month"].dropna().astype(str).unique().tolist()),
        "timestamp_outliers": time_outliers,
    }
    atomic_json(report, out / "validation.json")
    update_manifest(out, "validate", started=started, parameters={"limit": limit},
                    row_counts={"selected_markets": len(markets), "volume_mismatches": len(mismatches),
                                "duplicate_trades": duplicate_count,
                                "missing_trade_files": len(missing_trades),
                                "missing_price_files": len(missing_prices),
                                "timestamp_outliers": sum(item["count"] for item in time_outliers)},
                    extra={"validation_summary": report})
    log(json.dumps(report, indent=2, default=str))
    return report
