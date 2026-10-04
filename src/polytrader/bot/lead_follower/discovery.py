"""Resolve explicitly selected YES/NO families without inferring logical relations."""

from dataclasses import dataclass

from polytrader.orderbook.api import resolve_books
from polytrader.orderbook.models import OrderBooks


@dataclass(frozen=True)
class Market:
    id: str
    slug: str
    yes: str
    no: str


@dataclass(frozen=True)
class Family:
    id: str
    event: str
    leader: Market
    followers: tuple[Market, ...]


def prepare(config, client=None):
    families, refs, books, excluded = [], {}, {}, []
    for group in config.groups:
        collection = resolve_books(group.event, client=client)
        by_market = {}
        for ref in collection.markets.values():
            by_market.setdefault(ref.market_id, []).append(ref)
        markets = []
        for market_id, entries in by_market.items():
            tokens = {r.outcome.casefold(): r.token_id for r in entries}
            if set(tokens) == {"yes", "no"} and len(set(tokens.values())) == 2:
                markets.append(Market(market_id, entries[0].market_slug, tokens["yes"], tokens["no"]))
        def select(selector):
            matches = [m for m in markets if selector in (m.id, m.slug)]
            if len(matches) != 1:
                raise ValueError(f"{group.id}: expected one eligible YES/NO market for {selector!r}")
            return matches[0]
        leader = select(group.leader)
        followers = tuple(select(s) for s in group.followers) if group.followers else tuple(
            m for m in markets if m.id != leader.id)
        if not followers or leader in followers or len({m.id for m in followers}) != len(followers):
            raise ValueError(f"{group.id}: select distinct followers other than the leader")
        family = Family(group.id, collection.event.get("slug") or group.event, leader, followers)
        families.append(family)
        for m in (leader, *followers):
            for t in (m.yes, m.no):
                if t in refs and refs[t].market_id != collection.markets[t].market_id:
                    raise ValueError(f"Conflicting token identity: {t}")
                refs[t] = collection.markets[t]
                books[t] = collection.books[t]
        excluded.extend(collection.excluded)
    return tuple(families), OrderBooks({"strategy": "lead_follower"}, refs, books, excluded)
