"""Frozen price-trigger strategy v1, used only to reproduce older recordings.

New live sessions use engine.py. Do not migrate this detector to burst semantics.
"""

from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN

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
        self.prices, self.price_changes = {}, {}
        self.positions, self.cooldowns = {}, {}
        self.started, self.last_now, self.utc = None, None, None
        self.sequence = 0
        self.stats = defaultdict(lambda: dict(detected=0, rejected=0, opened=0, closed=0,
                                             wins=0, losses=0, closed_pnl=ZERO, hold_seconds=0.0))
        for family in families:
            for follower in family.followers:
                self.stats[(family.id, family.event, follower.id)]
        self.paused = True

    def event(self, kind, now, **data):
        self.emit({"type": kind, "elapsed_seconds": now, "utc": self.utc,
                   "hypothetical": True, "fees": "0", **data})

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
        if not all(valid(books.get(t)) for t in self.tokens):
            # Cancel pending entries before they can fill on a broken family.
            # Existing positions with healthy own books can still finish exits.
            self.reset(now, "incomplete_or_invalid_books", cancel_exits=False)
            self.manage(now, books)
            return
        # Existing positions can exit while signal histories are warming up.
        self.manage(now, books)
        if self.started is None:
            self.started = now
            self.paused = False
            self.event("warmup_started", now,
                       required_seconds=self.s.baseline_seconds + self.s.lookback_seconds)
        for token in self.tokens:
            book = books[token]
            price = (book.best_bid.price, book.best_ask.price)
            if self.prices.get(token) != price:
                self.price_changes[token] = now
                self.prices[token] = price
            history = self.history[token]
            if not history or history[-1][1] != book.midpoint:
                history.append((now, book.midpoint))
            while len(history) > 1 and history[1][0] <= now - self.s.lookback_seconds:
                history.popleft()
        if trade is not None and trade["token"] in self.leaders:
            self.tape[trade["token"]].append((now, trade["size"], trade["signed_size"]))
        cutoff = now - self.s.lookback_seconds - self.s.baseline_seconds
        for tape in self.tape.values():
            while tape and tape[0][0] <= cutoff:
                tape.popleft()
        if now - self.started < self.s.lookback_seconds + self.s.baseline_seconds:
            return
        for family in self.families:
            self.detect(family, now, books)

    def movement(self, token, now):
        history = self.history[token]
        if not history or history[0][0] > now - self.s.lookback_seconds:
            return None
        return (history[-1][1] - history[0][1]) * 100

    def detect(self, family, now, books):
        leader = family.leader.yes
        move = self.movement(leader, now)
        if move is None or abs(move) < self.s.min_move_pp or books[leader].spread > self.s.max_spread:
            return
        direction = 1 if move > 0 else -1
        boundary = now - self.s.lookback_seconds
        tape = self.tape[leader]
        volume = sum((v for t, v, _ in tape if t > boundary), ZERO)
        signed = sum((v for t, _, v in tape if t > boundary), ZERO)
        baseline = sum((v for t, v, _ in tape if t <= boundary), ZERO)
        expected = baseline * D(str(self.s.lookback_seconds)) / D(str(self.s.baseline_seconds))
        if volume < self.s.min_volume or expected <= 0:
            return
        ratio, imbalance = volume / expected, signed / volume
        if ratio < self.s.volume_ratio or direction * imbalance < self.s.min_imbalance:
            return
        for follower in family.followers:
            change = self.movement(follower.yes, now)
            if change is None or abs(change) > abs(move) * self.s.max_follower_fraction:
                continue
            age = now - self.price_changes[follower.yes]
            gap = self.price_changes[leader] - self.price_changes[follower.yes]
            if age < self.s.min_price_age_seconds or gap < self.s.min_age_gap_seconds:
                continue
            # A single portfolio owns each follower, even across overlapping groups.
            if follower.id in self.positions or now < self.cooldowns.get(follower.id, 0):
                continue
            token = follower.yes if direction > 0 else follower.no
            self.sequence += 1
            p = Position(self.sequence, family.id, family.event, follower.id, token,
                         "up" if direction > 0 else "down", now, now + self.s.entry_latency_seconds)
            self.positions[follower.id] = p
            self.cooldowns[follower.id] = now + self.s.cooldown_seconds
            self.counters(p)["detected"] += 1
            self.record("signal", p, now, leader_token=leader, follower_slug=follower.slug,
                        leader_move_pp=move, follower_move_pp=change, volume=volume,
                        expected_volume=expected, volume_ratio=ratio, signed_imbalance=imbalance,
                        price_age_seconds=age, age_gap_seconds=gap, spread=books[token].spread,
                        reasons=["leader_move", "unusual_volume", "aligned_flow", "follower_lag"])
            reserved = sum((x.cost if x.state == "open" else self.s.cash_per_position
                            for x in self.positions.values() if x is not p), ZERO)
            if reserved + self.s.cash_per_position > self.s.total_cash:
                self.reject(p, now, "portfolio_cash_limit")
            elif books[token].spread > self.s.max_spread:
                self.reject(p, now, "spread_limit")
            elif self.s.entry_latency_seconds == 0:
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
                               unresolved=unresolved,
                               mean_hold_seconds=stats["hold_seconds"] / stats["closed"] if stats["closed"] else None))
        opened = []
        for p in self.positions.values():
            book = books.get(p.token)
            fill = sweep(book.bids, p.shares) if p.state == "open" and healthy and valid(book) else None
            mark = max(ZERO, fill[0] - p.shares * self.s.slippage_per_share) - p.cost if fill else None
            opened.append({**asdict(p), "liquidation_pnl": mark})
        return {"hypothetical": True, "fees": "0", "by_follower": groups,
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
