"""Atomic JSON and lossless segmented JSONL with backwards-compatible readers."""

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import time


def default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (Decimal, Path)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def encode(value):
    return json.dumps(value, default=default, allow_nan=False, separators=(",", ":"))


def utc():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as f:
        f.write(encode(value) + "\n")
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


def digest(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


class Journal:
    """Single-writer append stream; manifest commit precedes removal of old data.

    Active segments are never externally truncated. All input segments form one
    replay chain; deleting any prefix explicitly expires the entire input stream.
    """
    def __init__(self, directory, name, *, max_bytes=16 * 1024**2, max_seconds=3600):
        self.root = Path(directory) / name
        self.root.mkdir()
        self.manifest = self.root / "segments.json"
        self.max_bytes, self.max_seconds = max_bytes, max_seconds
        self.items, self.index, self.bytes = [], 1, 0
        self.closed = False
        self._open()
        self._commit()

    def _open(self):
        self.path = self.root / f"{self.index:08d}.jsonl"
        self.file = self.path.open("x", encoding="utf-8", newline="\n", buffering=1)
        self.opened = time.monotonic()
        self.bytes = 0

    def _commit(self):
        atomic_json(self.manifest, dict(version=1, replay_requires_all_segments=True,
                    closed=self.closed, segments=self.items,
                    active=None if self.closed else self.path.name))

    def _seal(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        target = self.path.with_suffix(".jsonl.gz")
        temporary = target.with_suffix(".tmp")
        with self.path.open("rb") as source, gzip.open(temporary, "wb") as output:
            shutil.copyfileobj(source, output)
        temporary.replace(target)
        self.items.append(dict(file=target.name, sha256=digest(target), closed_at=utc(), bytes=target.stat().st_size))
        return self.path

    def write(self, text):
        if self.closed:
            raise ValueError("Journal is closed")
        if not text.endswith("\n"):
            raise ValueError("Journal writes must contain complete JSON lines")
        if self.bytes and (self.bytes >= self.max_bytes or time.monotonic() - self.opened >= self.max_seconds):
            old = self._seal()
            self.index += 1
            self._open()
            self._commit()
            old.unlink()
        self.file.write(text)
        self.bytes += len(text.encode("utf-8"))
        return len(text)

    def flush(self):
        self.file.flush()

    def fileno(self):
        return self.file.fileno()

    def close(self):
        if self.closed:
            return
        old = self._seal()
        self.closed = True
        self._commit()
        old.unlink()


def open_journal(directory, name):
    size = int(os.environ.get("POLYTRADER_SEGMENT_BYTES", "0"))
    if size > 0:
        return Journal(directory, name, max_bytes=size)
    return (Path(directory) / (name + ".jsonl")).open("x", encoding="utf-8", newline="\n", buffering=1)


def segments(directory, name):
    root = Path(directory)
    manifest = root / name / "segments.json"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("expired"):
            raise ValueError(f"{name} expired under retention policy; full replay unavailable")
        if data.get("backup_incomplete") and name == "inputs":
            raise ValueError("Backup omitted active inputs; full replay unavailable")
        for item in data["segments"]:
            file = item["file"]
            if Path(file).name != file:
                raise ValueError("Invalid segment path")
            yield file.removesuffix(".gz"), manifest.parent / file, True, item.get("sha256")
        if data.get("active"):
            file = data["active"]
            if Path(file).name != file:
                raise ValueError("Invalid active segment path")
            yield file, manifest.parent / file, False, None
    elif (root / (name + ".jsonl")).exists():
        yield name + ".jsonl", root / (name + ".jsonl"), (root / "summary.json").exists(), None


def open_binary(path):
    return gzip.open(path, "rb") if str(path).endswith(".gz") else Path(path).open("rb")


def read_journal(directory, name, *, strict=True):
    for _, path, _, checksum in segments(directory, name):
        if checksum and digest(path) != checksum:
            raise ValueError(f"Segment checksum mismatch: {path}")
        with open_binary(path) as f:
            for line in f:
                if not line.endswith(b"\n"):
                    if strict:
                        raise ValueError(f"Incomplete trailing record in {path}")
                    break
                yield json.loads(line)
