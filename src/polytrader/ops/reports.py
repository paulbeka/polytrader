"""Period reports shared by scheduled exports and the interactive dashboard."""

from collections import Counter
import csv
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import html
import io
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .files import atomic_json, utc
from .storage import runs

NOTE = ("Lead/follower P&L is hypothetical and fee-excluded. Arbitrage values are quoted estimates, "
        "never earned or additive P&L. Healthy hours count consecutive observed heartbeats; gaps are unknown. "
        "Open positions are current snapshots, not reconstructed historical positions.")
FIELDS = ("instance", "strategy", "version", "config_hash", "coverage", "healthy_observation_hours",
          "closed_paper_pnl", "closed_trades", "wins", "losses", "breakevens", "opportunity_episodes",
          "max_quoted_profit", "mean_holding_seconds")


def _group(key):
    return dict(instance=key[0], strategy=key[1], version=key[2], config_hash=key[3],
                events=Counter(), rejection_reasons=Counter(), closed_paper_pnl=Decimal(0),
                closed_trades=0, wins=0, losses=0, breakevens=0, opportunity_episodes=0,
                max_quoted_profit=None, holding_seconds=[], healthy_observation_hours=0,
                heartbeat_samples=0, coverage="No observations")


def period(con, first, last, timezone_name="Europe/London", *, instances=None, configured=None, compare=True):
    if last < first:
        raise ValueError("End date must follow start date")
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(first, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    selected = None if instances is None else set(instances)
    history = [r for r in runs(con) if selected is None or r["instance"] in selected]
    by_key = {r["key"]: r for r in history}
    groups, samples_by_run, run_health = {}, {}, []
    def key_for(run):
        return run["instance"], run["strategy"], run["version"], run["config_hash"]
    for row in con.execute("SELECT run_key,utc,healthy FROM health WHERE utc>=? AND utc<=? ORDER BY run_key,utc",
                           ((start - timedelta(seconds=30)).isoformat(), (end + timedelta(seconds=30)).isoformat())):
        if row["run_key"] in by_key:
            samples_by_run.setdefault(row["run_key"], []).append((datetime.fromisoformat(row["utc"]), row["healthy"]))
    for run_key, samples in samples_by_run.items():
        count = sum(start <= stamp < end for stamp, _ in samples)
        seconds = sum(max(0, (min(b, end) - max(a, start)).total_seconds())
                      for (a, ok), (b, next_ok) in zip(samples, samples[1:])
                      if ok and next_ok and 0 <= (b - a).total_seconds() <= 30)
        if not count and not seconds:
            continue
        run = by_key[run_key]
        key = key_for(run)
        group = groups.setdefault(key, _group(key))
        group["healthy_observation_hours"] += seconds / 3600
        group["heartbeat_samples"] += count
        from .collector import state
        run_health.append(dict(instance=run["instance"], run_id=run["run_id"], strategy=run["strategy"],
                               state=state(run), healthy_observation_hours=seconds / 3600,
                               unresolved_positions=len(run["summary"].get("open_positions", [])),
                               summary_as_of_report=run["summary"]))
    for row in con.execute("SELECT * FROM events WHERE utc>=? AND utc<? ORDER BY utc", (start.isoformat(), end.isoformat())):
        run = by_key.get(row["run_key"])
        if not run:
            continue
        key = key_for(run)
        group = groups.setdefault(key, _group(key))
        kind = row["kind"]
        group["events"][kind] += 1
        data = json.loads(row["payload"])
        if kind == "burst_candidate":
            group["rejection_reasons"].update(data.get("rejection_reasons", []))
        elif kind == "entry_rejected":
            group["rejection_reasons"].update([data.get("reason", "unspecified")])
        if run["strategy"] == "lead_follower" and kind == "exit":
            amount = Decimal(row["amount"])
            group["closed_paper_pnl"] += amount
            group["closed_trades"] += 1
            group["wins"] += amount > 0
            group["losses"] += amount < 0
            group["breakevens"] += amount == 0
            if data.get("holding_seconds") is not None:
                group["holding_seconds"].append(data["holding_seconds"])
        if run["strategy"] == "time_arbitrage":
            group["opportunity_episodes"] += kind == "opportunity_open"
            if row["amount"] is not None:
                amount = Decimal(row["amount"])
                group["max_quoted_profit"] = max(amount, group["max_quoted_profit"]) if group["max_quoted_profit"] is not None else amount
    present = {key[0] for key in groups}
    for name, row in (configured or {}).items():
        if name not in present and (selected is None or name in selected):
            strategy = row if isinstance(row, str) else row["strategy"]
            key = (name, strategy, None, None)
            groups[key] = _group(key)
    result = []
    for group in groups.values():
        holds = group.pop("holding_seconds")
        group["mean_holding_seconds"] = sum(holds) / len(holds) if holds else None
        health_observed = bool(group["heartbeat_samples"] or group["healthy_observation_hours"])
        observed = bool(group["events"] or health_observed)
        group["coverage"] = "Observed; gaps unknown" if health_observed else "Events only; health unknown" if observed else "No observations"
        if group["strategy"] != "lead_follower" or not observed:
            group["closed_paper_pnl"] = None
        if not observed:
            for key in ("closed_trades", "wins", "losses", "breakevens", "opportunity_episodes"):
                group[key] = None
        result.append(group)
    result.sort(key=lambda g: (g["strategy"], g["instance"], str(g["version"]), str(g["config_hash"])))
    snapshots, seen = [], set()
    for run in sorted(history, key=lambda r: r["run_id"], reverse=True):
        if run["instance"] not in seen:
            seen.add(run["instance"])
            snapshots.append({"instance": run["instance"], "run": run["run_id"], "as_of": run["indexed_at"],
                              "open_positions": run["summary"].get("open_positions"),
                              "active_opportunities": run["summary"].get("active_opportunities")})
    report = dict(date=str(first), end_date=str(last), timezone=timezone_name, utc_start=start, utc_end=end,
                  generated_at=utc(), groups=result, run_health=run_health, current_snapshots=snapshots, note=NOTE)
    trades = sum(g["closed_trades"] or 0 for g in result if g["strategy"] == "lead_follower")
    episodes = sum(g["opportunity_episodes"] or 0 for g in result if g["strategy"] == "time_arbitrage")
    missing = sum(g["coverage"] == "No observations" for g in result)
    report["summary"] = f"{len({g['instance'] for g in result})} bots: {trades} closed paper trades and {episodes} new quoted opportunity episodes. {missing} bots have no recorded observations."
    if compare:
        length = (last - first).days + 1
        previous = period(con, first - timedelta(days=length), first - timedelta(days=1), timezone_name,
                          instances=instances, configured=configured, compare=False)
        prior = {(g["instance"], g["strategy"], g["version"], g["config_hash"]): g for g in previous["groups"]}
        for group in result:
            before = prior.get((group["instance"], group["strategy"], group["version"], group["config_hash"]))
            group["paper_pnl_change"] = (group["closed_paper_pnl"] - before["closed_paper_pnl"]
                if before and group["closed_paper_pnl"] is not None and before["closed_paper_pnl"] is not None else None)
        report["previous_period"] = {"start": previous["date"], "end": previous["end_date"], "summary": previous["summary"]}
    return report


def daily(con, day, timezone_name="Europe/London", **kwargs):
    return period(con, day, day, timezone_name, **kwargs)


def csv_report(report):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(report["groups"])
    return buffer.getvalue()


def html_report(report):
    escape = lambda value: html.escape("Unavailable" if value is None else str(value))
    blocks = []
    for strategy, title, columns in (
        ("lead_follower", "Lead / follower · paper trading", ("closed_paper_pnl", "closed_trades", "wins", "losses", "breakevens")),
        ("time_arbitrage", "Time arbitrage · quoted opportunities", ("opportunity_episodes", "max_quoted_profit")),
    ):
        groups = [g for g in report["groups"] if g["strategy"] == strategy]
        if not groups:
            continue
        keys = ("instance", "version", "config_hash", "coverage", "healthy_observation_hours", *columns)
        head = "".join("<th>" + escape(k.replace("_", " ").title()) + "</th>" for k in keys)
        body = "".join("<tr>" + "".join("<td>" + escape(g.get(k)) + "</td>" for k in keys) + "</tr>" for g in groups)
        blocks.append(f"<h2>{title}</h2><div class='table'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")
    previous = report.get("previous_period")
    comparison = (f"<p>Previous period ({escape(previous['start'])} to {escape(previous['end'])}): "
                  f"{escape(previous['summary'])}</p>") if previous else ""
    return ("<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Polytrader research report</title><style>body{font:16px system-ui;background:#f4f6fa;color:#17243b;"
            "max-width:1200px;margin:40px auto;padding:24px}h1{font-size:32px}h2{margin-top:32px}"
            ".table{overflow:auto;background:white;border-radius:12px}table{border-collapse:collapse;width:100%}"
            "th,td{padding:14px;text-align:left;border-bottom:1px solid #e1e7ef}td{overflow-wrap:anywhere}"
            "th{font-size:12px;text-transform:uppercase}small{color:#53647e}footer{margin-top:32px}</style><body>"
            f"<small>POLYTRADER / RESEARCH REPORT</small><h1>{escape(report['date'])} to {escape(report['end_date'])}</h1>"
            f"<p>{escape(report['summary'])}</p>{comparison}<p><small>Calendar: {escape(report['timezone'])} · "
            f"Generated: {escape(report['generated_at'])}</small></p>" + "".join(blocks) +
            f"<footer>{escape(report['note'])}</footer></body></html>")


def save_report(root, report, *, name=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    stem = root / (name or report["date"])
    atomic_json(stem.with_suffix(".json"), report)
    for suffix, content in (("csv", csv_report(report)), ("html", html_report(report))):
        temporary = stem.with_suffix("." + suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(stem.with_suffix("." + suffix))
