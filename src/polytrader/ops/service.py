"""One collector owns SQLite, scheduled reports, retention and backup bookkeeping."""

from datetime import datetime, timedelta
from contextlib import closing
import json
import os
from pathlib import Path
import time
import tomllib
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .backup import backup, retain
from .collector import collect, read_json, state
from .files import atomic_json, utc
from .reports import daily, period, save_report
from .runtime import graceful_signals
from .storage import connect, runs


def run_collector(root, database, *, once=False, interval=5, timezone_name="Europe/London"):
    root = Path(root).resolve()
    ops = root / "ops"
    ops.mkdir(parents=True, exist_ok=True)
    from .locking import lock
    with lock(ops / "collector.lock"), closing(connect(database)) as con, graceful_signals():
        last_report, last_alert, next_backup = 0, None, 0
        try:
            while True:
                inserted = collect(con, root)
                now = datetime.now(ZoneInfo(timezone_name))
                configured = {}
                registry = os.environ.get("POLYTRADER_INSTANCES")
                if registry:
                    if registry.endswith(".json"):
                        configured = read_json(registry, {})
                    else:
                        with open(registry, "rb") as stream:
                            configured = tomllib.load(stream).get("instances", {})
                if time.monotonic() - last_report > 60 or once:
                    # Rebuild every indexed date on first start; subsequent runs update
                    # today/yesterday so late records and DST boundaries are reflected.
                    dates = {now.date(), now.date() - timedelta(days=1)}
                    if not last_report:
                        for (stamp,) in con.execute("SELECT substr(utc,1,10) FROM events WHERE utc IS NOT NULL UNION SELECT substr(utc,1,10) FROM health"):
                            day = datetime.fromisoformat(stamp).date()
                            dates.update((day - timedelta(days=1), day, day + timedelta(days=1)))
                    for day in sorted(dates):
                        save_report(ops / "reports", daily(con, day, timezone_name, configured=configured))
                    save_report(ops / "reports", period(con, now.date() - timedelta(days=6), now.date(), timezone_name,
                                                       configured=configured), name="last-7-days")
                    last_report = time.monotonic()
                latest = {}
                for run in sorted(runs(con), key=lambda r: r["run_id"], reverse=True):
                    latest.setdefault(run["instance"], run)
                alerts = []
                for name, run in latest.items():
                    status = state(run)
                    if status in {"interrupted_or_unreachable", "runtime_error", "error", "feed_ended", "reconnecting"}:
                        alerts.append(f"{name}: {status}")
                    if run["status"].get("disk_used_fraction", 0) > .85:
                        alerts.append(f"{name}: disk usage exceeds 85%")
                bucket = os.environ.get("POLYTRADER_BACKUP_BUCKET")
                previous = read_json(ops / "backup_status.json", {})
                if bucket and not once and time.monotonic() >= next_backup and previous.get("created_at", "")[:10] != utc()[:10]:
                    next_backup = time.monotonic() + 3600
                    try:
                        archive = backup(root, database, ops / "backups", bucket=bucket,
                                         prefix=os.environ.get("POLYTRADER_BACKUP_PREFIX", "polytrader"))
                        retain(root)
                        # Keep a bounded local cache; bucket lifecycle controls offsite history.
                        for old in sorted((ops / "backups").glob("*.tar.gz"))[:-2]:
                            old.unlink()
                            old.with_suffix(".receipt.json").unlink(missing_ok=True)
                    except Exception as exc:
                        alerts.append(f"Backup failed: {exc}")
                        atomic_json(ops / "backup_error.json", dict(at=utc(), error=str(exc)))
                backup_error = read_json(ops / "backup_error.json", {})
                if backup_error.get("at", "") > previous.get("created_at", ""):
                    alerts.append("Last backup failed; see backup_error.json")
                atomic_json(ops / "alerts.json", dict(updated_at=utc(), alerts=alerts))
                if alerts != last_alert:
                    if alerts:
                        print("ALERT " + "; ".join(alerts), flush=True)
                        webhook = os.environ.get("POLYTRADER_ALERT_WEBHOOK")
                        if webhook:
                            try:
                                request = Request(webhook, data=json.dumps({"text": "Polytrader: " + "; ".join(alerts)}).encode(),
                                                  headers={"Content-Type": "application/json"})
                                with urlopen(request, timeout=10) as response:
                                    response.read(1024)
                            except Exception as exc:
                                print(f"Alert delivery failed: {exc}", flush=True)
                    last_alert = alerts
                atomic_json(ops / "collector_status.json", dict(heartbeat_at=utc(), last_inserted=inserted,
                            timezone=timezone_name, configured_instances={name: value["strategy"] for name, value in configured.items()}))
                if once:
                    return
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
