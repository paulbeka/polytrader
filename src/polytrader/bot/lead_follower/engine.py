"""Deterministic signal/position state machine; no network, clocks or file access."""

from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN
from .features import burst_features, book_delta

D = Decimal
ZERO = D(0)


def valid(book):
    return (book is not None and book.status == "live" and book.midpoint is not None
            and book.spread >= 0)


def sweep(levels, shares):
    """Return full-size cash and consumed levels, or None. Never invent liquidity."""
    remaining, cash, used = shares, ZERO, []
    for level in levels:
        take = min(remaining, level.size)
        if take:
            cash += take * level.price
            used.append({"price": level.price, "shares": take})
            remaining -= take
        if not remaining:
            return cash, used
    return None


@dataclass
class Position:
    id: int
    group: str
    event: str
    market: str
    token: str
    direction: str
    detected: float
    due: float
    state: str = "pending"
    shares: Decimal = ZERO
    cost: Decimal = ZERO
    opened: float | None = None
    exit_due: float | None = None
    exit_reason: str | None = None


class Engine:
    def __init__(self, families, settings, emit):
        self.families, self.s, self.emit = families, settings, emit
        self.tokens = {t for f in families for m in (f.leader, *f.followers) for t in (m.yes, m.no)}
        self.leaders = {f.leader.yes for f in families}
        self.history = defaultdict(deque)
        self.tape = defaultdict(deque)
        self.last_trade_time, self.previous_gap = {}, {}
        self.arrival_gaps = defaultdict(deque)
        self.last_books, self.depth_changes = {}, defaultdict(deque)
        self.prices, self.price_changes = {}, {}
        self.positions, self.cooldowns = {}, {}
        self.started, self.last_now, self.utc = None, None, None
        self.sequence = 0
        self.candidate_sequence = 0
        self.candidate_stats = defaultdict(lambda: dict(total=0, qualified=0, rejected=0,
                                                       rejection_reasons=defaultdict(int)))
        self.stats = defaultdict(lambda: dict(detected=0, rejected=0, opened=0, closed=0,
                                             wins=0, losses=0, closed_pnl=ZERO, hold_seconds=0.0))
        for family in families:
            for follower in family.followers:
                self.stats[(family.id, family.event, follower.id)]
        self.paused = True

    def event(self, kind, now, **data):
        self.emit({"type": kind, "elapsed_seconds": now, "utc": self.utc,
                   "hypothetical": True, "fees": "0", "strategy_version": 2, **data})

    def record(self, kind, p, now, **data):
        self.event(kind, now, position_id=p.id, group=p.group, event=p.event,
                   follower=p.market, token=p.token, direction=p.direction, **data)

    def counters(self, p):
        return self.stats[(p.group, p.event, p.market)]

    def reject(self, p, now, reason):
        self.counters(p)["rejected"] += 1
        self.record("entry_rejected", p, now, reason=reason)
        self.positions.pop(p.market, None)

    def reset(self, now, reason, *, cancel_exits=True):
        self.history.clear()
        self.tape.clear()
        self.last_trade_time.clear()
        self.previous_gap.clear()
        self.arrival_gaps.clear()
        self.last_books.clear()
        self.depth_changes.clear()
        self.prices.clear()
        self.price_changes.clear()
        self.started = None
        if not self.paused:
            self.event("signals_paused", now, reason=reason)
        self.paused = True
        for p in list(self.positions.values()):
            if p.state == "pending":
                self.reject(p, now, reason)
            elif cancel_exits:
                p.exit_due = None
                p.exit_reason = None

    def observe(self, now, utc, books, *, trade=None, healthy=True, reason="feed_gap"):
        if self.last_now is not None and now < self.last_now:
            raise ValueError("Observation times must be nondecreasing")
        self.last_now, self.utc = now, utc
        if not healthy:
            self.reset(now, reason)
            return
        all_valid = all(valid(books.get(t)) for t in self.tokens)
        if not all_valid:
            # Keep the healthy trade tape for research even if a selected book
            # is unavailable. Pending entries remain blocked as before.
            for p in list(self.positions.values()):
                if p.state == "pending":
                    self.reject(p, now, "incomplete_or_invalid_books")
        # Existing positions can exit while signal histories are warming up.
        self.manage(now, books)
        if self.started is None:
            self.started = now
            self.paused = False
            self.event("warmup_started", now,
                       required_seconds=self.s.warmup_seconds)
        for token in self.tokens:
            book = books.get(token)
            if not valid(book):
                self.history.pop(token, None)
                self.prices.pop(token, None)
                self.price_changes.pop(token, None)
                self.last_books.pop(token, None)
                self.depth_changes.pop(token, None)
                continue
            price = (book.best_bid.price, book.best_ask.price)
            if self.prices.get(token) != price:
                self.price_changes[token] = now
                self.prices[token] = price
            history = self.history[token]
            if not history or history[-1][1] != book.midpoint:
                history.append((now, book.midpoint))
            while len(history) > 1 and history[1][0] <= now - max(30, self.s.lookback_seconds):
                history.popleft()
            if token in self.leaders:
                before = self.last_books.get(token)
                if before is not None and (before.bids != book.bids or before.asks != book.asks):
                    self.depth_changes[token].append((now, book_delta(before, book)))
                self.last_books[token] = book
                while self.depth_changes[token] and self.depth_changes[token][0][0] <= now - 30:
                    self.depth_changes[token].popleft()
        if trade is not None and trade["token"] in self.leaders:
            token = trade["token"]
            previous = self.last_trade_time.get(token)
            self.previous_gap[token] = now - previous if previous is not None else None
            self.last_trade_time[token] = now
            self.tape[token].append((now, trade["size"], trade["signed_size"]))
            self.arrival_gaps[token].append((now, self.previous_gap[token]))
        cutoff = now - max(30, self.s.lookback_seconds + self.s.baseline_seconds)
        for tape in self.tape.values():
            while tape and tape[0][0] <= cutoff:
                tape.popleft()
        for gaps in self.arrival_gaps.values():
            while gaps and gaps[0][0] <= cutoff:
                gaps.popleft()
        # A candidate is evaluated only on a newly received leader trade. Timers
        # and book updates continue managing positions without repeating candidates.
        for family in self.families:
            if trade is not None and trade["token"] == family.leader.yes:
                self.detect(family, now, books, trade, all_valid)

    def movement(self, token, now, seconds=None):
        seconds = self.s.lookback_seconds if seconds is None else seconds
        history = self.history[token]
        previous = next((value for t, value in reversed(history) if t <= now - seconds), None)
        if previous is None:
            return None
        return (history[-1][1] - previous) * 100

    def detect(self, family, now, books, trade, all_valid):
        leader = family.leader.yes
        features = burst_features(self.tape[leader], now, self.started, self.s.lookback_seconds,
                                  self.s.baseline_seconds, self.previous_gap.get(leader))
        features["gap_before_cluster_seconds"] = next(
            (gap for t, gap in self.arrival_gaps[leader] if t > now - self.s.lookback_seconds), None)
        activity = features["activity"]
        if activity["count"] < self.s.candidate_min_trades:
            return
        move = self.movement(leader, now)
        imbalance = activity["imbalance"]
        direction = 1 if imbalance > 0 else -1 if imbalance < 0 else 0
        depth = {}
        for _, delta in self.depth_changes[leader]:
            for key, value in delta.items():
                depth[key] = depth.get(key, 0) + value
        leader_book = books.get(leader)
        features.update(leader_move_pp=move,
                        leader_midpoint_moves_pp={str(s): self.movement(leader, now, s) for s in (5, 10, 30)},
                        leader_spread=leader_book.spread if valid(leader_book) else None,
                        leader_best_bid=leader_book.best_bid.price if valid(leader_book) else None,
                        leader_best_ask=leader_book.best_ask.price if valid(leader_book) else None,
                        leader_bid_depth_shares=sum((l.size for l in leader_book.bids), ZERO)
                            if valid(leader_book) else None,
                        leader_ask_depth_shares=sum((l.size for l in leader_book.asks), ZERO)
                            if valid(leader_book) else None,
                        leader_book_changes_30s=depth,
                        leader_book_reaction_observed=bool(depth),
                        leader_trade_source_time=trade.get("source_time"),
                        leader_trade_received_time=trade.get("received_time"))
        for follower in family.followers:
            change = self.movement(follower.yes, now)
            changed_at = self.price_changes.get(follower.yes)
            age = now - changed_at if changed_at is not None else None
            # Compare price age with the latest trade, not a required leader price change.
            gap = self.last_trade_time[leader] - changed_at if changed_at is not None else None
            token = follower.yes if direction > 0 else follower.no if direction < 0 else None
            book = books.get(token)
            follower_book = books.get(follower.yes)
            rejected = []
            if now - self.started < self.s.warmup_seconds:
                rejected.append("warmup")
            if not all_valid:
                rejected.append("incomplete_or_invalid_books")
            if activity["count"] < self.s.min_trades:
                rejected.append("insufficient_trade_count")
            if activity["volume"] < self.s.min_volume:
                rejected.append("insufficient_volume")
            if abs(imbalance) < self.s.min_imbalance:
                rejected.append("insufficient_directional_imbalance")
            if features["baseline_observed_seconds"] <= 0:
                rejected.append("insufficient_baseline_observation")
            if features["burst_score"] < self.s.min_burst_score:
                rejected.append("insufficient_activity_acceleration")
            if self.s.min_move_pp > 0 and (move is None or direction * move < self.s.min_move_pp):
                rejected.append("optional_price_confirmation")
            if move is not None and move != 0:
                if change is None:
                    rejected.append("insufficient_follower_price_history")
                elif abs(change) > abs(move) * self.s.max_follower_fraction:
                    rejected.append("follower_already_moved")
            if age is None or age < self.s.min_price_age_seconds:
                rejected.append("follower_price_not_stale")
            if gap is None or gap < self.s.min_age_gap_seconds:
                rejected.append("insufficient_trade_price_age_gap")
            if follower.id in self.positions:
                rejected.append("position_already_open_or_pending")
            if now < self.cooldowns.get(follower.id, 0):
                rejected.append("cooldown")
            reserved = sum((x.cost if x.state == "open" else self.s.cash_per_position
                            for x in self.positions.values()), ZERO)
            if reserved + self.s.cash_per_position > self.s.total_cash:
                rejected.append("portfolio_cash_limit")
            # Spreads are research features. max_spread remains an execution filter
            # on the token we would actually purchase, never on the leader book.
            if valid(book):
                if book.spread > self.s.max_spread:
                    rejected.append("spread_limit")
                if sweep(book.asks, self.s.shares) is None:
                    rejected.append("insufficient_ask_depth")
            elif direction:
                rejected.append("invalid_entry_book")
            self.candidate_sequence += 1
            candidate_id = self.candidate_sequence
            counts = self.candidate_stats[(family.id, family.event, follower.id)]
            counts["total"] += 1
            counts["rejected" if rejected else "qualified"] += 1
            for reason in rejected:
                counts["rejection_reasons"][reason] += 1
            self.event("burst_candidate", now, candidate_id=candidate_id,
                       group=family.id, event=family.event, leader_market=family.leader.id,
                       leader_slug=family.leader.slug, leader_token=leader,
                       follower=follower.id, follower_slug=follower.slug, token=token,
                       direction="up" if direction > 0 else "down" if direction < 0 else "neutral",
                       **features, follower_move_pp=change, follower_quote_age_seconds=age,
                       age_gap_seconds=gap,
                       follower_bid=follower_book.best_bid.price if valid(follower_book) else None,
                       follower_ask=follower_book.best_ask.price if valid(follower_book) else None,
                       follower_spread=follower_book.spread if valid(follower_book) else None,
                       entry_spread=book.spread if valid(book) else None,
                       qualified_for_paper_trade=not rejected,
                       rejection_reason=rejected[0] if rejected else None, rejection_reasons=rejected,
                       position_id=self.sequence + 1 if not rejected else None)
            if rejected:
                continue
            self.sequence += 1
            p = Position(self.sequence, family.id, family.event, follower.id, token,
                         "up" if direction > 0 else "down", now, now + self.s.entry_latency_seconds)
            self.positions[follower.id] = p
            self.cooldowns[follower.id] = now + self.s.cooldown_seconds
            self.counters(p)["detected"] += 1
            self.record("signal", p, now, candidate_id=candidate_id, leader_token=leader,
                        follower_slug=follower.slug, leader_move_pp=move, follower_move_pp=change,
                        volume=activity["volume"], burst_score=features["burst_score"], signed_imbalance=imbalance,
                        price_age_seconds=age, age_gap_seconds=gap, spread=books[token].spread,
                        reasons=["trade_burst", "aligned_flow", "follower_stale"])
            if self.s.entry_latency_seconds == 0:
                self.enter(p, now, books[token])

    def enter(self, p, now, book):
        if not valid(book) or book.spread > self.s.max_spread:
            self.reject(p, now, "invalid_book_or_spread")
            return
        # Size to budget using worst consumed price, then check exact sweep cost.
        shares = self.s.shares
        fill = sweep(book.asks, shares)
        if fill is None:
            self.reject(p, now, "insufficient_ask_depth")
            return
        allowance = shares * self.s.slippage_per_share
        if fill[0] + allowance > self.s.cash_per_position:
            unit = fill[1][-1]["price"] + self.s.slippage_per_share
            shares = (self.s.cash_per_position / unit).quantize(D("0.01"), rounding=ROUND_DOWN)
            fill = sweep(book.asks, shares) if shares > 0 else None
            allowance = shares * self.s.slippage_per_share
        if fill is None or not shares:
            self.reject(p, now, "insufficient_cash")
            return
        p.state, p.shares, p.cost, p.opened = "open", shares, fill[0] + allowance, now
        self.counters(p)["opened"] += 1
        self.record("entry", p, now, shares=shares, cost=p.cost, levels=fill[1],
                    slippage_allowance=allowance, sell_available_at=now + self.s.sell_delay_seconds,
                    book_source_time=book.updated_at, book_received_time=book.received_at)

    def manage(self, now, books):
        for p in list(self.positions.values()):
            book = books.get(p.token)
            if p.state == "pending":
                if now - p.detected >= self.s.entry_timeout_seconds:
                    self.reject(p, now, "entry_timeout")
                elif now >= p.due and valid(book):
                    self.enter(p, now, book)
                continue
            if not valid(book) or now < p.opened + self.s.sell_delay_seconds:
                continue
            fill = sweep(book.bids, p.shares)
            if fill is None:
                continue
            allowance = p.shares * self.s.slippage_per_share
            proceeds = max(ZERO, fill[0] - allowance)
            pnl = proceeds - p.cost
            reason = ("max_hold" if now - p.opened >= self.s.max_hold_seconds else
                      "stop_loss" if pnl <= -self.s.stop_loss else
                      "profit" if pnl >= self.s.min_profit else None)
            if p.exit_due is None:
                if reason is None:
                    continue
                p.exit_due, p.exit_reason = now + self.s.exit_latency_seconds, reason
                self.record("exit_requested", p, now, reason=reason, quoted_pnl=pnl, due=p.exit_due)
            if now < p.exit_due:
                continue
            if p.exit_reason == "profit" and reason is None:
                self.record("exit_cancelled", p, now, reason="profit_disappeared", quoted_pnl=pnl)
                p.exit_due, p.exit_reason = None, None
                continue
            reason = reason or p.exit_reason
            stats = self.counters(p)
            stats["closed"] += 1
            stats["wins"] += int(pnl > 0)
            stats["losses"] += int(pnl < 0)
            stats["closed_pnl"] += pnl
            stats["hold_seconds"] += now - p.opened
            self.record("exit", p, now, reason=reason, shares=p.shares, proceeds=proceeds,
                        pnl=pnl, levels=fill[1], slippage_allowance=allowance,
                        holding_seconds=now - p.opened, book_source_time=book.updated_at,
                        book_received_time=book.received_at)
            del self.positions[p.market]
            self.cooldowns[p.market] = max(self.cooldowns.get(p.market, 0), now + self.s.cooldown_seconds)

    def summary(self, books, *, healthy=True):
        groups = []
        for (group, event, follower), stats in self.stats.items():
            unresolved = sum(p.state == "open" and (p.group, p.event, p.market) == (group, event, follower)
                             for p in self.positions.values())
            groups.append(dict(group=group, event=event, follower=follower, **stats,
                               candidates=dict(self.candidate_stats[(group, event, follower)]),
                               unresolved=unresolved,
                               mean_hold_seconds=stats["hold_seconds"] / stats["closed"] if stats["closed"] else None))
        opened = []
        for p in self.positions.values():
            book = books.get(p.token)
            fill = sweep(book.bids, p.shares) if p.state == "open" and healthy and valid(book) else None
            mark = max(ZERO, fill[0] - p.shares * self.s.slippage_per_share) - p.cost if fill else None
            opened.append({**asdict(p), "liquidation_pnl": mark})
        return {"hypothetical": True, "fees": "0", "strategy_version": 2, "by_follower": groups,
                "candidate_count": sum(s["total"] for s in self.candidate_stats.values()),
                "qualified_candidate_count": sum(s["qualified"] for s in self.candidate_stats.values()),
                **{name: sum(s[name] for s in self.stats.values())
                   for name in ("detected", "rejected", "opened", "closed", "wins", "losses")},
                "unresolved": sum(p.state == "open" for p in self.positions.values()),
                "closed_pnl": sum((s["closed_pnl"] for s in self.stats.values()), ZERO),
                "open_positions": opened}

    def finish(self, now, utc):
        self.utc = utc
        for p in list(self.positions.values()):
            if p.state == "pending":
                self.reject(p, now, "session_ended_before_entry")
