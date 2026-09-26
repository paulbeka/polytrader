"""Pure Decimal sizing of earlier-NO / later-YES, using executable asks only."""

from decimal import ROUND_FLOOR, localcontext

from .models import D, Evaluation


def _fills(asks, quantity, metadata):
    remaining, levels = quantity, []
    for level in asks:
        take = min(remaining, level.size)
        if take:
            levels.append({"price": level.price, "shares": take,
                           "purchase_cost": take * level.price,
                           "fee": metadata.fee(level.price, take)})
            remaining -= take
        if not remaining:
            break
    if remaining:
        raise ValueError("Sizing exceeded available depth")
    purchase = sum((v["purchase_cost"] for v in levels), D(0))
    fee = sum((v["fee"] for v in levels), D(0))
    return {"consumed_levels": levels, "purchase_cost": purchase, "fee": fee,
            "vwap": purchase / quantity if quantity else None,
            "worst_price": levels[-1]["price"] if levels else None,
            "fee_metadata": {k: getattr(metadata, k) for k in
                             ("condition_id", "fetched_at", "adapter", "rate", "exponent",
                              "min_notional", "tick", "share_step", "provenance")}}


def _profitable_prefix(left, right, lm, rm, settings, costs):
    i = j = 0
    a, b, quantity = left[0].size, right[0].size, D(0)
    while i < len(left) and j < len(right):
        p, r = left[i].price, right[j].price
        edge = (1 - p - r - lm.unit_fee(p) - rm.unit_fee(r)
                - costs.extra_cost_per_pair - costs.execution_buffer_per_pair)
        if edge <= 0 or edge < settings.min_edge_per_pair:
            break
        take = min(a, b)
        quantity += take
        a, b = a - take, b - take
        if a == 0:
            i += 1
            if i < len(left):
                a = left[i].size
        if b == 0:
            j += 1
            if j < len(right):
                b = right[j].size
    return quantity


def evaluate(pair, books, metadata, settings, costs, now, *, healthy=True):
    """Capture both snapshots before calling. No I/O, wall-clock reads or awaits.

    Decimal context is isolated from a caller's precision settings. Book update
    age is diagnostic only; a quiet live book on a healthy feed is eligible.
    """
    with localcontext() as context:
        context.prec = 50
        return _evaluate(pair, books, metadata, settings, costs, now, healthy)


def _evaluate(pair, books, metadata, settings, costs, now, healthy):
    if not healthy:
        return Evaluation("blocked", "feed_not_healthy")
    for entry in (pair.earlier_entry, pair.later_entry):
        if entry.deadline is not None and now >= entry.deadline:
            return Evaluation("blocked", "configured_deadline_passed")
    legs_meta = [metadata.get(m.condition_id) for m in (pair.earlier, pair.later)]
    for meta in legs_meta:
        if meta is None:
            return Evaluation("blocked", "missing_metadata")
        if not meta.supported:
            return Evaluation("blocked", meta.reason)
        if (now - meta.fetched_at).total_seconds() > costs.max_metadata_age_seconds:
            return Evaluation("blocked", "metadata_expired")
        if (meta.rate < 0 or meta.exponent not in (1, 2) or meta.rate * meta.exponent > 1
                or meta.share_step != D(".01")):
            return Evaluation("blocked", "unsupported_cost_or_quantity_model")
    for token, book, meta in zip(pair.tokens, books, legs_meta):
        if book.token_id != token:
            return Evaluation("blocked", "wrong_outcome_token")
        if book.status != "live":
            return Evaluation("blocked", "book_" + book.status)
        if not book.asks:
            return Evaluation("blocked", "missing_asks")
        previous = D(-1)
        for level in book.asks:
            if (not level.price.is_finite() or not level.size.is_finite()
                    or not 0 < level.price < 1 or level.size <= 0
                    or level.price <= previous or level.price % meta.tick != 0):
                return Evaluation("blocked", "invalid_ask_or_tick_metadata")
            previous = level.price
    left, right = (b.asks[:1] if settings.depth_mode == "top" else b.asks for b in books)
    lm, rm = legs_meta
    raw_available = min(sum((v.size for v in left), D(0)), sum((v.size for v in right), D(0)))
    prefix = (raw_available if settings.depth_mode == "top" else
              _profitable_prefix(left, right, lm, rm, settings, costs))
    cap = min(prefix, settings.max_shares)
    step = lm.share_step

    def calculate(quantity):
        legs = [_fills(left, quantity, lm), _fills(right, quantity, rm)]
        purchase = sum((leg["purchase_cost"] for leg in legs), D(0))
        fees = sum((leg["fee"] for leg in legs), D(0))
        other = costs.fixed_cost_per_opportunity + quantity * costs.extra_cost_per_pair
        buffer = quantity * costs.execution_buffer_per_pair
        total = purchase + fees + other + buffer
        return {"shares": quantity, "legs": legs, "purchase_cost": purchase,
                "fee_cost": fees, "other_cost": other, "buffer": buffer,
                "total_cost_with_buffer": total, "minimum_payout": quantity,
                "gross_profit": quantity - purchase,
                "estimated_net_profit": quantity - purchase - fees - other,
                "conservative_profit": quantity - total,
                "conservative_edge_per_pair": (quantity - total) / quantity if quantity else D(0)}

    # Exact integer search on the order-size grid, including rounded fees/fixed cost.
    low, high = 0, int((cap / step).to_integral_value(rounding=ROUND_FLOOR))
    while low < high:
        middle = (low + high + 1) // 2
        if calculate(step * middle)["total_cost_with_buffer"] <= settings.max_cost_per_opportunity:
            low = middle
        else:
            high = middle - 1
    result = calculate(step * low)
    result.update({"depth_mode": settings.depth_mode, "available_paired_shares": raw_available,
                   "best_ask_paired_shares": min(b.best_ask.size for b in books),
                   "profitable_prefix_shares": prefix if settings.depth_mode == "full" else None,
                   "fixed_cost_per_opportunity": costs.fixed_cost_per_opportunity,
                   "extra_cost_per_pair": costs.extra_cost_per_pair,
                   "execution_buffer_per_pair": costs.execution_buffer_per_pair,
                   "thresholds": {"min_edge_per_pair": settings.min_edge_per_pair,
                                  "min_total_profit": settings.min_total_profit,
                                  "min_shares": settings.min_shares,
                                  "max_shares": settings.max_shares,
                                  "max_cost_per_opportunity": settings.max_cost_per_opportunity},
                   "quotes": [{"token_id": b.token_id, "status": b.status,
                               "best_ask": b.best_ask, "updated_at": b.updated_at,
                               "received_at": b.received_at,
                               "source_age_seconds": (now - b.updated_at).total_seconds() if b.updated_at else None}
                              for b in books]})
    reason = "qualifies"
    if result["shares"] < settings.min_shares:
        reason = "insufficient_quantity_or_budget"
    elif any(leg["purchase_cost"] < meta.min_notional for leg, meta in zip(result["legs"], legs_meta)):
        reason = "below_minimum_notional"
    elif result["conservative_profit"] <= 0:
        reason = "nonpositive_conservative_profit"
    elif result["conservative_profit"] < settings.min_total_profit:
        reason = "below_minimum_profit"
    elif result["conservative_profit"] < settings.min_edge_per_pair * result["shares"]:
        reason = "below_minimum_edge"
    return Evaluation("qualified" if reason == "qualifies" else "rejected", reason, result)
