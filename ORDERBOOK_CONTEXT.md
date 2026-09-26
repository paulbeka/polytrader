# Polytrader orderbook: AI context and operating guide

This document is a standalone handoff for an AI working with this repository.
It describes the implemented functionality as of September 26, 2026. Check the
current source and CLI help if the implementation has changed. Do not assume a
server or recorder from an earlier conversation is still running.

## Purpose and scope

`polytrader.orderbook` is a reusable Python package for public Polymarket data.
It supports snapshots, live order books, a local browser viewer, shared feeds,
compact recording, and offline replay. It does not place orders or manage wallets.
No account or API key is required. Importing the package starts no connections.

It belongs to the overall tool, not to a strategy or bot. Bots, notebooks, and
other programs import the same package.

An event contains markets, and each market contains outcome tokens. Maintain one
book per token. For example, November, December, and January Yes/No markets have
six books. Dates and outcome names are metadata, not hardcoded assumptions.
Token IDs are the stable book keys. Sports and other outcome labels work too.

## Environment and setup

This checkout is at `C:\Workspace\polytrader`; commands below run from that root.
The project requires Python 3.11+. Core snapshots and offline replay use the
standard library. Live streaming/recording requires the optional `live` extra.

```powershell
Set-Location C:\Workspace\polytrader

# If .venv is missing, create it first:
python -m venv .venv

# Install the project and its live-feed dependency.
.\.venv\Scripts\python.exe -m pip install -e ".[live]"

.\.venv\Scripts\python.exe -m polytrader orderbook --help
.\.venv\Scripts\python.exe -m polytrader replay --help
```

Using the explicit interpreter avoids PowerShell activation restrictions.
After activating/installing the environment, `polytrader ...` is equivalent to
`.\.venv\Scripts\python.exe -m polytrader ...`.

## Main CLI workflows

Replace angle-bracket placeholders with real values. Use a top-level event URL
(`https://polymarket.com/event/<event-slug>`) or a bare event slug. Select child
markets separately with `--market`.

### Discover and inspect

```powershell
# List child markets, outcome labels, and token IDs first.
.\.venv\Scripts\python.exe -m polytrader markets "<event-slug>"

# One REST snapshot of every eligible market/outcome in the event.
.\.venv\Scripts\python.exe -m polytrader orderbook "<event-slug>"

# Restrict to several markets and one outcome.
.\.venv\Scripts\python.exe -m polytrader orderbook "<event-slug>" --market "<market-a>" --market "<market-b>" --outcome Yes

# Bypass discovery and address an outcome token directly.
.\.venv\Scripts\python.exe -m polytrader orderbook --token-id "<token-id>"
```

Markets accept slugs or IDs; outcome labels are case insensitive. Repeat
`--market`, `--outcome`, or `--token-id` as needed. Use either an event with
market/outcome selectors, or direct token IDs. The default outcome selection is
all outcomes, not just Yes.

### Live feed and visualisation

```powershell
# Stream JSON lines to stdout until Ctrl+C.
.\.venv\Scripts\python.exe -m polytrader orderbook "<event-slug>" --watch

# Start a local viewer with an event selector.
.\.venv\Scripts\python.exe -m polytrader orderbook --serve

# Or start with a particular event and port.
.\.venv\Scripts\python.exe -m polytrader orderbook "<event-slug>" --serve --port 8765
```

Open `http://127.0.0.1:8765`. The page shows best bid/ask, quantities, spread,
status, update age, and the top ten price levels for a selected outcome. The
viewer displays prices in cents; Python/JSON prices are decimals from 0 to 1.
Stop the server with Ctrl+C. If the port is occupied, use another port instead
of terminating an unknown process.

An example event used during development was `russia-x-ukraine-ceasefire-by`.
Rediscover its markets before using it: market dates, eligibility, and liquidity
can change. Do not assume previous quotes remain current.

### Compact recording

```powershell
# Automatic unique UTC filename under data/orderbooks/.
.\.venv\Scripts\python.exe -m polytrader orderbook "<event-slug>" --record

# Example: ten minutes, every two seconds, capped at 5 MiB.
# Use a new filename; an existing session.jsonl will not be overwritten.
.\.venv\Scripts\python.exe -m polytrader orderbook "<event-slug>" --record data/orderbooks/new-session.jsonl --interval 2 --duration 600 --max-mb 5
```

| Setting | Default / behaviour |
|---|---|
| Sampling interval | 1 second; minimum 0.1 seconds |
| Duration | 3,600 seconds, starting after discovery and including stream initialization |
| File size | 10 MiB, including metadata and end marker |
| Saved data | Best bid/ask prices, their share quantities, status, source update time, and reason |
| Deduplication | At each sample, save only tokens whose best quotes, sizes, or status/reason changed |
| Stop condition | First duration/size limit, source completion/error, or interruption |
| Existing files | Refuse to overwrite |

Spreads are reconstructed exactly from decimal prices. Full depth and trades
are not recorded. A deeper-book change alone adds no row. Brief movements or
status changes between samples may be missed. This is sampled quote history,
not a tick-by-tick archive.

The JSON Lines file has a versioned header containing event/market metadata,
sample rows containing changed quotes indexed by token, and an end marker.
Elapsed sample time is monotonic; the header anchors it to UTC. The end marker
preserves the quiet tail and stop reason. Use the provided reader to reconstruct
state rather than treating each raw line as a complete event snapshot.

Ctrl+C writes an interrupted end marker. After abrupt termination, complete
rows remain readable; a missing end marker or partial final line produces an
`incomplete` terminal frame. Malformed complete records raise `DataError`.

### Offline replay

```powershell
# Reproduce recorded timing at 10x speed as JSON lines.
.\.venv\Scripts\python.exe -m polytrader replay data/orderbooks/new-session.jsonl --speed 10

# Inspect all reconstructed frames immediately, without waiting.
.\.venv\Scripts\python.exe -m polytrader replay data/orderbooks/new-session.jsonl --instant

# Visual replay; use 8766 if a live viewer already occupies 8765.
.\.venv\Scripts\python.exe -m polytrader replay data/orderbooks/new-session.jsonl --serve --port 8766 --speed 10
```

Replay does not contact Polymarket. The viewer is labelled **Recorded bid / ask**
and has restart/speed controls. It shows only the recorded best level, not full
depth. Replay frames preserve the historical status: a recorded `live` status
does not make a replayed quote a current live price.

`--watch`, `--serve`, and `--record` are mutually exclusive CLI modes. Viewing
and recording in separate commands creates separate upstream streams. To record
from an existing Python service without another connection, use `record_quotes`.

## Python API

### Snapshots and current state

```python
from polytrader.orderbook import fetch_orderbooks

books = fetch_orderbooks("<event-slug>", outcomes="Yes")
rows = books.summary()

for token_id, book in books.books.items():
    market = books.markets[token_id]
    print(market.question, market.outcome, book.status,
          book.best_bid, book.best_ask, book.spread)
```

`OrderBooks` exposes `event`, `markets` (token → market reference), `books`
(token → snapshot), and `excluded` (market exclusions and reasons).
`.to_dict()` creates JSON-safe data. Prices and quantities serialize as strings
to preserve decimal precision.

`BookSnapshot` provides sorted `bids` and `asks`, `best_bid`, `best_ask`, `spread`,
`midpoint`, `status`, `reason`, `updated_at`, and `received_at`. A best quote is
`Level(price: Decimal, size: Decimal)`. Size means outcome shares aggregated at
that price. Missing sides and unavailable derived metrics are `None`. Returned
book snapshots do not mutate when new updates arrive.

### Live consumption

```python
import asyncio
from contextlib import aclosing
from polytrader.orderbook import watch_orderbooks

async def watch():
    async with aclosing(watch_orderbooks("<event-slug>")) as updates:
        async for update in updates:
            if update.book.status == "live":
                print(update.market_slug, update.outcome,
                      update.book.best_bid, update.book.best_ask)

asyncio.run(watch())
```

In notebooks, use `await watch()` instead of `asyncio.run()`. Use `aclosing` when
breaking early so background tasks and sockets close promptly.

For a bot needing the whole event, call `resolve_books(...)`, then use
`async with OrderBookService(selection) as service`. Its
`service.collection` contains the latest snapshots and `service.updates()` yields
notifications. Resolve before starting the event loop or use `asyncio.to_thread`.
Avoid blocking the event loop with synchronous work.

There is one consumer per `service.updates()` iterator. Notifications are
coalesced per token for slow consumers, while received depth updates continue to
be applied. Use application-level fan-out or the HTTP server for multiple consumers.

### Recording and replay in Python

```python
import asyncio
from polytrader.orderbook import record_orderbooks, load_recording, replay_orderbooks

async def capture():
    return await record_orderbooks(
        "<event-slug>", output="data/orderbooks/new-python-session.jsonl",
        interval=1, duration=600, max_bytes=5 * 1024 * 1024,
    )

result = asyncio.run(capture())  # Notebook: result = await capture()
print(result.path, result.bytes_written, result.reason)

recording = load_recording(result.path)
for frame in recording.frames():  # Lazy, immediate, offline iteration.
    for token_id, quote in frame.quotes.items():
        print(frame.recorded_at, token_id, quote.status, quote.spread)

async def playback():
    async for frame in replay_orderbooks(recording, speed=10):
        print(frame.to_dict())
```

`QuoteFrame` includes `elapsed_seconds`, `recorded_at`, `quotes`, and `end_reason`.
`QuoteSnapshot` contains best quotes and a derived spread; it does not represent
full depth. Unchanged quotes are carried forward. Source `updated_at` remains
the last saved quote observation and is not advanced by omitted depth-only changes.

To reuse a running service, call
`await record_quotes(service.collection, output, interval=1, duration=600)`
inside its context. The lower-level `QuoteRecorder` supports explicit sampling;
its caller must schedule samples, enforce duration, and stop when `sample()`
returns `False` because the byte cap is reached.

## Attaching other programs to the viewer

The local server shares one maintained event feed with independent clients.
It binds only to loopback and does not enable cross-origin browser access.

| Endpoint | Behaviour |
|---|---|
| `GET /api/books` | Latest collection as JSON |
| `GET /api/stream` | SSE current-state stream; reconnect receives latest state, not missed history |
| `POST /api/event` | Live server: `{"event":"<event-slug>"}` switches the shared event for all clients |
| `POST /api/replay` | Replay server: `{"speed":10}` restarts the loaded file |

POST requests require `Content-Type: application/json`. Replay servers reject
live-event switching. Inspect the envelope's `mode`: replay payloads have
`mode: "replay"`. Do not decide that a feed is live from a book's status alone.

```powershell
curl.exe -N http://127.0.0.1:8765/api/stream
```

The live HTTP feed publishes current state up to four times per second; the
underlying service processes incoming book messages continuously. SSE heartbeat
events have empty payloads. Consumers should handle disconnects, inspect
`published_at` and per-book status/timestamps, and mark cached quotes stale when
their connection fails. This endpoint is not a lossless execution feed.

## Correctness and operating constraints

- Updates include order additions/cancellations and trades affecting depth.
  A new trade is not required. Price-level sizes replace the previous quantity;
  size zero removes the level.
- Book status is `initializing`, `live`, `stale`, `unavailable`, or `snapshot`.
  REST observations use `snapshot`; they are not maintained live.
- Stale books can retain old depth for inspection. Check status before using it.
  Quiet books can remain live while their connection is healthy.
- One connection subscribes to selected tokens. Heartbeats run every 10 seconds;
  no pong for 30 seconds causes disconnection. Initial snapshots also have a
  default 30-second timeout. Reconnection requires fresh stream snapshots.
- Python service/watch defaults allow five retries without a healthy 60-second
  session. `max_retries=None` permits unlimited retries; the live viewer uses it.
- Multi-token observations are not an atomic exchange-wide snapshot. Each book
  has independent exchange/local timestamps.
- Closed/inactive markets, disabled books, and missing tokens are excluded with
  reasons. Unknown selectors fail rather than silently choosing another market.
- Selection is fixed per service session. Rediscover and restart for new dates.
  Switching the viewer's event restarts its selection for every attached client.
- Contract deadlines differ from quote timestamps and API end dates. Preserve
  market questions/rules; do not infer “by” versus “during” from a month label.
  Python `deadlines={market_slug: value}` supplies explicit optional metadata.
- Recordings capture only the selected markets and sampled best quotes. They
  cannot be used to reconstruct historical full-depth liquidity or every trade.

## Code map and verification

| File | Responsibility |
|---|---|
| `src/polytrader/orderbook/api.py` | Discovery/selection, snapshots, convenience live iterator |
| `src/polytrader/orderbook/models.py` | Levels, immutable snapshots, references, collection, updates |
| `src/polytrader/orderbook/book.py` | In-memory depth replacement/removal and derived quotes |
| `src/polytrader/orderbook/client.py` | Public REST/WebSocket transport and heartbeat |
| `src/polytrader/orderbook/service.py` | Background maintenance, reconnects, notification lifecycle |
| `src/polytrader/orderbook/recording.py` | Bounded writer, lazy reader, timed replay |
| `src/polytrader/orderbook/viewer.py` | Local HTTP/SSE server and live/replay controllers |
| `src/polytrader/orderbook/viewer.html` | Live and recorded-quote interface |
| `src/polytrader/cli.py` | CLI commands and options |
| `sandbox/02_orderbooks.ipynb` | Snapshot/live/recording/replay examples; network flags default off |
| `data/orderbooks/` | Local recordings; paths are relative to the process working directory |

See [the detailed orderbook guide](src/polytrader/orderbook/README.md) for more
examples. Existing local recordings may include `data/orderbooks/session.jsonl`;
inspect before use and choose new filenames for new captures.

For implementation changes, preserve the separation between reusable market
data and bot strategies. Read the working tree first and preserve unrelated edits.
Validate with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Relevant suites: `test_orderbook.py`, `test_orderbook_recording.py`, and
`test_orderbook_viewer.py`. Most tests use offline fixtures; optional transport
tests require the live dependency. A passing mocked test suite does not by itself
prove current exchange connectivity or visually verify browser layout.
