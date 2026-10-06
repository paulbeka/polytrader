"""Host-side, locked deployment of one allowlisted instance and immutable release."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import tomllib

from .collector import read_json
from .files import atomic_json
from .locking import lock
from .worker import STRATEGIES

NAME = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")


def definition(root, instance, image=None):
    root = Path(root).resolve()
    if not NAME.fullmatch(instance):
        raise ValueError("Invalid instance name")
    registry = tomllib.loads((root / "instances.toml").read_text(encoding="utf-8"))
    row = registry["instances"][instance]
    if row["strategy"] not in STRATEGIES:
        raise ValueError("Strategy is not allowlisted")
    config = (root / row["config"]).resolve()
    if not config.is_relative_to(root / "configs") or not config.is_file():
        raise ValueError("Config must be an existing file under configs/")
    repository = registry["image_repository"]
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+", repository):
        raise ValueError("Expected a GHCR repository")
    if image is not None and not re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", image):
        raise ValueError("Image must use the allowlisted repository and sha256 digest")
    return row, config


def compose(root, instance, strategy, image, config, release):
    root = Path(root).resolve()
    return {"services": {"worker": {
        "image": image, "init": True, "restart": "unless-stopped", "stop_grace_period": "45s",
        "user": "10001:10001", "read_only": True, "tmpfs": ["/tmp"],
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
        "command": ["python", "-m", "polytrader.ops", "worker", strategy, "--config", "/config/bot.toml", "--output", "/data"],
        "environment": {"POLYTRADER_INSTANCE": instance, "POLYTRADER_IMAGE": image, "POLYTRADER_RELEASE": release,
                        "POLYTRADER_SEGMENT_BYTES": "16777216", "POLYTRADER_MIN_FREE_BYTES": "1073741824"},
        "volumes": [{"type": "bind", "source": str(config), "target": "/config/bot.toml", "read_only": True},
                    {"type": "bind", "source": str(root / "data" / instance), "target": "/data"}],
        "logging": {"driver": "local", "options": {"max-size": "10m", "max-file": "3"}},
        "healthcheck": {"test": ["CMD", "python", "-m", "polytrader.ops", "health"],
                        "interval": "15s", "timeout": "5s", "start_period": "90s", "retries": 4},
    }}}


def execute(command):
    subprocess.run(command, check=True, timeout=300)


def healthy(directory, release, *, since=None):
    paths = sorted(Path(directory).glob("*/status.json"), reverse=True)
    if not paths:
        return False
    status = read_json(paths[0], {})
    if status.get("release") != release or not status.get("heartbeat_at"):
        return False
    if since and status.get("started_at", "") < since:
        return False
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(status["heartbeat_at"])).total_seconds()
    return (age < 30 and status.get("process_state") == "running" and status.get("transport_healthy")
            and status.get("logging_ok"))


def deploy(root, instance, image=None, *, action="deploy", dry_run=False, timeout=120, command=execute,
           check=healthy, progress=lambda *_: None, expected_hash=None, expected_current=None):
    root = Path(root).resolve()
    row, source = definition(root, instance, image if action == "deploy" else None)
    state_dir = root / "releases" / instance
    current_path = state_dir / "current.json"
    previous_path = state_dir / "previous.json"
    with lock(root / "locks" / (instance + ".lock")):
        pending_path = state_dir / "pending.json"
        if pending_path.exists():
            raise ValueError("Interrupted deployment needs recovery before another operation")
        current = read_json(current_path)
        if expected_current is not None and (current or {}).get("release", "") != expected_current:
            raise ValueError("Deployed release changed after preview; preview again")
        def docker(release, *args):
            command(["docker", "compose", "-p", "polytrader-" + instance, "-f", release["compose"], *args])
        def wait_for(release, since=None):
            deadline = time.monotonic() + timeout
            def ready():
                if check is healthy:
                    return healthy(root / "data" / instance, release["release"], since=since)
                return check(root / "data" / instance, release["release"])
            while not ready():
                if time.monotonic() >= deadline:
                    raise RuntimeError("Startup heartbeat/transport check timed out (market activity is not required)")
                time.sleep(2)

        if action in {"stop", "status", "start", "restart"}:
            if not current:
                raise ValueError("Instance has no deployed release")
            if dry_run:
                return current
            progress(instance, action)
            if action in {"stop", "status"}:
                docker(current, "stop" if action == "stop" else "ps")
            else:
                started = datetime.now(timezone.utc).isoformat() if action == "restart" else None
                docker(current, "up", "-d", *(["--force-recreate"] if action == "restart" else []))
                progress(instance, "checking_health")
                wait_for(current, started)
            if action != "status":
                atomic_json(state_dir / "desired.json", {"state": "stopped" if action == "stop" else "running"})
            return current
        if action == "rollback":
            target = read_json(previous_path)
            if not target:
                raise ValueError("No previous release to roll back to")
        else:
            if image is None:
                raise ValueError("Image digest required")
            content = source.read_bytes()
            config_hash = hashlib.sha256(content).hexdigest()
            if expected_hash and config_hash != expected_hash:
                raise ValueError("Configuration changed after preview; preview again")
            if current and current["image"] == image and current["config_hash"] == config_hash and not dry_run:
                progress(instance, "unchanged")
                return current
            release_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + config_hash[:8]
            directory = state_dir / release_id
            target = dict(image=image, config_hash=config_hash, release=release_id,
                          compose=str(directory / "compose.json"), config=str(directory / "bot.toml"))
            model = compose(root, instance, row["strategy"], image, Path(target["config"]), release_id)
            if dry_run:
                return model
            directory.mkdir(parents=True)
            Path(target["config"]).write_bytes(content)
            atomic_json(target["compose"], model)
            output = root / "data" / instance
            output.mkdir(parents=True, exist_ok=True)
            if os.name != "nt" and os.geteuid() == 0:
                os.chown(output, 10001, 10001)
            progress(instance, "pulling")
            command(["docker", "pull", image])
            # Validate public discovery before stopping the current instance.
            progress(instance, "validating")
            command(["docker", "run", "--rm", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
                     "--mount", f"type=bind,source={target['config']},target=/config/bot.toml,readonly",
                     image, "python", "-m", "polytrader.ops", "worker", row["strategy"],
                     "--config", "/config/bot.toml", "--output", "/tmp", "--validate"])
        if dry_run:
            return target
        atomic_json(pending_path, {"previous": current, "target": target})
        try:
            # Same Compose project/service prevents duplicate logical instances.
            progress(instance, "starting")
            started = datetime.now(timezone.utc).isoformat()
            docker(target, "up", "-d", "--force-recreate", "--remove-orphans")
            progress(instance, "checking_health")
            wait_for(target, started)
        except BaseException as exc:
            progress(instance, "rolling_back")
            try:
                docker(target, "logs", "--tail", "100")
            except Exception:
                pass
            try:
                docker(target, "stop")
                if current:
                    started = datetime.now(timezone.utc).isoformat()
                    docker(current, "up", "-d", "--force-recreate", "--remove-orphans")
                    wait_for(current, started)
                pending_path.unlink()
                progress(instance, "rolled_back" if current else "failed_first_deploy")
            except Exception as recovery:
                progress(instance, "rollback_failed")
                raise RuntimeError(f"{exc}; recovery failed: {recovery}") from exc
            raise
        if current:
            atomic_json(previous_path, current)
        atomic_json(current_path, target)
        atomic_json(state_dir / "desired.json", {"state": "running"})
        pending_path.unlink()
        progress(instance, "succeeded")
        return target


def recover(root, instance, *, command=execute, check=healthy, timeout=120):
    """After host interruption, retain a committed release or restore the previous one."""
    root = Path(root)
    if not NAME.fullmatch(instance):
        raise ValueError("Invalid instance name")
    with lock(root / "locks" / (instance + ".lock")):
        directory = root / "releases" / instance
        pending_path = directory / "pending.json"
        pending = read_json(pending_path)
        if not pending:
            return "nothing_pending"
        current = read_json(directory / "current.json")
        committed = current and current.get("release") == pending["target"]["release"]
        target = current if committed else pending.get("previous")
        def docker(release, *args):
            command(["docker", "compose", "-p", "polytrader-" + instance, "-f", release["compose"], *args])
        if not committed:
            docker(pending["target"], "stop")
        if target:
            started = datetime.now(timezone.utc).isoformat()
            docker(target, "up", "-d", "--force-recreate")
            deadline = time.monotonic() + timeout
            while not (healthy(root / "data" / instance, target["release"], since=started) if check is healthy
                       else check(root / "data" / instance, target["release"])):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Interrupted deployment recovery failed health check")
                time.sleep(2)
            atomic_json(directory / "desired.json", {"state": "running"})
        pending_path.unlink()
        return "committed" if committed else "restored_previous" if target else "stopped_failed_first_deploy"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["deploy", "rollback", "stop", "status", "start", "restart"])
    parser.add_argument("instance")
    parser.add_argument("image", nargs="?")
    parser.add_argument("--root", type=Path, default=Path("/srv/polytrader"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(deploy(args.root, args.instance, args.image, action=args.action, dry_run=args.dry_run), indent=2))


def ssh_main():
    """Forced SSH command entrypoint: no arbitrary flags, paths or shell execution."""
    args = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", ""))
    if len(args) not in (2, 3) or args[0] not in {"deploy", "rollback", "stop", "status"}:
        raise ValueError("Expected action instance [image-digest]")
    if not NAME.fullmatch(args[1]) or (len(args) == 3 and args[0] != "deploy"):
        raise ValueError("Invalid deployment request")
    if args[0] == "deploy" and len(args) != 3:
        raise ValueError("Deploy requires image digest")
    deploy("/srv/polytrader", args[1], args[2] if len(args) == 3 else None, action=args[0])


if __name__ == "__main__":
    main()
