"""Readable, framework-independent bot summaries and health labels."""

from datetime import datetime, timezone
from decimal import Decimal

from .collector import state

STRATEGY_NAMES = {"lead_follower": "Lead / follower", "time_arbitrage": "Time arbitrage"}


def age_seconds(stamp):
    try:
        return max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds())
    except (TypeError, ValueError):
        return None


def money(value):
    return "Unavailable" if value is None else f"${Decimal(str(value)):,.2f}"


def fleet(history, configured, snapshot=None, *, collector_fresh=True):
    managed = (snapshot or {}).get("bots", {})
    names = set(configured) | set(managed) | {r["instance"] for r in history}
    releases = {r["worker_image"]: r["label"] for r in (snapshot or {}).get("releases", []) if r}
    pending = {t["instance"] for j in (snapshot or {}).get("jobs", []) if j["state"] in {"queued", "running"}
               for t in j["plan"]["targets"]}
    result = []
    for name in sorted(names):
        bot = managed.get(name, {})
        current = bot.get("current") or {}
        candidates = sorted([r for r in history if r["instance"] == name], key=lambda r: r["run_id"], reverse=True)
        matching = [r for r in candidates if r["status"].get("release") == current.get("release")]
        run = next(iter(matching or candidates), None)
        config = configured.get(name, {})
        strategy = bot.get("strategy") or (run["strategy"] if run else config if isinstance(config, str) else config.get("strategy"))
        container, desired = bot.get("container", "unknown"), bot.get("desired", "unknown")
        label, detail = "Unknown", "Waiting for health observations."
        if bot.get("recovery_required"):
            label, detail = "Failed", "An interrupted deployment requires recovery."
        elif name in pending:
            label, detail = "Starting", "A management operation is queued or running; see Deployments."
        elif desired == "stopped":
            label, detail = ("Degraded", "Container is still running despite a stop request.") if container == "running" else ("Stopped", "Intentionally stopped.")
        elif container in {"exited", "dead", "absent"} and current:
            label, detail = "Failed", f"Expected a running bot; container is {container}."
        elif current and not matching:
            label, detail = "Starting", "Waiting for observations from the current deployment."
        elif run:
            raw = state(run)
            states = {"healthy": ("Running", "Process and transport are healthy; market activity may be quiet."),
                      "warming_up": ("Warming up", "Collecting the initial market history."),
                      "reconnecting": ("Reconnecting", "Waiting for the market transport to reconnect."),
                      "connected_missing_books": ("Degraded", "Connected, but some market books are unavailable."),
                      "interrupted_or_unreachable": ("Unknown", "Worker heartbeat is stale; check container and collector status."),
                      "stopped": ("Stopped", "Run ended."), "completed_legacy": ("Stopped", "Historical completed run.")}
            if raw == "unknown_legacy":
                label, detail = "Unknown", "Historical run has no process heartbeat."
            elif raw in {"normal", "completed", "keyboard_interrupt", "cancelled", "sigterm", "interrupted"}:
                label, detail = "Stopped", "Run ended: " + raw.replace("_", " ")
            else:
                label, detail = states.get(raw, ("Failed", raw.replace("_", " ").capitalize()))
            if not collector_fresh and label not in {"Stopped", "Failed"}:
                label, detail = "Unknown", "Collector data is stale; the bot may still be running."
        else:
            label, detail = "Never started", "Configuration is ready; deploy a release to start collecting."
        summary = run["summary"] if run else {}
        markets = "—"
        if run:
            config = run["metadata"].get("config", {})
            groups = config.get("groups", config.get("chains", [])) if isinstance(config, dict) else []
            if isinstance(groups, list):
                markets = ", ".join(str(g.get("name") or g.get("event") or g.get("id", "")) for g in groups if isinstance(g, dict)) or "See configuration"
        metric = money(summary.get("closed_pnl")) + " paper P&L" if strategy == "lead_follower" else money(summary.get("peak_observed_profit")) + " best quote"
        result.append(dict(instance=name, name=bot.get("display_name", name), strategy=strategy, status=label,
                           explanation=detail, markets=markets, release=releases.get(current.get("image"), "Local / legacy" if run else "Not deployed"),
                           heartbeat=run["status"].get("heartbeat_at") if run else None, result=metric,
                           run=run, desired=desired, container=container))
    priority = {"Failed": 0, "Degraded": 1, "Unknown": 2, "Reconnecting": 3}
    return sorted(result, key=lambda row: (priority.get(row["status"], 5), row["name"]))
