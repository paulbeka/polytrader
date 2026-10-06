"""Consistent index backup and checksummed evidence archives; no live-file truncation."""

from datetime import datetime, timezone, timedelta
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile

from .collector import sessions, read_json
from .files import atomic_json, digest, utc, segments


def backup(root, database, destination, *, bucket=None, prefix="polytrader"):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = destination / f"{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(dir=destination) as temp:
        stage = Path(temp)
        snapshot = stage / "ops" / "index.sqlite3"
        snapshot.parent.mkdir()
        with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
        # Copy only sealed evidence; active runs retain checkpoints but cannot be
        # replayed fully from this backup. Never silently claim complete coverage.
        completed_runs = []
        covered_streams = {}
        for run, _ in sessions(root):
            output = stage / run.relative_to(root)
            output.mkdir(parents=True, exist_ok=True)
            for path in run.glob("*.json"):
                shutil.copy2(path, output / path.name)
            if (run / "PIN").exists():
                (output / "PIN").touch()
            coverage = {}
            for name in ("events", "inputs"):
                manifest = run / name / "segments.json"
                if manifest.exists():
                    data = read_json(manifest)
                    target_dir = output / name
                    target_dir.mkdir(exist_ok=True)
                    for item in data["segments"]:
                        path = run / name / item["file"]
                        if path.resolve().parent != (run / name).resolve():
                            raise ValueError("Invalid backup segment path")
                        if data.get("expired"):
                            continue
                        if digest(path) != item["sha256"]:
                            raise ValueError(f"Checksum mismatch: {path}")
                        shutil.copy2(path, target_dir / path.name)
                    coverage[name] = bool(data.get("closed") and not data.get("expired"))
                    if data.get("active"):
                        data.update(active=None, backup_incomplete=True)
                    atomic_json(target_dir / "segments.json", data)
                else:
                    path = run / (name + ".jsonl")
                    complete = (run / "summary.json").exists()
                    coverage[name] = complete and path.exists()
                    if coverage[name]:
                        shutil.copy2(path, output / path.name)
            atomic_json(output / "backup_coverage.json", coverage)
            covered_streams[run.relative_to(root).as_posix()] = coverage
            if (run / "summary.json").exists() and coverage.get("events"):
                completed_runs.append(run.relative_to(root).as_posix())
        reports = root / "ops" / "reports"
        if reports.exists():
            shutil.copytree(reports, stage / "ops" / "reports")
        checksums = {p.relative_to(stage).as_posix(): digest(p) for p in stage.rglob("*") if p.is_file()}
        atomic_json(stage / "backup.json", dict(created_at=utc(), files=checksums,
                    note="Active input tails excluded. Check per-run backup_coverage.json before replay."))
        temporary = archive.with_suffix(".tmp")
        with tarfile.open(temporary, "w:gz") as tar:
            for path in stage.rglob("*"):
                if path.is_file():
                    tar.add(path, arcname=path.relative_to(stage).as_posix(), recursive=False)
        temporary.replace(archive)
    receipt = dict(archive=archive.name, sha256=digest(archive), created_at=utc(), offsite=False,
                   completed_runs=completed_runs, covered_streams=covered_streams)
    if bucket:
        import boto3
        client = boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None)
        key = prefix.strip("/") + "/" + archive.name
        client.upload_file(str(archive), bucket, key)
        receipt.update(offsite=True, bucket=bucket, key=key)
    atomic_json(archive.with_suffix(".receipt.json"), receipt)
    atomic_json(root / "ops" / "backup_status.json", receipt)
    return archive


def restore(archive, destination):
    """Restore into an empty directory, reject links/traversal, verify every file."""
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Restore destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        names = set()
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if not member.isfile() or not target.is_relative_to(destination) or "\\" in member.name:
                raise ValueError("Unsafe backup member")
            if target in names:
                raise ValueError("Duplicate backup member")
            names.add(target)
        tar.extractall(destination, filter="data")
    manifest = read_json(destination / "backup.json")
    actual = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file() and p != destination / "backup.json"}
    if actual != set(manifest["files"]):
        raise ValueError("Backup file inventory does not match checksum manifest")
    for name, checksum in manifest["files"].items():
        target = (destination / name).resolve()
        if not target.is_relative_to(destination) or digest(target) != checksum:
            raise ValueError(f"Restore checksum mismatch: {name}")
    # The index contains absolute source paths: rebuild it from restored evidence.
    # Preserve its historical events (some retained raw files may have expired).
    with closing(sqlite3.connect(destination / "ops" / "index.sqlite3")) as con, con:
        for run, _ in sessions(destination):
            identity = read_json(run / "runtime.json", {})
            if identity:
                con.execute("UPDATE runs SET path=? WHERE key=?", (str(run), identity["instance_id"] + "/" + run.name))
    return destination


def retain(root, *, input_days=7, event_days=90, now=None):
    """Expire whole replay chains, only after successful offsite backup, never live runs."""
    root = Path(root).resolve()
    now = now or datetime.now(timezone.utc)
    receipt = read_json(root / "ops" / "backup_status.json", {})
    if not receipt.get("offsite"):
        return []
    backed_up = datetime.fromisoformat(receipt["created_at"])
    removed = []
    for run, _ in sessions(root):
        if (run / "PIN").exists() or not (run / "summary.json").exists():
            continue
        if run.relative_to(root).as_posix() not in receipt.get("completed_runs", []):
            continue
        completed = datetime.fromtimestamp((run / "summary.json").stat().st_mtime, timezone.utc)
        if completed >= backed_up:
            continue
        for name, days in (("inputs", input_days), ("events", event_days)):
            if not receipt.get("covered_streams", {}).get(run.relative_to(root).as_posix(), {}).get(name):
                continue
            if now - completed < timedelta(days=days):
                continue
            manifest = run / name / "segments.json"
            data = read_json(manifest, {})
            if data.get("expired") or (data and not data.get("closed")):
                continue
            paths = [p for _, p, _, _ in segments(run, name)]
            if not paths:
                continue
            if any(not p.resolve().is_relative_to(run.resolve()) for p in paths):
                raise ValueError("Retention path escapes session")
            if name == "events":
                from .storage import connect
                identity = read_json(run / "runtime.json", {})
                strategy = "lead_follower" if (run / "metadata.json").exists() else "time_arbitrage"
                key = identity.get("instance_id", strategy) + "/" + run.name
                with closing(connect(root / "ops" / "index.sqlite3", readonly=True)) as con:
                    indexed = {r[0] for r in con.execute("SELECT segment FROM sealed WHERE run_key=?", (key,))}
                if any(logical not in indexed for logical, *_ in segments(run, name)):
                    continue
            manifest.parent.mkdir(exist_ok=True)
            atomic_json(manifest, {**data, "expired": True, "expired_at": utc(), "segments": data.get("segments", [])})
            for path in paths:
                path.unlink()
                removed.append(str(path))
    return removed
