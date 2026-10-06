from contextlib import closing
from datetime import timedelta
from decimal import Decimal
import json
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from polytrader.ops import deployment
from polytrader.ops.management import (Manager, initialize, preview, registry, release, save_bot,
                                      templates, validate_manifest)
from polytrader.ops.files import atomic_json
from polytrader.ops.collector import collect
from polytrader.ops.reports import period, html_report, csv_report
from polytrader.ops.presentation import fleet
from polytrader.ops.storage import connect, runs
from test_ops import IMAGE, NOW, session


def manifest(label="test-release", image=IMAGE):
    return dict(schema_version=1, label=label, commit="abc123", built_at=NOW.isoformat(), worker_image=image,
                platform_image=image, config_schema=1, data_schema=1, architectures=["amd64"])


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        initialize(self.root, manifest())
        self.config = templates()["lead_follower"]
        save_bot(self.root, "lead", "lead_follower", self.config, "Lead bot")
        self.calls = []
        self.manager = Manager(self.root, runner=self.calls.append, health=lambda *_: True)

    def job(self, action="deploy", names=None, label=None, request_id="request-1234"):
        plan = preview(self.root, action, names or ["lead"], label)
        identity = self.manager.submit(plan, request_id)
        self.manager.execute(identity)
        return next(j for j in self.manager.jobs() if j["id"] == identity)

    def test_initialize_preserves_configs_and_labels_are_immutable(self):
        initialize(self.root, manifest())
        self.assertIn("lead", registry(self.root)["instances"])
        with self.assertRaisesRegex(ValueError, "immutable"):
            initialize(self.root, manifest(image=IMAGE.replace("a" * 64, "b" * 64)))
        self.assertEqual(release(self.root)["worker_image"], IMAGE)
        for value in ("latest", IMAGE + "; touch bad", "ghcr.io/other/image@sha256:" + "a" * 64):
            with self.assertRaises(ValueError):
                validate_manifest(manifest(image=value))
        with self.assertRaises(ValueError):
            save_bot(self.root, "../escape", "lead_follower", self.config)

    def test_persisted_jobs_idempotency_and_unchanged_deploy(self):
        plan = preview(self.root, "deploy", ["lead"])
        identity = self.manager.submit(plan, "request-same")
        self.assertEqual(self.manager.submit(plan, "request-same"), identity)
        self.manager.execute(identity)
        self.assertEqual(self.manager.submit(plan, "request-same"), identity)
        self.assertEqual(Manager(self.root).jobs()[0]["state"], "succeeded")
        self.calls.clear()
        result = self.job(request_id="second-request")
        self.assertEqual(result["result"]["lead"], "unchanged")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.job("stop", request_id="stop-request")["state"], "succeeded")
        self.assertEqual(self.manager.snapshot()["bots"]["lead"]["desired"], "stopped")
        self.assertEqual(self.job("start", request_id="start-request")["state"], "succeeded")

    def test_preview_rejects_stale_config_and_does_not_change_container(self):
        plan = preview(self.root, "deploy", ["lead"])
        save_bot(self.root, "lead", "lead_follower", self.config + "\n# reviewed change\n", update=True)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.manager.submit(plan, "request-stale")
        self.assertEqual(self.calls, [])

    def test_batch_preflights_every_bot_before_replacement(self):
        save_bot(self.root, "other", "lead_follower", self.config)
        def fail(args):
            self.calls.append(args)
            if args[:2] == ["docker", "run"] and "other-" in " ".join(args):
                raise RuntimeError("market validation failed")
        self.manager.runner = fail
        result = self.job(names=["lead", "other"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(set(result["result"].values()), {"untouched"})
        self.assertFalse(any("compose" in args for args in self.calls))

    def test_batch_failure_keeps_successful_earlier_bots(self):
        for name in ("other", "third"):
            save_bot(self.root, name, "lead_follower", self.config)
        def fail(args):
            self.calls.append(args)
            if "polytrader-other" in args and "up" in args:
                raise RuntimeError("cannot create container")
        self.manager.runner = fail
        result = self.job(names=["lead", "other", "third"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["result"], {"lead": "succeeded", "other": "failed_first_deploy", "third": "untouched"})

    def test_rollback_and_recovery_restore_saved_configuration(self):
        first = self.job()
        self.assertEqual(first["state"], "succeeded")
        old = self.manager.snapshot()["bots"]["lead"]["current"]
        initialize(self.root, manifest("new", IMAGE.replace("a" * 64, "b" * 64)))
        save_bot(self.root, "lead", "lead_follower", self.config + "\n# revised\n", update=True)
        self.job(label="new", request_id="new-request")
        new = self.manager.snapshot()["bots"]["lead"]["current"]
        self.job("rollback", request_id="rollback-request")
        self.assertEqual(self.manager.snapshot()["bots"]["lead"]["current"], old)
        self.assertNotEqual(Path(old["config"]).read_text(), Path(new["config"]).read_text())
        atomic_json(self.root / "releases/lead/pending.json", {"previous": old, "target": new})
        result = self.manager.reconcile()
        self.assertEqual(result["lead"], "restored_previous")
        self.assertFalse((self.root / "releases/lead/pending.json").exists())

    def test_failed_recovery_blocks_further_mutation(self):
        self.job()
        old = self.manager.snapshot()["bots"]["lead"]["current"]
        atomic_json(self.root / "releases/lead/pending.json", {"previous": old, "target": old})
        def fail(*_):
            raise RuntimeError("Docker unavailable")
        self.manager.runner = fail
        self.assertIn("recovery_failed", self.manager.reconcile()["lead"])
        result = self.job(request_id="blocked-request")
        self.assertEqual(result["state"], "failed")
        self.assertTrue(self.manager.snapshot()["bots"]["lead"]["recovery_required"])

    def test_queued_request_rechecks_release_before_execution(self):
        plan = preview(self.root, "deploy", ["lead"])
        identity = self.manager.submit(plan, "queued-request")
        self.job(request_id="other-request")
        self.calls.clear()
        self.manager.execute(identity)
        job = next(j for j in self.manager.jobs() if j["id"] == identity)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(self.calls, [])

    def test_controller_routes_validate_and_deduplicate(self):
        from http.server import ThreadingHTTPServer
        from http.client import HTTPConnection
        from polytrader.ops.controller import Handler
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.manager = self.manager
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def call(path, body, authorized=True, origin=False):
                    con = HTTPConnection(*server.server_address, timeout=5)
                    headers = {"Content-Type": "application/json"}
                    if authorized:
                        headers["X-Polytrader-Control"] = "1"
                    if origin:
                        headers["Origin"] = "https://untrusted.example"
                    con.request("POST", path, json.dumps(body), headers)
                    response = con.getresponse()
                    result = response.status, json.loads(response.read())
                    con.close()
                    return result
                body = {"action": "deploy", "instances": ["lead"]}
                self.assertEqual(call("/preview", body, authorized=False)[0], 403)
                self.assertEqual(call("/preview", body, origin=True)[0], 403)
                status, plan = call("/preview", body)
                self.assertEqual(status, 200)
                status, job = call("/jobs", {"plan": plan, "request_id": "browser-request"})
                self.assertEqual(status, 200)
                self.assertEqual(call("/jobs", {"plan": plan, "request_id": "browser-request"})[1], job)
                self.assertEqual(call("/config/read", {"name": "../secret"})[0], 400)
                self.assertEqual(call("/shell", {"command": "whoami"})[0], 404)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    @unittest.skipUnless(importlib.util.find_spec("streamlit"), "Install ops extra to test dashboard")
    def test_dashboard_management_preview_and_submit(self):
        from streamlit.testing.v1 import AppTest
        from polytrader.ops.management import preview as plan_preview
        dashboard = Path(__file__).resolve().parents[1] / "src/polytrader/ops/dashboard.py"
        def endpoint(socket, path, payload=None):
            if path == "/snapshot":
                return self.manager.snapshot()
            if path == "/preview":
                return plan_preview(self.root, payload["action"], payload["instances"], payload.get("release"))
            if path == "/jobs":
                return {"id": self.manager.submit(payload["plan"], payload["request_id"], "private-dashboard")}
            if path == "/config/read":
                return {"text": self.config}
            if path == "/config/validate":
                return {"note": "Valid settings"}
            raise AssertionError(path)
        with patch.dict(os.environ, POLYTRADER_DATA=str(self.root / "data"), POLYTRADER_CONTROL_SOCKET="test-socket"), \
                patch("polytrader.ops.controller.request", side_effect=endpoint), \
                patch("polytrader.ops.dashboard_views.request", side_effect=endpoint):
            app = AppTest.from_file(str(dashboard), default_timeout=20).run()
            self.assertFalse(app.exception)
            app.radio(key="navigation").set_value("Bots").run()
            self.assertFalse(app.exception)
            next(b for b in app.button if b.label == "Check configuration").click().run()
            self.assertFalse(app.exception)
            app.radio(key="navigation").set_value("Deployments").run()
            app.multiselect[0].set_value(["lead"]).run()
            next(b for b in app.button if b.label == "Preview changes").click().run()
            self.assertFalse(app.exception)
            next(b for b in app.button if b.label == "Apply reviewed changes").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(self.manager.jobs()), 1)
            self.assertEqual(self.manager.jobs()[0]["source"], "private-dashboard")

    def test_failed_upgrade_checks_successful_rollback(self):
        self.job()
        old = self.manager.snapshot()["bots"]["lead"]["current"]
        stages = []
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            deployment.deploy(self.root, "lead", IMAGE.replace("a" * 64, "b" * 64), timeout=0,
                              command=self.calls.append, check=lambda _, release_id: release_id == old["release"],
                              progress=lambda _, stage: stages.append(stage))
        self.assertEqual(stages[-1], "rolled_back")
        self.assertFalse((self.root / "releases/lead/pending.json").exists())
        self.assertEqual(self.manager.snapshot()["bots"]["lead"]["current"], old)

    def test_restart_health_must_belong_to_new_process(self):
        path = self.root / "data/lead/run"
        path.mkdir(parents=True)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        atomic_json(path / "status.json", {"release": "release", "heartbeat_at": now.isoformat(),
                    "started_at": (now - timedelta(hours=2)).isoformat(), "process_state": "running",
                    "transport_healthy": True, "logging_ok": True})
        self.assertTrue(deployment.healthy(path.parent, "release"))
        self.assertFalse(deployment.healthy(path.parent, "release", since=(now - timedelta(seconds=5)).isoformat()))

    @unittest.skipIf(os.name == "nt", "Unix socket service is deployed on Linux")
    def test_real_unix_controller_transport_and_shutdown(self):
        from polytrader.ops.controller import request
        process = subprocess.Popen([sys.executable, "-m", "polytrader.ops.controller", "--root", str(self.root)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            socket_path = self.root / "run/control.sock"
            deadline = time.monotonic() + 10
            while not socket_path.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(socket_path.exists())
            self.assertEqual(socket_path.stat().st_mode & 0o777, 0o660)
            snapshot = request(socket_path, "/snapshot")
            self.assertIn("lead", snapshot["bots"])
            plan = request(socket_path, "/preview", {"action": "deploy", "instances": ["lead"]})
            self.assertEqual(plan["targets"][0]["image"], IMAGE)
            process.terminate()
            self.assertEqual(process.wait(timeout=10), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


class ReportTests(unittest.TestCase):
    def test_scheduled_reports_include_json_registry_and_weekly_export(self):
        from polytrader.ops.service import run_collector
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_file = root / "ops/instances.json"
            atomic_json(registry_file, {"quiet": {"strategy": "lead_follower"}})
            with patch.dict(os.environ, POLYTRADER_INSTANCES=str(registry_file)):
                run_collector(root, root / "ops/index.sqlite3", once=True)
            report = json.loads((root / "ops/reports/last-7-days.json").read_text())
            self.assertEqual(report["groups"][0]["instance"], "quiet")
            self.assertEqual(report["groups"][0]["coverage"], "No observations")
            self.assertTrue((root / "ops/reports/last-7-days.html").exists())

    def test_period_exact_values_episodes_filtering_and_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(Path(tmp) / "index.sqlite3")) as con:
            path = session(tmp, rows=[dict(type="exit", pnl="0.1"), dict(type="exit", pnl="0.2"), dict(type="exit", pnl="0")])
            atomic_json(path / "summary.json", {"open_positions": [{"id": "open"}]})
            session(tmp, "time_arbitrage", "arb", [dict(event="opportunity_open", calculation={"conservative_profit": "10"}),
                                                      dict(event="opportunity_update", calculation={"conservative_profit": "12"})])
            collect(con, tmp)
            report = period(con, NOW.date() - timedelta(days=6), NOW.date(), configured={"missing": "lead_follower"})
            groups = {g["instance"]: g for g in report["groups"]}
            self.assertEqual(groups["one"]["closed_paper_pnl"], Decimal("0.3"))
            self.assertEqual(groups["one"]["breakevens"], 1)
            self.assertEqual(groups["arb"]["opportunity_episodes"], 1)
            self.assertEqual(groups["arb"]["max_quoted_profit"], Decimal("12"))
            self.assertIsNone(groups["missing"]["closed_paper_pnl"])
            self.assertEqual(len(next(s for s in report["current_snapshots"] if s["instance"] == "one")["open_positions"]), 1)
            filtered = period(con, NOW.date(), NOW.date(), instances=["one"])
            self.assertEqual([g["instance"] for g in filtered["groups"]], ["one"])
            self.assertIn("0.3", csv_report(filtered))
            rendered = html_report(filtered)
            self.assertIn("<table>", rendered)
            self.assertNotIn("<pre>", rendered)
            self.assertNotIn("quoted opportunities</h2>", rendered)

    def test_quiet_healthy_bot_and_boundary_observations(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(Path(tmp) / "index.sqlite3")) as con:
            path = session(tmp)
            atomic_json(path / "status.json", {"heartbeat_at": NOW.isoformat(), "process_state": "running", "transport_healthy": True})
            collect(con, tmp)
            key = runs(con)[0]["key"]
            con.execute("INSERT INTO health VALUES(?,?,?)", (key, (NOW + timedelta(seconds=10)).isoformat(), 1))
            report = period(con, NOW.date(), NOW.date())
            self.assertEqual(report["groups"][0]["closed_paper_pnl"], Decimal(0))
            self.assertAlmostEqual(report["groups"][0]["healthy_observation_hours"], 10 / 3600)

    def test_stale_collector_is_not_a_failed_worker(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(Path(tmp) / "index.sqlite3")) as con:
            session(tmp)
            collect(con, tmp)
            bot = fleet(runs(con), {}, collector_fresh=False)[0]
            self.assertNotEqual(bot["status"], "Running")
            stopped = fleet([], {"never": "lead_follower"}, {"bots": {"never": {"desired": "stopped"}}})[0]
            self.assertEqual(stopped["status"], "Stopped")


if __name__ == "__main__":
    unittest.main()
