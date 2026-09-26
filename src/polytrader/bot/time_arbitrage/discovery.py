"""Explicit market/event resolution and one-token-one-book registry."""

from datetime import datetime, timezone
from decimal import Decimal
from itertools import combinations
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlparse

from polytrader.data.client import DataError, GAMMA_URL
from polytrader.data.discovery import parse_market
from polytrader.orderbook import OrderBookClient
from .models import Pair, ResolvedMarket, Universe


class ScannerClient(OrderBookClient):
    json_float = Decimal

    def get_market(self, slug):
        return self.get_json(f"{GAMMA_URL}/markets/slug/{quote(slug, safe='')}")

    def get_clob_info(self, condition_id):
        return self.get_json(f"https://clob.polymarket.com/clob-markets/{quote(condition_id, safe='')}")

    def get_fee(self, token):
        return self.get_json("https://clob.polymarket.com/fee-rate", token_id=token)


def reference(entry):
    value = entry.ref.strip()
    kind, selector = "bare", entry.market
    if "://" in value:
        parsed = urlparse(value)
        parts = parsed.path.strip("/").split("/")
        if (parsed.scheme != "https" or parsed.hostname not in {"polymarket.com", "www.polymarket.com"}
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError(f"Unsupported Polymarket URL: {entry.ref}")
        if len(parts) == 2 and parts[0] in {"event", "market"}:
            kind, value = parts
        elif len(parts) == 3 and parts[0] == "event":
            kind, value = parts[:2]
            if selector is not None and selector != parts[2]:
                raise ValueError("Child URL and explicit market selector disagree")
            selector = parts[2]
        else:
            raise ValueError(f"Unsupported Polymarket URL path: {entry.ref}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"Invalid market/event slug: {value!r}")
    if kind == "market" and selector is not None:
        raise ValueError("market selector applies to event references only")
    return kind, value, selector


def _optional(fetch, value):
    try:
        return fetch(value)
    except DataError as exc:
        if isinstance(exc.__cause__, HTTPError) and exc.__cause__.code == 404:
            return None
        raise


def resolve_entry(client, entry, cache):
    key = reference(entry)
    if key in cache:
        return cache[key]
    kind, slug, selector = key
    market = _optional(client.get_market, slug) if kind in {"market", "bare"} and selector is None else None
    event = _optional(client.get_event, slug) if kind in {"event", "bare"} else None
    event_market = None
    if event is not None:
        rows = event.get("markets")
        if not isinstance(rows, list):
            raise DataError("Event is missing its market array")
        if selector is not None:
            rows = [r for r in rows if selector in {str(r.get("id")), r.get("slug")}]
        else:
            eligible = [r for r in rows if r.get("closed") is not True and r.get("active") is not False]
            rows = eligible or rows  # A known closed single market is resolved, then blocked.
        if len(rows) != 1:
            raise ValueError(f"Event {slug!r} requires an explicit, unique child market selector")
        event_market = rows[0]
    if market is not None and event_market is not None and market.get("conditionId") != event_market.get("conditionId"):
        raise ValueError(f"Ambiguous slug {slug!r}: market and event resolve differently; use an explicit URL")
    raw = market if market is not None else event_market
    if raw is None:
        raise ValueError(f"Unknown market/event reference: {entry.ref}")
    parsed = parse_market(raw)
    tokens = {label.casefold(): token for label, token in parsed.tokens.items()}
    if set(tokens) != {"yes", "no"}:
        raise ValueError(f"{parsed.slug} must be an unambiguous binary Yes/No market")
    condition = raw.get("conditionId")
    if not isinstance(condition, str) or not condition or not parsed.id:
        raise DataError("Missing canonical market/condition identity")
    result = ResolvedMarket(parsed.id, condition, parsed.slug, parsed.question,
                            tokens["yes"], tokens["no"], raw, datetime.now(timezone.utc))
    cache[key] = result
    return result


def resolve_universe(config, client):
    cache, markets, chains, pairs, tokens = {}, {}, {}, [], {}
    for chain in config.chains:
        if not chain.enabled:
            continue
        resolved = tuple(resolve_entry(client, entry, cache) for entry in chain.markets)
        if len({m.condition_id for m in resolved}) != len(resolved) or len({m.id for m in resolved}) != len(resolved):
            raise ValueError(f"Duplicate market aliases in chain {chain.id}")
        chains[chain.id] = resolved
        for market in resolved:
            old = markets.get(market.condition_id)
            if old and (old.id, old.yes, old.no) != (market.id, market.yes, market.no):
                raise DataError("Conflicting market/token identity across references")
            markets[market.condition_id] = market
            for token in (market.yes, market.no):
                if token in tokens and tokens[token] != market.condition_id:
                    raise DataError("One outcome token maps to multiple markets")
                tokens[token] = market.condition_id
        for i, j in combinations(range(len(resolved)), 2):
            pairs.append(Pair(chain.id, resolved[i], resolved[j], chain.markets[i], chain.markets[j]))
    reverse = {t: tuple(p for p in pairs if t in p.tokens) for t in tokens}
    return Universe(markets, chains, tuple(pairs), tuple(tokens), reverse)
