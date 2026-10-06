"""Shared host management: immutable releases, reviewed plans and durable jobs.

The research index is never written here. The host-only control database stores
requests; all mutations share one OS lock, including CLI and HTTP callers.
"""

from contextlib import closing
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import tomllib
import uuid

from . import deployment
from .collector import read_json
from .files import atomic_json, encode, utc
from .locking import lock
from .worker import STRATEGIES, configuration

REPOSITORY = "ghcr.io/paulbeka/polytrader"
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
ACTIONS = {"deploy", "start", "stop", "restart", "rollback"}


def registry(root):
    path = Path(root) / "instances.toml"
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "image_repository": REPOSITORY, "instances": {}}


def write_registry(root, value):
    lines = ["image_repository = " + json.dumps(value["image_repository"]), ""]
    for name, row in sorted(value.get("instances", {}).items()):
        lines.append(f"[instances.{name}]")
        for key in ("strategy", "config", "display_name"):
            if key in row:
                lines.append(f"{key} = {json.dumps(row[key])}")
        lines.append("")
    path = Path(root) / "instances.toml"
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(path)
    # Directory-mounted file: atomic updates remain visible in the collector.
    atomic_json(Path(root) / "data/ops/instances.json", value.get("instances", {}))


def validate_manifest(value, repository=REPOSITORY):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Expected release manifest schema_version 1")
    if not LABEL.fullmatch(str(value.get("label", ""))):
        raise ValueError("Invalid release label")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+", repository):
        raise ValueError("Invalid image repository")
    for role in ("worker_image", "platform_image"):
        if not re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", str(value.get(role, ""))):
            raise ValueError(f"{role} must be an allowlisted immutable image digest")
    if value.get("config_schema") != 1 or value.get("data_schema") != 1:
        raise ValueError("Release is incompatible with config/data schema 1")
    if not value.get("architectures") or not set(value["architectures"]) <= {"amd64", "arm64"}:
        raise ValueError("Release must declare supported architectures")
    if not isinstance(value.get("commit"), str) or not value["commit"]:
        raise ValueError("Release must identify its commit")
    datetime.fromisoformat(value["built_at"])
    return value


def initialize(root, manifest):
    root = Path(root).resolve()
    for name in ("configs", "releases", "locks", "control", "catalog", "data/ops", "run"):
        (root / name).mkdir(parents=True, exist_ok=True)
    with lock(root / "locks/management.lock"):
        value = registry(root)
        manifest = validate_manifest(manifest, value["image_repository"])
        path = root / "catalog" / (manifest["label"] + ".json")
        existing = read_json(path)
        if existing and existing != manifest:
            raise ValueError("Release labels are immutable; use a new label")
        atomic_json(path, manifest)
        write_registry(root, value)
        defaults = root / "control/defaults.json"
        if not defaults.exists():
            atomic_json(defaults, {"release": manifest["label"]})
    return manifest


def release(root, label=None):
    root = Path(root)
    label = label or read_json(root / "control/defaults.json", {}).get("release")
    if not isinstance(label, str) or not LABEL.fullmatch(label):
        raise ValueError("Import a release first: polytraderctl init --release-file release.json")
    value = read_json(root / "catalog" / (label + ".json"))
    if not value:
        raise ValueError("Unknown release; import its release.json first")
    return validate_manifest(value, registry(root)["image_repository"])


def templates():
    directory = Path(__file__).resolve().parents[1] / "bot/config"
    return {"lead_follower": (directory / "lead_follower.example.toml").read_text(encoding="utf-8"),
            "time_arbitrage": (directory / "example.time_arbitrage.toml").read_text(encoding="utf-8")}


def validate_config(strategy, text, root):
    if strategy not in STRATEGIES or not isinstance(text, str) or len(text.encode()) > 128 * 1024:
        raise ValueError("Choose a supported strategy and a configuration below 128 KiB")
    import tempfile
    directory = Path(root) / "configs"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        path = Path(tmp) / "bot.toml"
        path.write_text(text, encoding="utf-8")
        configuration(strategy, path, Path(root) / "data/validation")
    return hashlib.sha256(text.encode()).hexdigest()


def save_bot(root, name, strategy, text, display_name="", *, update=False):
    root = Path(root)
    if not deployment.NAME.fullmatch(name):
        raise ValueError("Use a lowercase bot name: letters, digits and hyphens (48 characters max)")
    if not isinstance(display_name, str) or len(display_name) > 100:
        raise ValueError("Display name is limited to 100 characters")
    with lock(root / "locks/management.lock"):
        value = registry(root)
        old = value.get("instances", {}).get(name)
        if bool(old) != update:
            raise ValueError("Bot already exists" if old else "Bot does not exist")
        if old and old["strategy"] != strategy:
            raise ValueError("Create a new bot to change strategy")
        digest = validate_config(strategy, text, root)
        destination = root / "configs" / f"{name}-{digest}.toml"
        if not destination.exists():
            destination.write_text(text, encoding="utf-8", newline="\n")
            destination.chmod(0o644)
        value.setdefault("instances", {})[name] = {
            "strategy": strategy, "config": destination.relative_to(root).as_posix(),
            "display_name": display_name or name}
        write_registry(root, value)
        return value["instances"][name]


def preview(root, action, instances, label=None):
    root = Path(root)
    if action not in ACTIONS or not isinstance(instances, list) or not instances:
        raise ValueError("Select an action and at least one bot")
    manifest = release(root, label) if action == "deploy" else None
    targets = []
    for name in sorted(set(instances)):
        row, path = deployment.definition(root, name, manifest["worker_image"] if manifest else None)
        current = read_json(root / "releases" / name / "current.json")
        previous = read_json(root / "releases" / name / "previous.json")
        if action != "deploy" and not current:
            raise ValueError(f"{name} has never been deployed")
        if action == "rollback" and not previous:
            raise ValueError(f"{name} has no previous release")
        config = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if action == "deploy":
            validate_config(row["strategy"], config, root)
        old_config = Path(current["config"]).read_text(encoding="utf-8") if current else ""
        target = previous if action == "rollback" else current
        target_image = manifest["worker_image"] if manifest else target["image"]
        target_config = Path(target["config"]).read_text(encoding="utf-8") if action == "rollback" else config
        targets.append(dict(instance=name, strategy=row["strategy"], config_hash=digest,
                            current=current, previous=previous, image=target_image,
                            unchanged=bool(action == "deploy" and current and current["image"] == target_image
                                           and current["config_hash"] == digest),
                            config_diff="".join(difflib.unified_diff(old_config.splitlines(True), target_config.splitlines(True),
                                                                   fromfile="deployed", tofile="target"))))
    return dict(action=action, release=manifest["label"] if manifest else None, targets=targets)


class Manager:
    def __init__(self, root, *, runner=None, health=None):
        self.root = Path(root).resolve()
        (self.root / "control").mkdir(parents=True, exist_ok=True)
        self.runner = runner or self.command
        self.health = health or deployment.healthy
        with self.database() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, created TEXT NOT NULL,
                updated TEXT NOT NULL, state TEXT NOT NULL, source TEXT NOT NULL,
                plan TEXT NOT NULL, result TEXT NOT NULL, log TEXT NOT NULL)""")

    def database(self):
        con = sqlite3.connect(self.root / "control/jobs.sqlite3", timeout=10)
        con.row_factory = sqlite3.Row
        # Return a closing transaction context rather than leak a connection.
        from contextlib import contextmanager
        @contextmanager
        def session():
            with closing(con), con:
                yield con
        return session()

    def jobs(self):
        with self.database() as con:
            return [dict(row, plan=json.loads(row["plan"]), result=json.loads(row["result"]))
                    for row in con.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 100")]

    def next_queued(self):
        with self.database() as con:
            row = con.execute("SELECT id FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            return row[0] if row else None

    def submit(self, plan, request_id, source="cli"):
        if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", request_id):
            raise ValueError("Invalid idempotency key")
        # Retry of an accepted request returns that job even after state has changed.
        with self.database() as con:
            old = con.execute("SELECT id,plan FROM jobs WHERE request_id=?", (request_id,)).fetchone()
        if old:
            if json.loads(old["plan"]) != plan:
                raise ValueError("Request ID was already used for a different request")
            return old["id"]
        actual = preview(self.root, plan["action"], [t["instance"] for t in plan["targets"]], plan.get("release"))
        if actual != plan:
            raise ValueError("Deployment or configuration changed; preview again")
        identity = str(uuid.uuid4())
        with self.database() as con:
            con.execute("INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",
                        (identity, request_id, utc(), utc(), "queued", source, encode(actual), "{}", ""))
            accepted = con.execute("SELECT id,plan FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if json.loads(accepted["plan"]) != plan:
                raise ValueError("Request ID was already used for a different request")
            return accepted["id"]

    def update(self, identity, state=None, result=None, message=None):
        with self.database() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            log = row["log"]
            if message:
                log = (log + utc() + " " + message + "\n")[-64000:]
            con.execute("UPDATE jobs SET updated=?,state=?,result=?,log=? WHERE id=?",
                        (utc(), state or row["state"], encode(result) if result is not None else row["result"], log, identity))

    def command(self, args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=300)
        if getattr(self, "active_job", None):
            self.update(self.active_job, message=(result.stdout + result.stderr)[-16000:])
        if result.returncode:
            raise RuntimeError(f"{args[0]} exited {result.returncode}: {(result.stderr or result.stdout)[-2000:]}")
        return result.stdout

    def execute(self, identity):
        with lock(self.root / "locks/management.lock"):
            with self.database() as con:
                row = con.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            if not row or row["state"] != "queued":
                return
            plan = json.loads(row["plan"])
            self.active_job = identity
            results = {t["instance"]: "untouched" for t in plan["targets"]}
            self.update(identity, "running", results)
            try:
                actual = preview(self.root, plan["action"], list(results), plan.get("release"))
                if actual != plan:
                    raise ValueError("State changed since preview; preview and submit again")
                # Validate every image/config before replacing any selected container.
                for target in plan["targets"]:
                    if (self.root / "releases" / target["instance"] / "pending.json").exists():
                        raise ValueError(f"{target['instance']} requires polytraderctl recover")
                if plan["action"] == "deploy":
                    pulled = set()
                    for target in plan["targets"]:
                        if target["unchanged"]:
                            continue
                        name = target["instance"]
                        self.update(identity, message=f"{name}: validating before batch")
                        if target["image"] not in pulled:
                            self.runner(["docker", "pull", target["image"]])
                            pulled.add(target["image"])
                        _, config = deployment.definition(self.root, name, target["image"])
                        self.runner(["docker", "run", "--rm", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
                                     "--mount", f"type=bind,source={config},target=/config/bot.toml,readonly",
                                     target["image"], "python", "-m", "polytrader.ops", "worker", target["strategy"],
                                     "--config", "/config/bot.toml", "--output", "/tmp", "--validate"])
                def progress(name, stage):
                    results[name] = stage
                    self.update(identity, result=results, message=f"{name}: {stage}")
                for target in plan["targets"]:
                    name = target["instance"]
                    results[name] = "running"
                    deployment.deploy(self.root, name, target["image"], action=plan["action"], command=self.runner,
                                      check=self.health, progress=progress, expected_hash=target["config_hash"],
                                      expected_current=(target["current"] or {}).get("release", ""))
                    if results[name] not in {"unchanged", "succeeded"}:
                        progress(name, "succeeded")
                self.update(identity, "succeeded", results)
            except Exception as exc:
                for name, state in results.items():
                    if state not in {"untouched", "unchanged", "succeeded", "rolled_back", "rollback_failed", "failed_first_deploy"}:
                        results[name] = "failed"
                self.update(identity, "failed", results, str(exc))
            finally:
                self.active_job = None

    def reconcile(self):
        with lock(self.root / "locks/management.lock"):
            recovery = {}
            for name in registry(self.root).get("instances", {}):
                if (self.root / "releases" / name / "pending.json").exists():
                    try:
                        recovery[name] = deployment.recover(self.root, name, command=self.runner, check=self.health)
                    except Exception as exc:
                        recovery[name] = "recovery_failed: " + str(exc)
            with self.database() as con:
                interrupted = [r[0] for r in con.execute("SELECT id FROM jobs WHERE state='running'")]
            for identity in interrupted:
                self.update(identity, "interrupted", recovery,
                            "Controller stopped during operation; reconciled containers. Review before resubmitting.")
            return recovery

    def snapshot(self, *, containers=False):
        value = registry(self.root)
        bots = {}
        for name, row in value.get("instances", {}).items():
            current = read_json(self.root / "releases" / name / "current.json")
            bots[name] = {**row, "current": current,
                          "desired": read_json(self.root / "releases" / name / "desired.json", {}).get("state", "unknown"),
                          "recovery_required": (self.root / "releases" / name / "pending.json").exists()}
        if containers:
            try:
                raw = subprocess.run(["docker", "ps", "-a", "--format", "{{json .}}"],
                                     capture_output=True, text=True, check=True, timeout=10).stdout
                for line in raw.splitlines():
                    item = json.loads(line)
                    labels = dict(part.split("=", 1) for part in item.get("Labels", "").split(",") if "=" in part)
                    project = labels.get("com.docker.compose.project", "")
                    name = project.removeprefix("polytrader-")
                    if name in bots and labels.get("com.docker.compose.service") == "worker":
                        bots[name]["container"] = item.get("State", "unknown")
                for row in bots.values():
                    row.setdefault("container", "absent")
            except (OSError, ValueError, subprocess.SubprocessError):
                for row in bots.values():
                    row["container"] = "unknown"
        return {"bots": bots, "releases": [read_json(p) for p in sorted((self.root / "catalog").glob("*.json"))],
                "jobs": self.jobs(), "generated_at": utc(), "templates": templates()}


def doctor(root):
    root = Path(root)
    checks = []
    def record(name, fn):
        try:
            detail = fn()
            checks.append({"check": name, "ok": True, "detail": str(detail)})
        except Exception as exc:
            checks.append({"check": name, "ok": False, "detail": str(exc)})
    def docker():
        subprocess.run(["docker", "info"], capture_output=True, text=True, check=True, timeout=15)
        version = subprocess.run(["docker", "compose", "version", "--short"], capture_output=True, text=True, check=True).stdout.strip()
        parts = tuple(int(x) for x in version.lstrip("v").split(".")[:2])
        if parts < (2, 24):
            raise ValueError("Docker Compose 2.24+ required")
        return version
    def disk():
        free = shutil.disk_usage(root).free
        if free < 1024**3:
            raise ValueError("Less than 1 GiB free")
        return f"{free / 1024**3:.1f} GiB free"
    def image():
        manifest = release(root)
        arch = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
        if arch not in manifest["architectures"]:
            raise ValueError(f"Release does not support {arch}")
        for digest in {manifest["worker_image"], manifest["platform_image"]}:
            subprocess.run(["docker", "manifest", "inspect", digest], capture_output=True, check=True, timeout=60)
        return manifest["label"]
    def permissions():
        if not os.access(root / "configs", os.W_OK):
            raise ValueError("Host account cannot write configs")
        if os.name != "nt":
            for path in (root / "data", root / "data/ops"):
                stat = path.stat()
                if stat.st_uid != 10001 or not stat.st_mode & 0o200:
                    raise ValueError(f"{path} must be writable by container UID 10001")
        return "Host and container directories writable"
    record("Docker and Compose", docker)
    record("Storage", disk)
    record("Permissions", permissions)
    record("Release / registry / architecture", image)
    for name, row in registry(root).get("instances", {}).items():
        record("Config: " + name, lambda n=name, r=row: validate_config(r["strategy"], deployment.definition(root, n)[1].read_text(encoding="utf-8"), root))
    import socket
    def port():
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", 8501)) == 0:
                return "Port 8501 in use; check it is the Polytrader dashboard"
            return "Port 8501 available"
    record("Dashboard port", port)
    def controller():
        if not (root / "run/control.sock").exists():
            raise ValueError("Controller socket missing; check systemctl status polytrader-controller")
        from .controller import request
        request(root / "run/control.sock", "/snapshot")
        return "Private controller responds"
    record("Controller", controller)
    if (root / "platform/current.json").exists():
        def collector():
            from .presentation import age_seconds
            age = age_seconds(read_json(root / "data/ops/collector_status.json", {}).get("heartbeat_at"))
            if age is None or age > 60:
                raise ValueError("Collector heartbeat missing or stale")
            return f"Updated {age:.0f}s ago"
        record("Collector", collector)
    return checks
