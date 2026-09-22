"""Resolve an event to its markets and outcome token IDs."""

from dataclasses import dataclass
import json
import re
from urllib.parse import urlparse

from polytrader.data.client import DataError, PolymarketClient


@dataclass(frozen=True)
class Market:
    id: str
    slug: str
    question: str
    tokens: dict[str, str]
    raw: dict


def event_slug(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parsed = urlparse(value)
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"polymarket.com", "www.polymarket.com"}
            or len(parts) != 2
            or parts[0] != "event"
        ):
            raise ValueError("Use https://polymarket.com/event/<event-slug> or a bare event slug.")
        value = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid event slug; use the slug from a Polymarket event URL.")
    return value


def _array(value, field: str) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise DataError(f"Invalid JSON in market field {field}") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise DataError(f"Expected a string array in market field {field}")
    return value


def parse_market(raw: dict) -> Market:
    if not isinstance(raw, dict):
        raise DataError("Expected a market object")
    outcomes = _array(raw.get("outcomes"), "outcomes")
    tokens = _array(raw.get("clobTokenIds"), "clobTokenIds")
    if len(set(outcome.casefold() for outcome in outcomes)) != len(outcomes):
        raise DataError("Market contains duplicate outcome labels")
    # Older/non-CLOB markets can legitimately have no outcome token IDs.
    if tokens and (len(tokens) != len(outcomes) or len(set(tokens)) != len(tokens)):
        raise DataError("Market outcomes and token IDs do not form a one-to-one mapping")
    return Market(
        id=str(raw.get("id", "")), slug=raw.get("slug", ""),
        question=raw.get("question", ""),
        tokens=dict(zip(outcomes, tokens)), raw=raw,
    )


def discover(client: PolymarketClient, source: str) -> tuple[dict, list[Market]]:
    event = client.get_event(event_slug(source))
    markets = event.get("markets")
    if not isinstance(markets, list):
        raise DataError("Event response is missing its markets array")
    return event, [parse_market(market) for market in markets]


def select_market(markets: list[Market], selector: str | None) -> Market:
    matches = markets if selector is None else [
        market for market in markets if selector in {market.slug, market.id}
    ]
    if len(matches) != 1:
        raise ValueError("Select exactly one market with --market <slug-or-id>; use 'markets' to list them.")
    return matches[0]


def select_token(market: Market, outcome: str) -> tuple[str, str]:
    for label, token in market.tokens.items():
        if label.casefold() == outcome.casefold():
            return label, token
    available = ", ".join(market.tokens) or "no CLOB tokens available"
    raise ValueError(f"Outcome {outcome!r} is unavailable. Available outcomes: {available}")
