"""Small, unauthenticated HTTP client; no trading endpoints."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

GAMMA_URL = "https://gamma-api.polymarket.com"
HISTORY_URL = "https://data-api.polymarket.com/v2/prices-history"


class DataError(Exception):
    """An API request or response could not be used."""


class PolymarketClient:
    json_float = float

    def __init__(self, timeout: float = 20):
        self.timeout = timeout

    def get_json(self, url: str, **params) -> dict:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        request = Request(
            f"{url}?{query}" if query else url,
            headers={"Accept": "application/json", "User-Agent": "polytrader/0.1"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response, parse_float=self.json_float)
        except HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise DataError(f"HTTP {exc.code} from {url}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DataError(f"Request to {url} failed: {exc}") from exc
        except (ValueError, UnicodeError) as exc:
            raise DataError(f"Invalid JSON from {url}") from exc
        if not isinstance(payload, dict):
            raise DataError(f"Expected a JSON object from {url}")
        return payload

    def get_event(self, slug: str) -> dict:
        return self.get_json(f"{GAMMA_URL}/events/slug/{quote(slug, safe='')}")

    def get_history_page(self, token_id: str, **params) -> dict:
        return self.get_json(HISTORY_URL, token_id=token_id, **params)
