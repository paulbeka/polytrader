"""SQLite is a rebuildable index, never the authoritative execution log."""

import json
from pathlib import Path
import sqlite3


def connect(path, *, readonly=False):
    path = Path(path).resolve()
    if readonly:
        con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
        con.execute("PRAGMA query_only=ON")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript('''
            CREATE TABLE IF NOT EXISTS runs (
                key TEXT PRIMARY KEY, instance TEXT NOT NULL, run_id TEXT NOT NULL,
                strategy TEXT NOT NULL, version TEXT, config_hash TEXT, git_sha TEXT,
                path TEXT NOT NULL, metadata TEXT NOT NULL, status TEXT NOT NULL,
                summary TEXT NOT NULL, indexed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                run_key TEXT NOT NULL, segment TEXT NOT NULL, offset INTEGER NOT NULL,
                utc TEXT, kind TEXT, market TEXT, amount TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(run_key,segment,offset));
            CREATE INDEX IF NOT EXISTS events_time ON events(utc,kind);
            CREATE INDEX IF NOT EXISTS events_run ON events(run_key,utc);
            CREATE TABLE IF NOT EXISTS cursors (
                run_key TEXT, segment TEXT, offset INTEGER NOT NULL,
                PRIMARY KEY(run_key,segment));
            CREATE TABLE IF NOT EXISTS issues (
                source TEXT PRIMARY KEY, message TEXT NOT NULL, seen_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS health (
                run_key TEXT, utc TEXT, healthy INTEGER, PRIMARY KEY(run_key,utc));
            CREATE TABLE IF NOT EXISTS sealed (
                run_key TEXT, segment TEXT, fingerprint TEXT, PRIMARY KEY(run_key,segment));
        ''')
    con.row_factory = sqlite3.Row
    return con


def rows(con, query, args=()):
    return [dict(r) for r in con.execute(query, args)]


def runs(con):
    result = rows(con, "SELECT * FROM runs ORDER BY indexed_at DESC, run_id DESC")
    for r in result:
        for k in ("metadata", "status", "summary"):
            r[k] = json.loads(r[k])
    return result
