# Bot

Put reusable automation code in this package as the project grows.
Import data fetching from `polytrader.data`; it works without the CLI or notebooks.
For current and live bid/ask depth, import `polytrader.orderbook`. Its
`OrderBookService` maintains one book per outcome token across an event's markets.
See [the orderbook guide](../orderbook/README.md) for bot integration and freshness
checks. Market-data connections and book state belong there; strategies and order
execution belong in this package.

This package is currently a placeholder. Scheduling, strategies, portfolio state,
and order execution can be added when needed. Importing it starts no processes
and makes no network requests.
