"""Public Python entry point shared by scripts, notebooks, and the CLI."""

from datetime import datetime

from polytrader.data.client import HISTORY_URL, PolymarketClient
from polytrader.data.dataset import PriceHistory
from polytrader.data.discovery import discover, select_market, select_token
from polytrader.data.history import UTC, fetch_history, resolve_window


def fetch_price_history(
    event: str | None = None,
    *,
    market: str | None = None,
    outcome: str | None = None,
    token_id: str | None = None,
    days: float | None = None,
    start: str | None = None,
    end: str | None = None,
    bucket_seconds: int | None = None,
    client: PolymarketClient | None = None,
) -> PriceHistory:
    """Fetch prices by event URL/slug and outcome, or directly by token ID.

    Choose ``days`` or ``start``/``end``. Dates without offsets use UTC; the end
    is exclusive and defaults to now. ``outcome`` defaults to Yes for an event.
    Multiple-market events require a market slug or ID. An optional client can
    supply a custom timeout or be replaced in tests.

    Returns data in memory, including a valid empty result. Does not print or
    save files. Invalid inputs raise ValueError; API failures raise DataError.
    """
    if bool(event) == bool(token_id):
        raise ValueError("Provide an event URL/slug OR token_id.")
    if token_id and (market is not None or outcome is not None):
        raise ValueError("market and outcome apply only when an event is provided.")
    start_ts, end_ts = resolve_window(start=start, end=end, days=days)
    if bucket_seconds is not None and not 60 <= bucket_seconds <= 86400:
        raise ValueError("bucket_seconds must be between 60 and 86400.")
    client = client if client is not None else PolymarketClient()
    metadata = {}
    if event:
        event_data, markets = discover(client, event)
        selected = select_market(markets, market)
        label, token_id = select_token(selected, outcome or "Yes")
        metadata = {
            "event": {key: event_data.get(key) for key in ("id", "slug", "title")},
            "market": selected.raw, "outcome": label,
        }
    points = fetch_history(client, token_id, start_ts, end_ts, bucket_seconds)
    return PriceHistory(data=points, metadata={
        "source": HISTORY_URL,
        "fetched_at": datetime.now(UTC).isoformat(),
        "token_id": token_id,
        "window": {
            "start": datetime.fromtimestamp(start_ts, UTC).isoformat(),
            "end": datetime.fromtimestamp(end_ts, UTC).isoformat(),
            "start_inclusive": True, "end_exclusive": True,
        },
        "requested_bucket_seconds": bucket_seconds,
        **metadata,
    })
