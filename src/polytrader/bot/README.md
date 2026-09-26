# Bot

Put reusable automation code in this package as the project grows.
Import data fetching from `polytrader.data`; it works without the CLI or notebooks.
For current and live bid/ask depth, import `polytrader.orderbook`. Its
`OrderBookService` maintains one book per outcome token across an event's markets.
See [the orderbook guide](../orderbook/README.md) for bot integration and freshness
checks. Market-data connections and book state belong there; strategies and order
execution belong in this package.

The [time-arbitrage scanner](time_arbitrage/README.md) scans user-reviewed ordered
market chains, sizes earlier-NO / later-YES quoted opportunities, and saves
fee-aware lifecycle records. It is read-only and places no orders. Importing the
package starts no processes and makes no network requests.
