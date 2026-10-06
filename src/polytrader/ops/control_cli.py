"""Host commands for setup and day-to-day bot management."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from . import deployment, management
from .collector import read_json
from .files import atomic_json, encode
from .locking import lock


def platform_up(root, label=None, *, rollback=False):
    root = Path(root)
    folder = root / "platform"
    if not (folder / "compose.yaml").exists():
        raise ValueError("Run deploy/bootstrap.sh first to install platform/compose.yaml")
    with lock(root / "locks/management.lock"):
        old = read_json(folder / "current.json")
        target = read_json(folder / "previous.json") if rollback else management.release(root, label) if label or not old else old
        if not target:
            raise ValueError("No previous platform release")
        env = {**os.environ, "POLYTRADER_ROOT": str(root), "POLYTRADER_IMAGE": target["platform_image"]}
        command = ["docker", "compose", "--project-directory", str(folder), "-f", str(folder / "compose.yaml")]
        subprocess.run([*command, "pull"], env=env, check=True)
        try:
            subprocess.run([*command, "up", "-d", "--wait", "--wait-timeout", "150"], env=env, check=True)
        except subprocess.CalledProcessError:
            if old:
                env["POLYTRADER_IMAGE"] = old["platform_image"]
                subprocess.run([*command, "up", "-d", "--wait", "--wait-timeout", "150"], env=env, check=True)
            raise
        if old:
            atomic_json(folder / "previous.json", old)
        atomic_json(folder / "current.json", target)
        return target


def main(argv=None):
    parser = argparse.ArgumentParser(prog="polytraderctl", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("POLYTRADER_ROOT", "/srv/polytrader")))
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Import an immutable release; preserve existing installation")
    init.add_argument("--release-file", type=Path, required=True)
    commands.add_parser("doctor", help="Check server prerequisites and configuration")
    commands.add_parser("recover", help="Reconcile interrupted deployments")
    status = commands.add_parser("status")
    status.add_argument("instance", nargs="?")
    status.add_argument("--json", action="store_true")
    for action in sorted(management.ACTIONS):
        p = commands.add_parser(action)
        p.add_argument("instances", nargs="*")
        p.add_argument("--all", action="store_true")
        p.add_argument("--release")
        p.add_argument("--preview", action="store_true")
    bot = commands.add_parser("bot").add_subparsers(dest="operation", required=True)
    for action in ("add", "update"):
        p = bot.add_parser(action)
        p.add_argument("name")
        p.add_argument("--strategy", choices=management.STRATEGIES, required=True)
        p.add_argument("--config", type=Path, required=True)
        p.add_argument("--display-name", default="")
    conf = commands.add_parser("config").add_subparsers(dest="operation", required=True)
    p = conf.add_parser("validate")
    p.add_argument("--strategy", choices=management.STRATEGIES, required=True)
    p.add_argument("--config", type=Path, required=True)
    plat = commands.add_parser("platform")
    plat.add_argument("operation", choices=("up", "update", "rollback"))
    plat.add_argument("--release")
    logs = commands.add_parser("logs")
    logs.add_argument("instance")
    logs.add_argument("--tail", type=int, default=100)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "init":
            result = management.initialize(root, json.loads(args.release_file.read_text(encoding="utf-8")))
        elif args.command == "doctor":
            result = management.doctor(root)
            for row in result:
                print(f"{'OK' if row['ok'] else 'FAIL'}  {row['check']}: {row['detail']}")
            return 0 if all(r["ok"] for r in result) else 1
        elif args.command == "bot":
            result = management.save_bot(root, args.name, args.strategy, args.config.read_text(encoding="utf-8"),
                                         args.display_name, update=args.operation == "update")
        elif args.command == "config":
            result = {"config_hash": management.validate_config(args.strategy, args.config.read_text(encoding="utf-8"), root),
                      "note": "Settings valid. Current market discovery is checked during deployment."}
        elif args.command == "platform":
            result = platform_up(root, args.release, rollback=args.operation == "rollback")
        elif args.command == "logs":
            deployment.definition(root, args.instance)
            current = read_json(root / "releases" / args.instance / "current.json")
            if not current:
                raise ValueError("Bot has never been deployed")
            if not 1 <= args.tail <= 10000:
                raise ValueError("--tail must be between 1 and 10000")
            subprocess.run(["docker", "compose", "-p", "polytrader-" + args.instance, "-f", current["compose"],
                            "logs", "--tail", str(args.tail)], check=True)
            return 0
        else:
            manager = management.Manager(root)
            if args.command == "status":
                result = manager.snapshot(containers=True)
                if args.instance:
                    result["bots"] = {args.instance: result["bots"][args.instance]}
                if not args.json:
                    print(f"{'BOT':28} {'DESIRED':10} {'CONTAINER':12} IMAGE")
                    for name, row in result["bots"].items():
                        current = row.get("current") or {}
                        print(f"{name:28} {row['desired']:10} {row.get('container', 'unknown'):12} {current.get('image', 'never deployed')}")
                    return 0
            elif args.command == "recover":
                result = manager.reconcile()
            else:
                if args.all and args.instances:
                    raise ValueError("Choose named bots or --all")
                names = list(management.registry(root).get("instances", {})) if args.all else args.instances
                plan = management.preview(root, args.command, names, args.release)
                if args.preview:
                    result = plan
                else:
                    identity = manager.submit(plan, str(uuid.uuid4()))
                    manager.execute(identity)
                    result = next(job for job in manager.jobs() if job["id"] == identity)
                    print(json.dumps(json.loads(encode(result)), indent=2))
                    return 0 if result["state"] == "succeeded" else 1
        print(json.dumps(json.loads(encode(result)), indent=2))
        return 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f"polytraderctl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
