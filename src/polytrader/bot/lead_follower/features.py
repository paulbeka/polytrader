"""Causal burst statistics and observable book changes, independent of execution."""

from decimal import Decimal as D
from statistics import mean, median

ZERO = D(0)


def trade_stats(rows, seconds):
    buy = sum((size for _, size, signed in rows if signed > 0), ZERO)
    sell = sum((size for _, size, signed in rows if signed < 0), ZERO)
    volume = buy + sell
    gaps = [b[0] - a[0] for a, b in zip(rows, rows[1:])]
    return dict(count=len(rows), volume=volume, buy_volume=buy, sell_volume=sell,
                imbalance=(buy - sell) / volume if volume else ZERO,
                trades_per_second=len(rows) / seconds if seconds > 0 else None,
                interarrival_mean_seconds=mean(gaps) if gaps else None,
                interarrival_median_seconds=median(gaps) if gaps else None,
                interarrival_min_seconds=min(gaps) if gaps else None,
                interarrival_max_seconds=max(gaps) if gaps else None)


def burst_features(tape, now, started, lookback, baseline, previous_gap):
    rows = list(tape)
    recent = [r for r in rows if r[0] > now - lookback]
    prior = [r for r in rows if now - lookback - baseline < r[0] <= now - lookback]
    observed = min(baseline, max(0, now - started - lookback))
    expected_count = D(len(prior)) * D(str(lookback)) / D(str(observed)) if observed else ZERO
    # A finite one-trade floor recognizes sparse bursts without infinite scores.
    score = D(len(recent)) / max(D(1), expected_count)
    current = trade_stats(recent, lookback)
    return dict(
        trade_10s=trade_stats([r for r in rows if r[0] > now - 10], 10),
        trade_30s=trade_stats([r for r in rows if r[0] > now - 30], 30),
        activity_window_seconds=lookback, activity=current,
        baseline=trade_stats(prior, observed), baseline_observed_seconds=observed,
        baseline_requested_seconds=baseline, baseline_complete=observed >= baseline,
        expected_trade_count=expected_count, burst_score=score,
        time_since_previous_trade_seconds=previous_gap,
    )


def book_delta(previous, current):
    result = {}
    for side in ("bids", "asks"):
        before = {l.price: l.size for l in getattr(previous, side)}
        after = {l.price: l.size for l in getattr(current, side)}
        prices = before.keys() | after.keys()
        result[side + "_levels_changed"] = sum(before.get(p, ZERO) != after.get(p, ZERO) for p in prices)
        result[side + "_levels_removed"] = len(before.keys() - after.keys())
        result[side + "_shares_reduced"] = sum((max(ZERO, before.get(p, ZERO) - after.get(p, ZERO))
                                                 for p in prices), ZERO)
        result[side + "_shares_added"] = sum((max(ZERO, after.get(p, ZERO) - before.get(p, ZERO))
                                               for p in prices), ZERO)
    result["best_bid_changes"] = int(previous.best_bid.price != current.best_bid.price)
    result["best_ask_changes"] = int(previous.best_ask.price != current.best_ask.price)
    return result
