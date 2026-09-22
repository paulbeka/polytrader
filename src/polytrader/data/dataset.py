"""Load and save history using the same JSON format as the CLI."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import TYPE_CHECKING

from polytrader.data.client import DataError
from polytrader.data.history import validate_point

if TYPE_CHECKING:
    import pandas as pd


@dataclass
class PriceHistory:
    """Price observations plus their source, window, and market metadata.

    ``data`` is a list of ordinary dictionaries. pandas is optional and is only
    imported when ``to_frame()`` is called.
    """

    data: list[dict]
    metadata: dict

    @classmethod
    def from_dict(cls, payload: dict) -> "PriceHistory":
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise DataError("History must be a JSON object containing a data array")
        for point in payload["data"]:
            validate_point(point)
        return cls(
            data=[dict(point) for point in payload["data"]],
            metadata={key: value for key, value in payload.items() if key != "data"},
        )

    def to_dict(self) -> dict:
        """Return the JSON-compatible envelope used by existing history files."""
        return {**self.metadata, "data": self.data}

    def save(self, path: str | Path) -> Path:
        """Save to a new JSON file; never overwrite an existing file."""
        encoded = json.dumps(self.to_dict(), indent=2, allow_nan=False) + "\n"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as destination:
            destination.write(encoded)
        return path

    def to_frame(self) -> "pd.DataFrame":
        """Return a time-sorted DataFrame with a UTC datetime index.

        Keeps Unix-second timestamps, prices, and reported resolutions. Does not
        resample, fill gaps, or discard observations sharing a timestamp.
        """
        try:
            import pandas as pd
        except ImportError as exc:
            raise ImportError(
                'DataFrame support requires pandas. Run: python -m pip install -e ".[sandbox]"'
            ) from exc
        frame = pd.DataFrame(self.data, columns=["timestamp", "price", "resolution_seconds"])
        frame = frame.astype({"timestamp": "int64", "price": "float64", "resolution_seconds": "int64"})
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], unit="s", utc=True), name="datetime")
        return frame.sort_index(kind="stable")


def load_history(path: str | Path) -> PriceHistory:
    """Read an existing CLI/Python history JSON file without accessing the API."""
    try:
        with Path(path).open(encoding="utf-8-sig") as source:
            payload = json.load(source)
    except (ValueError, UnicodeError) as exc:
        raise DataError(f"Invalid history JSON in {path}") from exc
    return PriceHistory.from_dict(payload)
