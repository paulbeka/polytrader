"""Complete public trade history for one market, walking backwards in time.

The data API returns newest trades first, at most 500 per page, and refuses
offsets above 10,000. Paging by time avoids that cap: each full page keeps the
trades newer than its oldest second, then that boundary second is read on its
own (offset paging within one second), and the next page ends a second earlier.
No trade is read twice, so no deduplication heuristic is needed.
"""

from polytrader.data.client import DataError, PolymarketClient

TRADES_URL = "https://data-api.polymarket.com/trades"
PAGE_SIZE = 500
MAX_OFFSET = 10_000


def _page(client: PolymarketClient, condition_id: str, **params) -> list[dict]:
    rows = client.get_list(TRADES_URL, market=condition_id, limit=PAGE_SIZE,
                           takerOnly="true", **params)
    if any(not isinstance(row, dict) or type(row.get("timestamp")) is not int for row in rows):
        raise DataError("Trade rows must be objects with integer timestamps")
    return rows


def _second(client, condition_id, timestamp) -> list[dict]:
    rows, offset = [], 0
    while True:
        page = _page(client, condition_id, start=timestamp, end=timestamp, offset=offset or None)
        if any(row["timestamp"] != timestamp for row in page):
            raise DataError("Trade time filter returned rows outside the requested second")
        rows += page
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE
        if offset > MAX_OFFSET:
            raise DataError(f"More than {MAX_OFFSET} trades in one second; refusing partial results")


def fetch_trades(client: PolymarketClient, condition_id: str, *,
                 start: int | None = None, end: int | None = None) -> list[dict]:
    """Return every taker trade of a market, newest first; start/end are inclusive Unix seconds.

    Taker-only rows count each fill once; their summed ``size`` equals Gamma's
    market ``volume`` (shares). Dollar volume is ``size * price``.
    """
    trades = []
    while end is None or start is None or end >= start:
        page = _page(client, condition_id, start=start, end=end)
        if len(page) < PAGE_SIZE:
            return trades + page
        oldest = min(row["timestamp"] for row in page)
        trades += [row for row in page if row["timestamp"] > oldest]
        trades += _second(client, condition_id, oldest)
        end = oldest - 1
    return trades
