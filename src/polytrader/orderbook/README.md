# Order books

`polytrader.orderbook` is a reusable public market-data tool for bots, notebooks,
scripts, and the CLI. Importing it starts no connections. No wallet or API key is
required. Each outcome token has its own book: three dated Yes/No markets have six
books. The engine also supports non-date markets and other outcome labels.

## Snapshots

```python
from polytrader.orderbook import fetch_orderbooks

books = fetch_orderbooks("<event-slug>")  # A top-level event URL also works.
print(books.summary())                   # One row per market/outcome.

# Optional selection. The supplied market order is preserved.
books = fetch_orderbooks(
    "<event-slug>",
    markets=["<november-market-slug>", "<december-market-slug>"],
    outcomes=["Yes"],                    # Default: every outcome.
    deadlines={"<november-market-slug>": "2026-11-30T23:59:59Z"},
)

for token_id, book in books.books.items():
    reference = books.markets[token_id]
    print(reference.question, reference.outcome, book.best_ask, book.spread)

books = fetch_orderbooks(token_ids=["<token-id>"])  # Direct lookup.
payload = books.to_dict()                # JSON-safe; decimals become strings.
```

`best_bid` and `best_ask` are `Level(price, size)` objects or `None`. `bids` and
`asks` contain full received price depth, sorted best first. Quantities are outcome
shares aggregated at each price. `spread` is ask minus bid; `midpoint` is their
average. Either is `None` when a side is empty. Prices and sizes use `Decimal`.
Returned book snapshots do not change after subsequent updates.

REST observations have status `snapshot`. Requests for multiple tokens are
sequential, so a collection is not an exchange-wide atomic snapshot. Each book
retains its own exchange `updated_at` and local `received_at`, both UTC.

Closed/inactive markets, disabled books, and markets without CLOB tokens appear
in `books.excluded` with reasons. Failed token requests have status `unavailable`
and a reason; other requests continue. Invalid selectors raise `ValueError`;
discovery failures raise `polytrader.data.DataError`.

Original questions, rules, and API date fields are preserved in `reference.raw`.
`deadline` is only an explicit caller-provided label, never inferred from `endDate`.
Check the rules: "by November" and "during November" are different contracts.
Market selectors accept slugs or IDs; outcome labels are case insensitive.
Use `polytrader markets <event-slug>` to find selectors.

## Live updates

```powershell
python -m pip install -e ".[live]"
```

```python
import asyncio
from contextlib import aclosing
from polytrader.orderbook import watch_orderbooks

async def watch():
    async with aclosing(watch_orderbooks("<event-slug>")) as updates:
        async for update in updates:
            print(update.market_slug, update.outcome, update.book.status,
                  update.book.best_bid, update.book.best_ask, update.book.spread)

asyncio.run(watch())  # In a notebook, use: await watch()
```

Use `aclosing` when stopping iteration early to close the connection and background
task immediately. Ctrl+C stops the CLI. No history is saved automatically.

For bot code that needs all current books:

```python
from contextlib import aclosing
from polytrader.orderbook import OrderBookService, resolve_books

# Resolve before starting the event loop, or use asyncio.to_thread.
selection = resolve_books("<event-slug>", outcomes="Yes")

async def run_bot():
    async with OrderBookService(selection) as service:
        async with aclosing(service.updates()) as updates:
            async for update in updates:
                if update.book.status != "live":
                    continue
                rows = service.collection.summary()
                # Check each relevant row's status and timestamps before using it.
```

The service applies depth messages in a background asyncio task. Slow async
consumers receive the latest pending snapshot per token: notifications are
coalesced, while every received depth change is applied. This is a current-state
feed, not a lossless recording API. Avoid blocking the event loop; use
`asyncio.to_thread` for blocking I/O.

Status transitions are `initializing` -> `live` -> `stale` on disconnect or invalid
data, then back to `live` only after a fresh stream snapshot. Old depth remains on
stale views for inspection. Resolved tokens become `unavailable`. REST snapshots
are never mixed into an active stream.

Application heartbeats are sent every 10 seconds; the connection closes after 30
seconds without a pong. Quiet markets remain live while the connection is healthy;
`live` does not mean a quote changed recently. Missing initial snapshots time out
after 30 seconds. Invalid/crossed/out-of-order depth triggers resubscription. After
five retries without a healthy 60-second session, iteration raises `DataError`.
Set `max_retries=None` for unlimited retries. `OrderBookService` also accepts
`retry_delay` and `snapshot_timeout` in seconds.

Selection is fixed per session. To add a newly listed date or switch events,
resolve the selection again and start a new service. Newly listed markets are
not automatically discovered during a session.

## CLI

```powershell
polytrader orderbook "<event-slug>"
polytrader orderbook "<event-slug>" --market "<nov-market>" --market "<dec-market>" --outcome Yes
polytrader orderbook "<event-slug>" --watch
polytrader orderbook --token-id "<token-id>" --watch
```

Snapshots print JSON. `--watch` prints JSON lines, including status changes.
Repeat `--token-id`, `--market`, or `--outcome` as needed. Watch exclusions go to
stderr. Snapshot mode exits nonzero if any requested book is unavailable or no
eligible books exist; inspect the JSON for partial results.

## Components

- `models.py`: book views, market references, event collection.
- `book.py`: network-independent level replacement/removal and derived quotes.
- `client.py`: REST, WebSocket framing, heartbeat, timestamp conversion.
- `service.py`: multiple books, background updates, reconnection, lifecycle.
- `api.py`: discovery, filters, snapshots, convenience async iterator.

The bot imports this package; orderbook code does not import the bot. Other
transports can translate messages into `OrderBook.replace()` / `apply()` calls.

Protocol references: [REST books](https://docs.polymarket.com/api-reference/market-data/get-order-book),
[market channel](https://docs.polymarket.com/api-reference/wss/market),
[heartbeats](https://docs.polymarket.com/market-data/realtime-data).
