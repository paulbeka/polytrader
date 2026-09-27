# Polytrader

Foundations for Polymarket research and an eventual automation bot. The data layer
can be imported from Python scripts, notebooks, or future bot code; the CLI is
another way to call it. Currently implements public event metadata and historical
outcome prices, plus current and live outcome order books. No account, API key,
or paid service is needed. Python 3.11+; the core package has no runtime dependencies.
Live order books use the optional `live` extra.

## Setup (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` and
`.\.venv\Scripts\polytrader.exe` directly. If using uv, the equivalent setup is
`uv venv --python 3.12` followed by `uv pip install -e .`.

## Use from Python

Load a history file already downloaded by the CLI, from the repository root:

```python
from polytrader.data import load_history

history = load_history("data/lepen-history-7d.json")  # Use your own saved file.
points = history.data         # List of price-point dictionaries.
metadata = history.metadata   # Token ID, time window, market details, etc.
```

Fetch directly into Python with the same date options as the CLI:

```python
from polytrader.data import fetch_price_history

recent = fetch_price_history(
    token_id=history.metadata["token_id"],
    days=7,
    bucket_seconds=300,
)

# Or select a market and outcome within an event:
# recent = fetch_price_history(
#     "<event-slug>", market="<market-slug>", outcome="Yes",
#     start="2026-09-01", end="2026-09-08",
# )

recent.save("data/recent-history.json")  # Optional; refuses to overwrite.
```

`fetch_price_history()` returns a `PriceHistory` object without printing or
writing files. Empty responses have `history.data == []`. Invalid arguments raise
`ValueError`; API failures raise `polytrader.data.DataError`. `load_history()` reads
the existing JSON format without making network requests. Relative file paths are
relative to your Python process's working directory.

## Notebooks and analysis

Install the optional analysis dependencies:

```powershell
python -m pip install -r requirements-sandbox.txt
```

Then convert either loaded or freshly fetched history into a pandas DataFrame:

```python
df = history.to_frame()
print(df.head())
df["price"].plot(title="Outcome price", ylabel="Price")
```

The frame has a UTC `datetime` index and `timestamp`, `price`, and
`resolution_seconds` columns. Conversion sorts by time without filling gaps,
resampling, or dropping points with shared timestamps. Metadata remains available
on `history.metadata`; `.to_dict()` returns the original JSON-shaped envelope.

Start with [sandbox/01_history.ipynb](sandbox/01_history.ipynb) and select `.venv`
as its kernel in VS Code. It loads and plots local history; fresh API calls are
optional. See [sandbox/README.md](sandbox/README.md) for setup details.

`src/polytrader/bot/` is the place for future automation code. It currently contains
only a package placeholder and guidance; reusable bot code can import the data
layer directly, just like the notebook.

## Current and live order books

The standalone `polytrader.orderbook` package maintains a separate bid/ask book
for every selected outcome token across an event's markets, including quantity
at each price. Import it from notebooks, scripts, or future bot code.

```python
from polytrader.orderbook import fetch_orderbooks

books = fetch_orderbooks("<event-slug>")
rows = books.summary()  # Best bid, ask, sizes, spread, and status per outcome.
```

```powershell
polytrader orderbook "<event-slug>"
python -m pip install -e ".[live]"
polytrader orderbook "<event-slug>" --watch
polytrader orderbook "<event-slug>" --serve
```

Use `--market` and `--outcome` to filter, or repeat `--token-id` for direct token
access. See [the orderbook guide](src/polytrader/orderbook/README.md) for streaming
Python examples, date metadata, and bot integration, and
[the notebook](sandbox/02_orderbooks.ipynb) for a bounded live example.

`--serve` provides a small live view at http://127.0.0.1:8765 with bid/ask prices,
quantities, spreads, and depth bars. Omit the event to choose it in the page.
Other programs can share its feed through `/api/books` (JSON snapshot) or
`/api/stream` (SSE). The view/feed publishes current state up to four times per
second while the underlying service processes book updates continuously.

Record a compact history of best prices, quantities and spreads, then replay it
offline:

```powershell
polytrader orderbook "<event-slug>" --record data/orderbooks/session.jsonl --duration 600
polytrader replay data/orderbooks/session.jsonl --serve --port 8766 --speed 10
```

Recording defaults to one-second sampling, saves only changed best quotes, and
stops after one hour or 10 MiB. It preserves status and timestamps, excludes full
depth, and never overwrites existing files. See the orderbook guide for Python
recording/replay APIs, other limits, and interrupted-file recovery.

## Find markets and outcome tokens

An event can contain several markets. List them before choosing one:

```powershell
polytrader markets "https://polymarket.com/event/<event-slug>"
```

Replace angle-bracket placeholders with real values. A bare event slug also works.
The command prints each market's question, slug, ID, and outcome token IDs.
Use the top-level `/event/<event-slug>` URL; pass a child market separately with
`--market`. Market selection accepts a slug or ID, and is optional only when the
event has exactly one market. Outcome labels are case-insensitive; the default
is `Yes`. For sports or other non-Yes/No markets, use the exact label from the list.

## Fetch historical prices

Last seven days, ending at the time the command starts:

```powershell
polytrader history "<event-slug>" --market "<market-slug>" --outcome Yes --days 7 --output data/history-7d.json
```

An explicit date range (September 1 through September 7):

```powershell
polytrader history "<event-slug>" --market "<market-slug>" --outcome No --start 2026-09-01 --end 2026-09-08 --output data/history-range.json
```

From a date up to now, or directly from an outcome token ID:

```powershell
polytrader history "<event-slug>" --market "<market-slug>" --start 2026-09-01
polytrader history --token-id "<token-id>" --days 0.5 --bucket-seconds 300
```

- Use either `--days` or `--start` with optional `--end`. Fractional days work.
- Dates without times mean midnight UTC. Start is inclusive; end is exclusive.
  To include all of September 7, use `--end 2026-09-08`.
- ISO timestamps also work: `2026-09-01T12:00:00Z` or
  `2026-09-01T13:00:00+01:00`. Times without an offset use UTC. Future end dates
  and reversed/empty ranges are rejected.
- `--bucket-seconds` requests sampling resolution, between 60 and 86400 seconds.
  Omit it to let the API choose an available resolution per request. For a
  consistent requested resolution across a long range, supply it explicitly.
- Ranges longer than the API's 15-day request limit are split automatically;
  every page is fetched. Results are sorted and duplicate timestamp/resolution
  pairs removed. A failed request stops the command with a nonzero exit code.
- JSON goes to stdout unless `--output` is supplied. Parent directories are
  created; existing files are never overwritten. Messages go to stderr.

The JSON includes the token ID, requested UTC window, fetch time, source URL,
and price points (`timestamp` in Unix seconds, `price` from 0 to 1, and
`resolution_seconds`). Fetching by event also includes the selected market's
raw metadata and resolution rules. Direct token lookup omits market metadata.

These are historical price observations, not historical order books or a
guaranteed evenly spaced series. Fine-grained history has limited retention;
an older window with an unavailable requested resolution may return no points
or partial coverage. No missing prices are filled in. Always inspect each point's
`resolution_seconds`; zero denotes a settlement point. Shorter chunks can have
different automatically chosen resolutions and terminal observations between
bucket boundaries. A valid empty response produces an empty `data` array and a
message rather than invented prices.

For older history, try `--bucket-seconds 10800` (3 hours) or `43200` (12 hours).
API details: [event lookup](https://docs.polymarket.com/api-reference/events/get-event-by-slug)
and [price history, pagination, and retention](https://docs.polymarket.com/api-reference/markets/get-a-tokens-price-history).

## Research dataset: resolved markets, trades and volume

Builds a local dataset under `data/research/` for backtests (needs
`pip install -r requirements-sandbox.txt`). Each step is resumable; rerunning
`trades` skips markets already saved and retries earlier failures.

```powershell
python -m polytrader.research markets --since 2025-01-01 --min-volume 100000
python -m polytrader.research trades --sample 5000 --no-updown --workers 4
python -m polytrader.research bars --freq 5min   # Any pandas frequency: 1min, 1h, 1D...
```

- `markets.parquet`: one row per resolved market with its winning outcome,
  event tags (use these as categories), dates and volume. Polymarket's `volume`
  is in **shares**; dollar volume is shares x price.
- `trades/<condition_id>.parquet`: every taker trade (second timestamps, side,
  size, price, wallet). Summed sizes match the official market volume.
- `volume_<freq>.parquet`: per-market bars with trade count, shares, USD,
  YES-equivalent VWAP and net taker flow towards YES. Up/Down markets treat Up as YES.

Load in a notebook with `pandas.read_parquet` or
`polytrader.research.pull.load_trades("data/research", condition_ids)`.

## Development

The [time-arbitrage scanner](src/polytrader/bot/time_arbitrage/README.md) lives in
`polytrader.bot`. Configure ordered, reviewed market chains to scan earlier-NO /
later-YES asks, including fee-aware sizing, full depth, and persistent opportunity
logs. It is read-only and does not place orders.

```powershell
python -m unittest discover -s tests -v
python -m polytrader --help
```

```text
src/polytrader/
    bot/                # Read-only time-arbitrage scanner and strategy code
    orderbook/          # Full-depth snapshots and live event-wide books
    cli.py              # CLI wrapper around the Python data API
    data/
        api.py          # fetch_price_history() orchestration
        client.py       # Public HTTP requests, timeouts, and errors
        dataset.py      # PriceHistory, loading/saving, optional DataFrame support
        discovery.py    # Event to markets to outcome tokens
        history.py      # UTC windows, chunking, and pagination
sandbox/
    01_history.ipynb     # Load, inspect, and plot saved history
tests/                  # Offline tests; no API calls
```

Downloaded data, local environments, and secrets are ignored by Git.

## IDEAS

- Statistical arbitrage on events that are very similar 
--> things like time for example, like events happening in order
- Oil trading? Tracking insiders?
- What about looking at one market and performing some kind of tme time analysis
---> Then find a way to replicate findings on other markets
- Maybe some way to use the orderbooks etc to do some other kind of trading strat?
- Forecast recurring markets to predict the next price

Example: arbitrage on the French presidential election?
