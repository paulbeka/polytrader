# Polytrader

Fetch public Polymarket event metadata and historical outcome prices. This first
version provides data fetching only. No account, API key, or paid service is needed.
Python 3.11+; no runtime dependencies.

## Setup (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` and
`.\.venv\Scripts\polytrader.exe` directly. If using uv, the equivalent setup is
`uv venv --python 3.12` followed by `uv pip install -e .`.

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

## Development

```powershell
python -m unittest discover -s tests -v
python -m polytrader --help
```

```text
src/polytrader/
    cli.py              # Commands and JSON output
    data/
        client.py       # Public HTTP requests, timeouts, and errors
        discovery.py    # Event to markets to outcome tokens
        history.py      # UTC windows, chunking, and pagination
tests/test_data.py       # Offline tests; no API calls
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
