"""Convenient readers for compacted research datasets."""

from pathlib import Path

import pandas as pd

from polytrader.research.pull import DEFAULT_OUT


TABLES = {
    "universe", "markets", "events", "market_tags", "trades", "prices",
    "bars_1min", "bars_1h", "bars_1d",
}


def load_frame(name: str, out_dir=DEFAULT_OUT, *, columns=None, filters=None) -> pd.DataFrame:
    """Load one public table into pandas, including Hive partitions."""
    if name not in TABLES:
        raise ValueError(f"Unknown table {name!r}; choose from {', '.join(sorted(TABLES))}")
    out = Path(out_dir)
    path = out / (f"{name}.parquet" if name in {"universe", "markets", "events", "market_tags"}
                  else name)
    if not path.exists():
        raise FileNotFoundError(f"Dataset table does not exist: {path}")
    return pd.read_parquet(path, columns=columns, filters=filters)


def duckdb_connection(out_dir=DEFAULT_OUT):
    """Return an optional in-memory DuckDB connection with one view per table."""
    try:
        import duckdb
    except ImportError as exc:
        raise ImportError("DuckDB is optional; install it with: python -m pip install duckdb") from exc
    out = Path(out_dir).resolve()
    connection = duckdb.connect()
    for name in sorted(TABLES):
        target = out / (f"{name}.parquet" if name in {"universe", "markets", "events", "market_tags"}
                        else name / "close_month=*" / "*.parquet")
        if name in {"universe", "markets", "events", "market_tags"}:
            exists = target.exists()
        else:
            exists = any((out / name).glob("close_month=*/*.parquet"))
        if exists:
            escaped = str(target).replace("'", "''").replace("\\", "/")
            connection.execute(
                f'CREATE VIEW "{name}" AS SELECT * FROM read_parquet(\'{escaped}\', hive_partitioning=true)'
            )
    return connection
