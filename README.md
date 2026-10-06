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

## One-year research dataset

The research pipeline builds a versioned local dataset under `data/research/v1/`
for calibration, longshot-bias, deadline, drift, volume, convergence and wallet
studies. Install `requirements-sandbox.txt` first; Parquet output uses pyarrow and
zstd compression. All timestamps are UTC.

Run the steps in order:

```powershell
python -m polytrader.research universe
python -m polytrader.research trades --workers 4 --rps 4
python -m polytrader.research prices --workers 4 --rps 4
python -m polytrader.research compact
python -m polytrader.research validate
```

`universe` uses Gamma's keyset endpoint and always enumerates the complete eligible
universe: markets closed in the preceding 365 days with more than zero traded
shares. Recurring Up/Down crypto markets remain in `universe.parquet` with
`is_updown=true`, but are not selected. At most 50,000 other markets are selected
with fixed-seed stratified random sampling by close month, volume decile and first
event tag. `weight` is the stratum population divided by its sampled count.

Use `--limit 200` on each command for a small end-to-end smoke run. A universe run
still enumerates all metadata so the sample is not biased. Trades and prices are
written atomically as one file per market under `.staging/`; completed files are
skipped on rerun and failed markets (recorded in `failures.jsonl`) are retried.
Selection order is deterministically shuffled so a partial download is not just
the highest-volume or newest slice. Universe requests retry transient failures up
to 20 times by default; use `--retry-attempts` to change that and rerun the same
command after any terminal failure to resume from its saved keyset cursor.

### Public tables

- `universe.parquet`: every eligible market. It contains identifiers, question,
  event fields, tags, outcomes and token IDs, final outcome prices, winner and
  `cancelled`, dates/rules, neg-risk and fee fields, tick/minimum sizes, share
  volume, `is_updown`, `close_month`, `volume_decile`, `stratum`, `selected`,
  `selection_order`, and sampling `weight`. `gamma_market_*` columns and
  `raw_market_json` retain every market field returned by Gamma.
- `markets.parquet`: the selected rows from the universe used by the compacted
  dataset.
- `events.parquet`: one selected event per row, including identifiers, title,
  description, dates, category and tags. `gamma_event_*` columns and
  `raw_event_json` preserve the complete event metadata.
- `market_tags.parquet`: long-form `condition_id, tag` relationships.
- `raw/events-*.jsonl.zst`: lossless raw event responses, one JSON object per line.
- `trades/close_month=YYYY-MM/`: taker-only fills with `condition_id`, Unix-second
  `timestamp`, outcome `asset` and index, `side`, `size` (shares), `price`, proxy
  `wallet`, and transaction hash `tx`.
- `prices/close_month=YYYY-MM/`: outcome-token history with `condition_id`,
  `token_id`, `outcome`, `timestamp`, `price`, returned `resolution_seconds`, and
  the `requested_bucket_seconds` that succeeded.
- `bars_1min/`, `bars_1h/`, `bars_1d/`: only intervals containing trades. Columns
  are `condition_id`, UTC `time`, trade count, shares, USD (`size * price`),
  YES-equivalent VWAP, net taker flow towards YES, and YES-equivalent OHLC. A NO
  fill at price `p` is represented as YES at `1-p`; gaps are never filled.
- `manifest.json`: parameters, endpoints, step timings, row counts, Git commit and
  latest validation summary. `validation.json` contains the detailed report.

Load a table into pandas:

```python
from polytrader.research.load import load_frame

markets = load_frame("markets")
trades = load_frame("trades", filters=[("close_month", "=", "2026-09")])
```

`duckdb_connection()` in the same module is optional and creates views over every
available table; install `duckdb` separately if wanted. It is not required by the
pipeline.

### Runtime, storage and API limits

Metadata enumeration is comparatively fast. Trade and price stages make many
requests and a 50,000-market run is expected to take hours to days depending on
market age, activity, API latency and the configured global request rate. Use the
200-market smoke run to measure this machine and API, then scale its elapsed time
and disk bytes by `50,000 / 200`; price retention and trade density make this an
estimate, not a guarantee.

Gamma `volume`/`volumeNum` is **shares**, not dollars. Fine price resolutions have
limited retention, so the downloader tries 60 seconds, 300 seconds, one hour, one
day, then the API's automatic resolution, and records the actual resolution on
every point. A successful empty result is recorded explicitly rather than filled.
There is no public historical bid/ask or full order-book dataset; the deprecated
Goldsky order-book subgraph is not used. The validation command reports, but does
not abort on, trade/Gamma volume differences over 1%, duplicate fills, missing
artifacts, winner inconsistencies, coverage and timestamp outliers.

## Development

The [lead/follower paper trader](src/polytrader/bot/lead_follower/README.md) detects
directional trade bursts and lagging related deadline contracts, then tracks
hypothetical entries and first profitable bid-side exits. It includes slippage,
excludes fees, and records losses and unresolved positions alongside profits.

```powershell
python -m polytrader.bot.lead_follower "<event-slug>" --leader "<leader-market-slug>" --duration 3600
```

Use repeatable `--follower` filters, `--validate`, or `--config` for multiple events.
Session logs and replay inputs default to `data/lead_follower/`. The bot can run
alongside `time_arbitrage`; both remain read-only.

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
    research/           # Resumable one-year dataset pipeline and loaders
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

## Continuous bot operations

The [operations guide](docs/bot_operations.md) includes a new-server quickstart,
website bot management, daily/weekly reports, backups and independent Docker releases.
On Ubuntu 24.04, start with `sudo sh deploy/bootstrap.sh --install-dependencies`,
then use `polytraderctl` to import a release, add configs and deploy selected bots.
The website provides Overview, Bots, Deployments and Reports views. For local research, start
with `python -m polytrader.ops collect --root data --once`, then run
`python -m streamlit run src/polytrader/ops/dashboard.py --server.address=127.0.0.1`.
Install `.[live,ops]` first. Both bots remain paper/read-only research tools.

## IDEAS

- Statistical arbitrage on events that are very similar 
--> things like time for example, like events happening in order
- Oil trading? Tracking insiders?
- What about looking at one market and performing some kind of tme time analysis
---> Then find a way to replicate findings on other markets
- Maybe some way to use the orderbooks etc to do some other kind of trading strat?
- Forecast recurring markets to predict the next price

Example: arbitrage on the French presidential election?
