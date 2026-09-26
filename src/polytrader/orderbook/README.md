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

`service.healthy` exposes transport health; also check each captured snapshot's
`status == "live"`. `service.continuity` increments on feed invalidation, including
failures hidden by coalesced stale/live notifications. Consumers measuring observed
opportunity lifetimes should close their intervals when that counter changes.
Quote age alone does not determine connection health.

For mixed selections where some tokens may no longer have books,
`OrderBookService(..., allow_missing_snapshots=True)` keeps other books running.
Missing initial snapshots become stale once the deadline is checked on incoming
messages/heartbeats; a later full snapshot recovers them. The default remains a
timeout/reconnect when initial snapshots are missing. The connection's PONG timeout
still applies in either mode.

Selection is fixed per session. To add a newly listed date or switch events,
resolve the selection again and start a new service. Newly listed markets are
not automatically discovered during a session.

## CLI

### Compact recording and offline replay

```powershell
# Default: one-second sampling, one hour maximum, 10 MiB file cap.
# Creates a new UTC-named file under data/orderbooks/.
polytrader orderbook "<event-slug>" --record

# Keep a small, ten-minute sample of selected outcomes.
polytrader orderbook "<event-slug>" --outcome Yes --record data/orderbooks/session.jsonl --interval 2 --duration 600 --max-mb 5

# Replay locally at 10x speed, or inspect immediately with no delays.
polytrader replay data/orderbooks/session.jsonl --speed 10
polytrader replay data/orderbooks/session.jsonl --instant

# Replay in the existing viewer, on a separate port from a running live view.
polytrader replay data/orderbooks/session.jsonl --serve --port 8766 --speed 10
```

Recording saves **best bid/ask prices, quantities, status, and source update time**.
Spreads are reproduced exactly from the decimal prices. It omits full depth and
trades. Event/market metadata appears once in a versioned JSON Lines header; rows
refer to tokens by a compact index. At each interval, only tokens whose best
prices, sizes, or status changed are written. Changes deeper in the book do not
add rows. A quiet session therefore needs very little storage.

Defaults are `interval=1` second, `duration=3600` seconds and a hard 10 MiB file
limit, including header and end marker. Recording stops at the first limit;
files are never overwritten. Duration starts after discovery and includes feed
initialization. Ctrl+C flushes an interrupted end marker; complete rows also
remain readable after abrupt termination. A missing end marker or partial final
line is reported as `incomplete` on replay. Other corrupt complete rows fail
clearly. An invalid/disconnected feed is retained as initializing/stale/unavailable
state instead of silently treating old prices as live.

Each sample records elapsed monotonic time, and the header records its UTC start.
Replay restores quotes for every token, carrying unchanged quotes forward to the
next recorded change. The end marker preserves the quiet tail of the session and
the stop reason (`duration`, `size_limit`, `interrupted`, `source_complete`, or
`error`). Source `updated_at` is from the last saved quote observation and is not
advanced by omitted depth-only updates. Brief movements and status changes between
sample times are not captured: this format cannot reconstruct tick-by-tick trading
or full-depth books.

Python API:

```python
import asyncio
from polytrader.orderbook import record_orderbooks, load_recording, replay_orderbooks

async def record():
    return await record_orderbooks(
        "<event-slug>", output="data/orderbooks/session.jsonl",
        interval=1, duration=600, max_bytes=5 * 1024 * 1024,
    )

result = asyncio.run(record())  # In a notebook: result = await record()
print(result.path, result.bytes_written, result.reason)

recording = load_recording(result.path)
print(recording.metadata["event"])
for frame in recording.frames():  # Immediate, streaming iteration; no network.
    for token, quote in frame.quotes.items():
        print(frame.recorded_at, token, quote.best_bid, quote.best_ask, quote.spread)

async def playback():
    async for frame in replay_orderbooks(recording, speed=10):
        print(frame.to_dict())
```

If a bot already maintains an `OrderBookService`, use
`await record_quotes(service.collection, output, interval=1, duration=600)` inside
its service context to reuse the connection. This samples the collection without
taking notifications away from its consumer. The lower-level `QuoteRecorder`
context manager exposes `sample(collection, elapsed_seconds)` and `finish()` for
custom scheduling; its caller is responsible for scheduling and duration limits.
`sample()` returns `False` when the next frame will not fit the byte cap.

The replay viewer is labelled **Recorded bid / ask**, displays recorded time,
and offers restart and speed controls. It shows only the recorded best levels.
It makes no calls to Polymarket and exposes replay state through the same local
`/api/books` and `/api/stream` endpoints. Replay server event switching is disabled;
`POST /api/replay` with `{"speed":10}` restarts the loaded recording.

Recording, live viewing, and CLI watching are separate command modes. To run a
live viewer while recording from the CLI, start a second command with the same
event; it will open its own stream. Use `record_quotes` for sharing a Python service.

### Live visualisation and a shared feed

```powershell
polytrader orderbook --serve
# Or start with a selected event:
polytrader orderbook "<event-slug>" --serve
```

Open **http://127.0.0.1:8765**. Paste an event URL or slug to switch events.
The page shows live bids, asks, share quantities, spreads, connection status,
and time since each book changed. Select a row to see its top 10 bid/ask levels.
Prices are displayed in cents; the Python API and JSON retain prices from 0 to 1.
Use `--port 8766` to run another independent event feed. Stop the server with Ctrl+C.

Book changes include resting orders being added or cancelled, and trades that
change depth. A trade is not required for bid/ask updates. A change away from the
best prices may update depth without changing the displayed best bid or ask.

The server shares one maintained event feed with any number of local clients:

| Endpoint | Result |
|---|---|
| `GET /api/books` | Latest event state and all selected books as JSON |
| `GET /api/stream` | Server-sent events (SSE), each `data:` frame containing that same JSON shape |
| `POST /api/event` | Switch the shared event using `{"event":"<event-slug>"}` and `Content-Type: application/json` |

Every browser or program receives its own copy of the latest state; clients do
not take updates away from each other. An event switch affects all attached
clients. The server binds to this computer's loopback interface only. It does
not enable cross-origin browser access; command-line programs can connect directly.

```powershell
curl.exe -N http://127.0.0.1:8765/api/stream
```

Python, using only the standard library:

```python
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8765/api/stream", timeout=15) as response:
    for line in response:
        if not line.startswith(b"data: "):
            continue
        state = json.loads(line[6:])
        for book in state.get("books", []):  # Heartbeats have an empty payload.
            if book["status"] == "live":
                print(book["market"]["outcome"], book["best_bid"], book["best_ask"])
```

The HTTP feed publishes the current state up to four times per second while
the underlying book service processes received updates continuously. It is for
current quotes and visualisation, not lossless recording or latency-sensitive
execution. Check book `status`, quote `updated_at`, and envelope `published_at`.
On client disconnection, treat cached quotes as stale and reconnect. SSE clients
receive the newest full state after reconnect; missed updates are not replayed.

For a Python program that needs notifications without the dashboard's 250 ms
sampling interval, use `watch_orderbooks()` or `OrderBookService` as above.
`OrderBookService.updates()` has one consumer; fan out within your application
or use the shared HTTP server for multiple separate programs.

### JSON commands

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
- `viewer.py` / `viewer.html`: local live visualisation and shared HTTP/SSE access.
- `recording.py`: bounded best-quote storage, lazy reading, and timed offline replay.

The bot imports this package; orderbook code does not import the bot. Other
transports can translate messages into `OrderBook.replace()` / `apply()` calls.

Protocol references: [REST books](https://docs.polymarket.com/api-reference/market-data/get-order-book),
[market channel](https://docs.polymarket.com/api-reference/wss/market),
[heartbeats](https://docs.polymarket.com/market-data/realtime-data).
