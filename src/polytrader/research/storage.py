"""Storage, retry, rate-limit, progress, and manifest helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError

from polytrader.data.client import DataError


UTC = timezone.utc
_FAILURE_LOCK = threading.Lock()
_MANIFEST_LOCK = threading.Lock()


def _replace(temporary: Path, path: Path) -> None:
    """Atomic rename with short retries for transient OneDrive/AV locks."""
    for attempt in range(8):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (2 ** attempt))


class RateLimiter:
    """Thread-safe, process-wide spacing between HTTP requests."""

    def __init__(self, requests_per_second: float = 4.0):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.interval = 1.0 / requests_per_second
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if delay:
            time.sleep(delay)


class LimitedClient:
    """Apply one shared limiter to every request made by a client."""

    def __init__(self, client, limiter: RateLimiter):
        self.client = client
        self.limiter = limiter

    def get_json(self, *args, **kwargs):
        self.limiter.wait()
        return self.client.get_json(*args, **kwargs)

    def get_list(self, *args, **kwargs):
        self.limiter.wait()
        return self.client.get_list(*args, **kwargs)

    def get_history_page(self, *args, **kwargs):
        self.limiter.wait()
        return self.client.get_history_page(*args, **kwargs)


def retrying(call, *, attempts: int = 6, on_retry=None):
    """Retry 429/5xx and network failures with exponential backoff and jitter."""
    for attempt in range(attempts):
        try:
            return call()
        except DataError as exc:
            cause = exc.__cause__
            transient = (
                isinstance(cause, HTTPError) and (cause.code == 429 or cause.code >= 500)
                or isinstance(cause, (URLError, TimeoutError, ConnectionError, OSError))
            )
            if not transient or attempt == attempts - 1:
                raise
            delay = min(30.0, 2.0**attempt) + random.random()
            if on_retry is not None:
                on_retry(attempt + 1, attempts, delay, exc)
            time.sleep(delay)


def atomic_parquet(frame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    frame.to_parquet(temporary, index=False, compression="zstd")
    _replace(temporary, path)


def atomic_json(value, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    _replace(temporary, path)


def atomic_jsonl_zstd(rows: list[dict], path: Path) -> None:
    """Write a standards-compatible zstd-compressed JSON Lines stream."""
    try:
        import pyarrow as pa
    except ImportError as exc:
        raise ImportError("Research storage requires pyarrow; install requirements-sandbox.txt") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with pa.CompressedOutputStream(str(temporary), "zstd") as sink:
        for row in rows:
            sink.write((json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
    _replace(temporary, path)


def append_failure(out_dir: Path, *, stage: str, condition_id: str | None, error: Exception | str) -> None:
    entry = {
        "stage": stage,
        "condition_id": condition_id,
        "error": str(error),
        "at": datetime.now(UTC).isoformat(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    with _FAILURE_LOCK:
        with (out_dir / "failures.jsonl").open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(entry, ensure_ascii=False) + "\n")


def git_commit(repo: Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def update_manifest(out_dir: Path, step: str, *, started: datetime, parameters: dict,
                    row_counts: dict | None = None, extra: dict | None = None) -> dict:
    """Merge one completed step into the run manifest atomically."""
    path = out_dir / "manifest.json"
    finished = datetime.now(UTC)
    with _MANIFEST_LOCK:
        if path.exists():
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {}
        else:
            manifest = {}
        manifest.setdefault("dataset_version", 1)
        manifest.setdefault("created_at", started.isoformat())
        manifest["updated_at"] = finished.isoformat()
        manifest.setdefault("api_endpoints", {
            "events": "https://gamma-api.polymarket.com/events/keyset",
            "trades": "https://data-api.polymarket.com/trades",
            "prices": "https://data-api.polymarket.com/v2/prices-history",
        })
        manifest.setdefault("code", {})["git_commit"] = git_commit()
        manifest.setdefault("steps", {})[step] = {
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_seconds": round((finished - started).total_seconds(), 3),
            "parameters": parameters,
            "row_counts": row_counts or {},
        }
        if extra:
            manifest.update(extra)
        atomic_json(manifest, path)
    return manifest


class Progress:
    def __init__(self, total: int, *, label: str, log=print, every: int = 25):
        self.total = total
        self.label = label
        self.log = log
        self.every = every
        self.started = time.monotonic()

    def report(self, finished: int, *, detail: str = "") -> None:
        if finished != self.total and finished % self.every:
            return
        elapsed = time.monotonic() - self.started
        eta = elapsed / finished * (self.total - finished) if finished else 0.0
        suffix = f", {detail}" if detail else ""
        self.log(
            f"  {self.label}: {finished:,}/{self.total:,}{suffix}, "
            f"{elapsed / 60:.1f} min elapsed, ~{eta / 60:.1f} min left"
        )
