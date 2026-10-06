"""Single-writer, restartable ingestion of legacy and segmented session events."""

from datetime import datetime, timezone
from decimal import InvalidOperation
import hashlib
import json
from pathlib import Path

from .adapters import ADAPTERS
from .files import digest, encode, open_binary, segments, utc


def read_json(path, fallback=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback


def sessions(root):
    root = Path(root).resolve()
    found = set()
    for pattern in ("metadata.json", "manifest.json"):
        for path in root.rglob(pattern):
            if any(p in {"ops", "backups", ".staging"} for p in path.relative_to(root).parts):
                continue
            directory = path.parent
            if directory in found or not ((directory / "events.jsonl").exists() or (directory / "events").is_dir()):
                continue
            found.add(directory)
            yield directory, pattern


def collect(con, root, *, batch_size=10000):
    inserted = 0
    for directory, filename in sessions(root):
        try:
            metadata = read_json(directory / filename)
            strategy = "lead_follower" if filename == "metadata.json" else "time_arbitrage"
            runtime = read_json(directory / "runtime.json", {})
            instance = runtime.get("instance_id", strategy)
            key = instance + "/" + directory.name
            status = read_json(directory / "status.json", {})
            summary = read_json(directory / "summary.json")
            if summary is None:
                summary = read_json(directory / "current_summary.json", {}).get("summary", {})
                checkpoint = read_json(directory / "checkpoint.json", {})
                if checkpoint.get("summary"):
                    summary = checkpoint.get("summary", {})
            config_hash = runtime.get("config_hash", metadata.get("config_sha256")) or hashlib.sha256(
                encode(metadata.get("config", {})).encode()).hexdigest()
            with con:
                con.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                            "status=excluded.status,summary=excluded.summary,indexed_at=excluded.indexed_at,path=excluded.path",
                            (key, instance, directory.name, strategy, str(metadata.get("strategy_version", 1)),
                             config_hash, runtime.get("git_sha", metadata.get("git_commit", "unknown")),
                             str(directory), encode(metadata), encode(status), encode(summary), utc()))
                if status.get("heartbeat_at"):
                    con.execute("INSERT OR IGNORE INTO health VALUES(?,?,?)", (key, status["heartbeat_at"],
                                int(status.get("process_state") == "running" and status.get("transport_healthy", False))))
            if read_json(directory / "events" / "segments.json", {}).get("expired"):
                continue  # Historical index remains queryable; raw retention is explicit.
            for logical, path, closed, checksum in segments(directory, "events"):
                fingerprint = checksum or f"{path.stat().st_size}:{path.stat().st_mtime_ns}"
                sealed = con.execute("SELECT fingerprint FROM sealed WHERE run_key=? AND segment=?", (key, logical)).fetchone()
                if sealed:
                    if sealed[0] != fingerprint:
                        raise ValueError(f"Previously indexed closed segment changed: {path}")
                    continue
                if checksum and digest(path) != checksum:
                    raise ValueError(f"Segment checksum mismatch: {path}")
                cursor = con.execute("SELECT offset FROM cursors WHERE run_key=? AND segment=?", (key, logical)).fetchone()
                offset = cursor[0] if cursor else 0
                with open_binary(path) as stream, con:
                    stream.seek(offset)
                    for _ in range(batch_size):
                        start = stream.tell()
                        line = stream.readline()
                        if not line:
                            if closed:
                                con.execute("INSERT OR REPLACE INTO sealed VALUES(?,?,?)", (key, logical, fingerprint))
                            break
                        if not line.endswith(b"\n"):
                            if closed:
                                raise ValueError(f"Incomplete closed event segment at {start}")
                            break
                        try:
                            row = json.loads(line)
                            kind, market, amount = ADAPTERS[strategy].event(row)
                            stamp = row.get("utc")
                            if stamp:
                                dt = datetime.fromisoformat(stamp)
                                if dt.tzinfo is None:
                                    raise ValueError("Event timestamp requires timezone")
                                stamp = dt.astimezone(timezone.utc).isoformat()
                            count = con.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?)",
                                (key, logical, start, stamp, kind, market, amount, encode(row))).rowcount
                            inserted += count
                        except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as exc:
                            con.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?)",
                                        (f"{directory}/{logical}:{start}", str(exc), utc()))
                        offset = stream.tell()
                    con.execute("INSERT OR REPLACE INTO cursors VALUES(?,?,?)", (key, logical, offset))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            with con:
                con.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?)", (str(directory), str(exc), utc()))
    return inserted


def state(run, *, now=None):
    status = run["status"]
    if status.get("process_state") == "stopped":
        return status.get("termination", "stopped")
    if status.get("heartbeat_at"):
        age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(status["heartbeat_at"])).total_seconds()
        if age > 60:
            return "interrupted_or_unreachable"
        if not status.get("transport_healthy"):
            return "reconnecting"
        if status.get("warming_up"):
            return "warming_up"
        if status.get("unavailable_books"):
            return "connected_missing_books"
        return "healthy"
    summary = run["summary"]
    return (summary.get("termination") or summary.get("termination_reason") or "completed_legacy") if summary else "unknown_legacy"
