"""UTC date windows and paginated price history."""

from datetime import datetime, timedelta, timezone
import math
import re

from polytrader.data.client import DataError, PolymarketClient

UTC = timezone.utc
MAX_WINDOW_SECONDS = 15 * 86400


def validate_point(point: dict) -> None:
    """Validate the common price-point schema for API responses and local files."""
    if not isinstance(point, dict):
        raise DataError("Invalid history point")
    timestamp = point.get("timestamp")
    price = point.get("price")
    resolution = point.get("resolution_seconds")
    if (
        type(timestamp) is not int or type(resolution) is not int or resolution < 0
        or type(price) not in {int, float} or not math.isfinite(price)
        or not 0 <= price <= 1
    ):
        raise DataError("History point has an invalid timestamp, price, or resolution")


def parse_date(value: str) -> datetime:
    if not re.match(r"^\d{4}-\d{2}-\d{2}($|T)", value):
        raise ValueError("Dates must use YYYY-MM-DD or an ISO timestamp such as 2026-09-01T12:00:00Z.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid date: {value!r}") from exc
    if result.microsecond:
        raise ValueError("Dates must use whole seconds.")
    return (result.replace(tzinfo=UTC) if result.tzinfo is None else result).astimezone(UTC)


def resolve_window(
    *, start: str | None = None, end: str | None = None,
    days: float | None = None, now: datetime | None = None,
) -> tuple[int, int]:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC).replace(microsecond=0)
    if days is not None:
        if start is not None or end is not None:
            raise ValueError("Use --days OR --start/--end, not both.")
        if not math.isfinite(days) or days <= 0:
            raise ValueError("--days must be a positive, finite number.")
        try:
            beginning, ending = now - timedelta(days=days), now
        except OverflowError as exc:
            raise ValueError("--days is too large.") from exc
    else:
        if start is None:
            raise ValueError("Specify --days N or --start DATE (with optional --end DATE).")
        beginning = parse_date(start)
        ending = parse_date(end) if end else now
    start_ts, end_ts = int(beginning.timestamp()), int(ending.timestamp())
    if start_ts < 0 or start_ts >= end_ts:
        raise ValueError("The start must be on/after 1970-01-01 and earlier than the end.")
    if end_ts > int(now.timestamp()):
        raise ValueError("The end cannot be in the future.")
    return start_ts, end_ts


def fetch_history(
    client: PolymarketClient, token_id: str, start: int, end: int,
    bucket_seconds: int | None = None,
) -> list[dict]:
    if not token_id.isascii() or not token_id.isdecimal():
        raise ValueError("The token ID must contain only digits.")
    if start < 0 or start >= end:
        raise ValueError("History requires 0 <= start < end.")
    if bucket_seconds is not None and not 60 <= bucket_seconds <= 86400:
        raise ValueError("--bucket-seconds must be between 60 and 86400.")
    points = {}
    window_start = start
    while window_start < end:
        window_end = min(window_start + MAX_WINDOW_SECONDS, end)
        cursor = None
        seen_cursors = set()
        while True:
            page = client.get_history_page(
                token_id, start=window_start, end=window_end,
                bucket_seconds=bucket_seconds, cursor=cursor,
            )
            data, pagination = page.get("data"), page.get("pagination")
            if not isinstance(data, list) or not isinstance(pagination, dict):
                raise DataError("History response is missing data or pagination")
            for point in data:
                validate_point(point)
                timestamp = point["timestamp"]
                resolution = point["resolution_seconds"]
                # Keep actual resolution and settlement points (resolution 0).
                # The API can clamp a settlement point to the exclusive end.
                if window_start <= timestamp < window_end:
                    points[(timestamp, resolution)] = point
            has_more = pagination.get("has_more")
            if type(has_more) is not bool:
                raise DataError("History pagination is missing has_more")
            if not has_more:
                break
            cursor = pagination.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise DataError("History pagination cursor is missing or repeated; refusing partial results")
            seen_cursors.add(cursor)
        window_start = window_end
    return [points[key] for key in sorted(points)]
