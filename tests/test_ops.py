import asyncio
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from polytrader.ops.backup import backup, restore, retain
from polytrader.ops.collector import collect, state
from polytrader.ops.deployment import deploy, definition, compose
from polytrader.ops.files import Journal, atomic_json, encode, read_journal, utc
from polytrader.ops.reports import daily, save_report
from polytrader.ops.runtime import Runtime
from polytrader.ops.storage import connect, runs

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
IMAGE = "ghcr.io/paulbeka/polytrader@sha256:" + "a" * 64


def session(root, strategy="lead_follower", identity="one", rows=()):
    path = Path(root) / identity / "20261005T120000Z-test"
    path.mkdir(parents=True)
    atomic_json(path / ("metadata.json" if strategy == "lead_follower" else "manifest.json"),
                dict(strategy_version=2 if strategy == "lead_follower" else 1, config={}))
    atomic_json(path / "runtime.json", dict(instance_id=identity, git_sha="fixture"))
    (path / "events.jsonl").write_text("".join(encode({"utc": NOW.isoformat(), **row}) + "\n" for row in rows), encoding="utf-8")
    return path


class StorageTests(unittest.TestCase):
    def test_partial_lines_restart_and_decimal_strategy_separation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = session(root, rows=[dict(type="exit", pnl="0.1", holding_seconds=2),
                                      dict(type="exit", pnl="0.2", holding_seconds=4)])
            session(root, "time_arbitrage", "two", [dict(event="opportunity_open", calculation={"conservative_profit": "12"}),
                                                       dict(event="opportunity_update", calculation={"conservative_profit": "10"})])
            with (path / "events.jsonl").open("ab") as stream:
                stream.write(b'{"type":"burst_candidate"')
            database = root / "ops" / "index.sqlite3"
            with closing(connect(database)) as con:
                self.assertEqual(collect(con, root), 4)
                self.assertEqual(collect(con, root), 0)
            with (path / "events.jsonl").open("ab") as stream:
                stream.write(b',"rejection_reasons":["warmup"]}\nBROKEN\n')
            with closing(connect(database)) as con:
                self.assertEqual(collect(con, root), 1)
                self.assertEqual(collect(con, root), 0)
                self.assertEqual(con.execute("SELECT count(*) FROM issues").fetchone()[0], 1)
                report = daily(con, NOW.date())
                groups = {r["strategy"]: r for r in report["groups"]}
                self.assertEqual(groups["lead_follower"]["closed_paper_pnl"], Decimal("0.3"))
                self.assertIsNone(groups["time_arbitrage"]["closed_paper_pnl"])
                self.assertEqual(groups["time_arbitrage"]["max_quoted_profit"], Decimal("12"))
                save_report(root / "ops" / "reports", report)
                self.assertTrue((root / "ops" / "reports" / "2026-10-05.csv").exists())

    def test_rotation_keeps_offsets_and_verifies_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session(tmp)
            (path / "events.jsonl").unlink()
            stream = Journal(path, "events", max_bytes=1)
            with closing(connect(Path(tmp) / "index.sqlite3")) as con:
                stream.write(encode(dict(type="signal")) + "\n")
                self.assertEqual(collect(con, tmp), 1)
                stream.write(encode(dict(type="entry")) + "\n")
                stream.close()
                self.assertEqual(collect(con, tmp), 1)
                self.assertEqual(collect(con, tmp), 0)
                self.assertEqual([r["type"] for r in read_journal(path, "events")], ["signal", "entry"])
                segment = next((path / "events").glob("*.gz"))
                segment.write_bytes(b"corrupt")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    list(read_journal(path, "events"))

    def test_dst_days_and_health_gaps(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(Path(tmp) / "index.sqlite3")) as con:
            spring = daily(con, date(2026, 3, 29))
            autumn = daily(con, date(2026, 10, 25))
            self.assertEqual((spring["utc_end"] - spring["utc_start"]).total_seconds(), 23 * 3600)
            self.assertEqual((autumn["utc_end"] - autumn["utc_start"]).total_seconds(), 25 * 3600)
            path = session(tmp)
            status = dict(heartbeat_at=NOW.isoformat(), process_state="running", transport_healthy=True)
            atomic_json(path / "status.json", status)
            collect(con, tmp)
            status["heartbeat_at"] = (NOW + timedelta(seconds=10)).isoformat()
            atomic_json(path / "status.json", status)
            collect(con, tmp)
            status["heartbeat_at"] = (NOW + timedelta(hours=3)).isoformat()
            atomic_json(path / "status.json", status)
            collect(con, tmp)
            report = daily(con, NOW.date())
            self.assertAlmostEqual(report["run_health"][0]["healthy_observation_hours"], 10 / 3600)

    def test_quiet_runtime_and_interrupted_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session(tmp)
            book = SimpleNamespace(status="live", updated_at=NOW - timedelta(hours=5))
            service = SimpleNamespace(healthy=True, continuity=0, collection=SimpleNamespace(books={"yes": book}))
            runtime = Runtime(path, "lead_follower", {})
            runtime.publish(dict(open_positions=[dict(id="unresolved")], closed_pnl="0"), service)
            with closing(connect(Path(tmp) / "index.sqlite3")) as con:
                collect(con, tmp)
                run = runs(con)[0]
                self.assertEqual(state(run), "healthy")
                later = datetime.now(timezone.utc) + timedelta(hours=3)
                self.assertEqual(state(run, now=later), "interrupted_or_unreachable")
                self.assertEqual(run["summary"]["open_positions"][0]["id"], "unresolved")
                self.assertEqual(run["summary"]["closed_pnl"], "0")
            with patch.dict(os.environ, POLYTRADER_MIN_FREE_BYTES=str(10**30)):
                with self.assertRaisesRegex(OSError, "disk"):
                    runtime.publish({}, service, force=True)

    def test_backup_restore_checksum_and_live_replay_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            path = session(root, rows=[dict(type="exit", pnl="1")])
            atomic_json(path / "summary.json", dict(closed_pnl="1"))
            (path / "inputs.jsonl").write_text('{"test":true}\n')
            live = session(root, identity="live")
            stream = Journal(live, "inputs", max_bytes=1)
            stream.write('{"first":true}\n')
            stream.write('{"second":true}\n')
            try:
                database = root / "ops" / "index.sqlite3"
                with closing(connect(database)) as con:
                    collect(con, root)
                archive = backup(root, database, Path(tmp) / "archives")
                restored = restore(archive, Path(tmp) / "restored")
                self.assertEqual(list(read_journal(restored / path.relative_to(root), "inputs")), [{"test": True}])
                with self.assertRaisesRegex(ValueError, "omitted active"):
                    list(read_journal(restored / live.relative_to(root), "inputs"))
                with closing(connect(restored / "ops" / "index.sqlite3", readonly=True)) as con:
                    self.assertEqual(con.execute("SELECT count(*) FROM events").fetchone()[0], 1)
                self.assertEqual(retain(root, input_days=0, event_days=0), [])  # local backup isn't offsite
                receipt_path = root / "ops" / "backup_status.json"
                receipt = json.loads(receipt_path.read_text())
                receipt["offsite"] = True  # Simulate confirmed upload, no network in tests.
                atomic_json(receipt_path, receipt)
                (path / "PIN").touch()
                self.assertEqual(retain(root, input_days=0, event_days=0), [])
                (path / "PIN").unlink()
                self.assertTrue(retain(root, input_days=0, event_days=0))
                self.assertTrue(stream.path.exists())
                with self.assertRaisesRegex(ValueError, "expired"):
                    list(read_journal(path, "inputs"))
            finally:
                stream.close()

    def test_restore_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                member = tarfile.TarInfo("../outside")
                member.size = 1
                tar.addfile(member, io.BytesIO(b"x"))
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                restore(archive, Path(tmp) / "restore")
            self.assertFalse((Path(tmp) / "outside").exists())

    def test_segmented_real_engine_replay_matches_legacy(self):
        from test_lead_follower import FAMILY, settings, book, Config, Group, Engine, Session, replay
        summaries = []
        with tempfile.TemporaryDirectory() as tmp:
            for size in ("0", "1"):
                config = Config((Group("g", "event", "leader", ("follower",)),), settings(), Path(tmp) / size)
                with patch.dict(os.environ, POLYTRADER_SEGMENT_BYTES=size):
                    collection = SimpleNamespace(markets={}, excluded=[])
                    session_log = Session(config.output_dir, config, (FAMILY,), collection)
                    books = {t: book(t) for t in ("ly", "ln", "fy", "fn")}
                    engine = Engine((FAMILY,), config.settings, session_log.emit)
                    for elapsed in (0, 10, 30, 3600):
                        stamp = (NOW + timedelta(seconds=elapsed)).isoformat()
                        row = dict(elapsed_seconds=elapsed, utc=stamp, books={t: b.to_dict() for t, b in books.items()},
                                   trade=None, healthy=True, reason="transport_gap")
                        session_log.input(row)
                        engine.observe(elapsed, stamp, books)
                    session_log.finish(engine.summary(books))
                    session_log.close()
                    summaries.append(replay(session_log.directory))
            self.assertEqual(summaries[0], summaries[1])


class DeploymentTests(unittest.TestCase):
    def prepare(self, root):
        (root / "configs").mkdir()
        (root / "configs" / "bot.toml").write_text("first configuration")
        (root / "instances.toml").write_text('image_repository="ghcr.io/paulbeka/polytrader"\n[instances.bot]\nstrategy="lead_follower"\nconfig="configs/bot.toml"\n')

    def test_allowlist_and_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare(root)
            model = deploy(root, "bot", IMAGE, dry_run=True)
            self.assertEqual(model["services"]["worker"]["image"], IMAGE)
            self.assertEqual(model["services"]["worker"]["volumes"][1]["source"], str(root / "data" / "bot"))
            for image in ("ghcr.io/other/bot@sha256:" + "a" * 64, "ghcr.io/paulbeka/polytrader:latest", IMAGE + ";evil"):
                with self.assertRaises(ValueError):
                    deploy(root, "bot", image, dry_run=True)
            with self.assertRaises(ValueError):
                deploy(root, "../escape", IMAGE, dry_run=True)
            self.assertFalse((root / "releases").exists())

    def test_failed_release_restores_previous_image_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare(root)
            calls = []
            old = deploy(root, "bot", IMAGE, command=calls.append, check=lambda *_: True)
            (root / "configs" / "bot.toml").write_text("second configuration")
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                deploy(root, "bot", IMAGE.replace("a" * 64, "b" * 64), command=calls.append, check=lambda *_: False, timeout=0)
            current = json.loads((root / "releases" / "bot" / "current.json").read_text())
            self.assertEqual(current, old)
            self.assertEqual(Path(old["config"]).read_text(), "first configuration")
            self.assertIn(old["compose"], calls[-1])
            self.assertIn("up", calls[-1])
            self.assertTrue(all("polytrader-bot" in c for c in calls if "compose" in c))

    @unittest.skipIf(os.name == "nt", "Windows terminate does not deliver cooperative SIGTERM")
    def test_sigterm_cancels_root_task_and_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready, finished = Path(tmp) / "ready", Path(tmp) / "finished"
            code = '''import asyncio,sys
from pathlib import Path
from polytrader.ops.runtime import until_stopped
async def main():
    Path(sys.argv[1]).touch()
    try:
        await asyncio.Event().wait()
    finally:
        Path(sys.argv[2]).write_text('checkpoint saved')
try:
    asyncio.run(until_stopped(main()))
except asyncio.CancelledError:
    pass
'''
            process = subprocess.Popen([sys.executable, "-c", code, str(ready), str(finished)])
            import time
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertEqual(finished.read_text(), "checkpoint saved")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


@unittest.skipUnless(importlib.util.find_spec("streamlit"), "Install ops extra to test dashboard")
class DashboardTests(unittest.TestCase):
    def test_empty_and_populated_dashboard(self):
        from streamlit.testing.v1 import AppTest
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, POLYTRADER_DATA=tmp):
            dashboard = ROOT / "src" / "polytrader" / "ops" / "dashboard.py"
            app = AppTest.from_file(str(dashboard), default_timeout=20).run()
            self.assertFalse(app.exception)
            session(tmp, rows=[dict(type="exit", pnl="1", holding_seconds=2)])
            session(tmp, "time_arbitrage", "two", [dict(event="opportunity_open", calculation={"conservative_profit": "3"})])
            with closing(connect(Path(tmp) / "ops" / "index.sqlite3")) as con:
                collect(con, tmp)
                save_report(Path(tmp) / "ops" / "reports", daily(con, NOW.date()))
            app = AppTest.from_file(str(dashboard), default_timeout=20).run()
            self.assertFalse(app.exception)
            self.assertGreaterEqual(len(app.dataframe), 2)
            next(b for b in app.button if b.label == "Manage bots").click().run()
            next(s for s in app.selectbox if s.label == "Choose a bot").set_value("two").run()
            self.assertFalse(app.exception)
            app.radio(key="navigation").set_value("Reports").run()
            self.assertFalse(app.exception)
            next(r for r in app.radio if r.label == "Period").set_value("Last 7 days").run()
            self.assertFalse(app.exception)
            app.radio(key="navigation").set_value("Deployments").run()
            self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
