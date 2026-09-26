"""Convenient event-wide snapshots and live feeds for scripts and notebooks."""

import asyncio
from contextlib import aclosing
from copy import deepcopy

from polytrader.data import DataError, discover
from polytrader.orderbook.book import OrderBook
from polytrader.orderbook.client import OrderBookClient, timestamp
from polytrader.orderbook.models import BookSnapshot, MarketReference, OrderBooks


def _selectors(values, name):
    if values is None:
        return []
    values = [values] if isinstance(values, str) else list(values)
    if not values or any(not isinstance(v, str) or not v.strip() for v in values):
        raise ValueError(f"{name} must contain nonempty strings")
    return list(dict.fromkeys(values))


def resolve_books(event=None, *, markets=None, outcomes=None, token_ids=None,
                  deadlines=None, client=None) -> OrderBooks:
    """Discover once; selected market order is preserved. Deadlines are explicit metadata."""
    selected = _selectors(markets, "markets")
    labels = _selectors(outcomes, "outcomes")
    tokens = _selectors(token_ids, "token_ids")
    if bool(event) == bool(tokens):
        raise ValueError("Provide an event URL/slug OR token_ids")
    if tokens and (selected or labels or deadlines):
        raise ValueError("markets, outcomes and deadlines require an event")
    if tokens:
        refs = {token: MarketReference(token) for token in tokens}
        return OrderBooks({}, refs, {t: BookSnapshot(t) for t in refs})
    client = client if client is not None else OrderBookClient()
    raw_event, discovered = discover(client, event)
    if selected:
        chosen = []
        for selector in selected:
            matches = [m for m in discovered if selector in {m.id, m.slug}]
            if len(matches) != 1:
                raise ValueError(f"Market selector must match exactly one market: {selector!r}")
            if matches[0] not in chosen:
                chosen.append(matches[0])
        discovered = chosen
    deadlines = deadlines or {}
    known = {key for m in discovered for key in (m.id, m.slug)}
    if set(deadlines) - known:
        raise ValueError("Deadline overrides must reference selected market IDs or slugs")
    refs, excluded = {}, []
    for market in discovered:
        reason = None
        if market.raw.get("closed"):
            reason = "Market is closed"
        elif market.raw.get("active") is False:
            reason = "Market is inactive"
        elif market.raw.get("enableOrderBook") is False:
            reason = "Order book is disabled"
        elif not market.tokens:
            reason = "No CLOB outcome tokens"
        if reason:
            excluded.append({"market_id": market.id, "market_slug": market.slug, "reason": reason})
            continue
        wanted = {label.casefold() for label in labels}
        missing = wanted - {label.casefold() for label in market.tokens}
        if missing:
            raise ValueError(f"Unavailable outcomes in {market.slug}: {', '.join(sorted(missing))}")
        for label, token in market.tokens.items():
            if wanted and label.casefold() not in wanted:
                continue
            if token in refs:
                raise DataError(f"Token belongs to multiple selected markets: {token}")
            refs[token] = MarketReference(
                token, market.id, market.slug, market.question, label,
                market.raw.get("groupItemTitle"),
                deadlines.get(market.slug, deadlines.get(market.id)), deepcopy(market.raw),
            )
    event_info = {key: raw_event.get(key) for key in ("id", "slug", "title")}
    return OrderBooks(event_info, refs, {t: BookSnapshot(t) for t in refs}, excluded)


def fetch_orderbooks(event=None, *, markets=None, outcomes=None, token_ids=None,
                     deadlines=None, client=None) -> OrderBooks:
    """Fetch each selected token. Failed tokens have unavailable status and a reason.

    REST books have status 'snapshot': they are observations, not maintained live state.
    Prices and quantities serialize to decimal strings; no files are written.
    """
    client = client if client is not None else OrderBookClient()
    result = resolve_books(event, markets=markets, outcomes=outcomes, token_ids=token_ids,
                           deadlines=deadlines, client=client)
    for token in result.books:
        book = OrderBook(token)
        try:
            raw = client.get_book(token)
            if not isinstance(raw, dict) or raw.get("asset_id") != token:
                raise DataError("Orderbook response token does not match request")
            book.replace(raw.get("bids"), raw.get("asks"), timestamp(raw.get("timestamp")),
                         status="snapshot")
        except DataError as exc:
            book.invalidate(str(exc), status="unavailable")
        result.books[token] = book.snapshot
    return result


async def watch_orderbooks(event=None, *, markets=None, outcomes=None, token_ids=None,
                           deadlines=None, client=None, max_retries=5):
    """Yield latest per-token updates. Use contextlib.aclosing when breaking early.

    Slow consumers receive coalesced notifications; the service still applies every
    received depth update. Use OrderBookService directly to read the full collection.
    """
    from polytrader.orderbook.service import OrderBookService

    client = client if client is not None else OrderBookClient()
    collection = await asyncio.to_thread(
        resolve_books, event, markets=markets, outcomes=outcomes, token_ids=token_ids,
        deadlines=deadlines, client=client,
    )
    if not collection.books:
        raise ValueError(f"No eligible outcome books: {collection.excluded}")
    async with OrderBookService(collection, client=client, max_retries=max_retries) as service:
        async with aclosing(service.updates()) as updates:
            async for update in updates:
                yield update
