"""Streamlit views. All host mutations go through the private controller."""

from datetime import datetime, timedelta
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

import streamlit as st

from .controller import request
from .files import encode
from .presentation import STRATEGY_NAMES, money
from .reports import period, daily, csv_report, html_report, NOTE
from .storage import rows


def navigate(page, bot=None):
    st.session_state["navigation"] = page
    if bot:
        st.session_state["deploy_bots"] = [bot]


def table_rows(bots):
    return [{"Bot": b["name"], "Strategy": STRATEGY_NAMES.get(b["strategy"], b["strategy"]),
             "Status": b["status"], "Markets": b["markets"], "Release": b["release"],
             "Latest run result": b["result"], "Last heartbeat (UTC)": b["heartbeat"]} for b in bots]


def overview(bots, con, root):
    st.header("Your bots, at a glance")
    st.caption("Health, recent activity and results across your research fleet.")
    counts = [len(bots), sum(b["status"] == "Running" for b in bots),
              sum(b["status"] in {"Failed", "Degraded", "Unknown", "Reconnecting"} for b in bots),
              sum(b["status"] == "Stopped" for b in bots)]
    for column, label, value in zip(st.columns(4), ["Total bots", "Running", "Need attention", "Stopped"], counts):
        column.metric(label, value)
    for col, label, target in zip(st.columns(3), ["Manage bots", "Deploy a release", "Read reports"], ["Bots", "Deployments", "Reports"]):
        if col.button(label, on_click=navigate, args=(target,), width="stretch"):
            st.rerun()
    if not bots:
        st.info("Welcome to Polytrader. Open Bots to add a configuration, then deploy your first release.")
        st.code("sudo polytraderctl init --release-file ./release.json\nsudo polytraderctl platform up", language="bash")
        return
    st.dataframe(table_rows(bots), hide_index=True, width="stretch")
    attention = [b for b in bots if b["status"] in {"Failed", "Degraded", "Unknown", "Reconnecting"}]
    for bot in attention:
        st.warning(f"{bot['name']}: {bot['explanation']}")
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Recent activity")
        if con:
            events = rows(con, "SELECT e.utc AS Time,r.instance AS Bot,e.kind AS Activity,e.market AS Market FROM events e JOIN runs r ON e.run_key=r.key ORDER BY e.utc DESC LIMIT 12")
            st.dataframe(events, hide_index=True, width="stretch")
        else:
            st.info("Activity appears after the collector indexes the first run.")
    with right:
        st.subheader("Today’s research")
        if con:
            today = datetime.now(ZoneInfo("Europe/London")).date()
            report = daily(con, today, configured={b["instance"]: b["strategy"] for b in bots})
            st.write(report["summary"])
            st.caption("Open Reports for comparisons and downloadable summaries.")
        st.caption("Paper trading and quoted opportunities. No live orders.")


def config_form(socket, snapshot, *, bot=None):
    if not socket:
        st.info("Bot setup is available when the host controller is connected. Server command: polytraderctl bot add.")
        return
    editing = bot is not None
    prefix = "edit_" + bot["instance"] if editing else "new"
    strategy = bot["strategy"] if editing else st.selectbox("Strategy", list(STRATEGY_NAMES), format_func=STRATEGY_NAMES.get, key=prefix + "strategy")
    st.caption("Lead / follower follows activity between related markets. Time arbitrage watches price differences across ordered outcomes.")
    if editing:
        cache_key = prefix + "source"
        if cache_key not in st.session_state:
            try:
                st.session_state[cache_key] = request(socket, "/config/read", {"name": bot["instance"]})["text"]
            except (ValueError, OSError) as exc:
                st.error(str(exc))
                return
        template = st.session_state[cache_key]
    else:
        template = snapshot.get("templates", {}).get(strategy, "")
    upload = st.file_uploader("Use an existing TOML configuration", type="toml", key=prefix + "upload")
    if upload:
        try:
            template = upload.getvalue().decode("utf-8")
        except UnicodeError:
            st.error("Configuration must be UTF-8 text.")
            return
    with st.form(prefix + "form"):
        name = st.text_input("Bot ID", value=bot["instance"] if editing else "", disabled=editing,
                             placeholder="lead-main", help="A stable lowercase name, using letters, numbers and hyphens.")
        display = st.text_input("Display name", value=bot["name"] if editing else "", placeholder="Ukraine · lead / follower")
        st.caption("Review the event and market names in the template. Comments explain warm-up, position size, limits and strategy settings.")
        text = st.text_area("Configuration (TOML)", template, height=320, key=prefix + strategy + str(upload.file_id if upload else "template"))
        validate = st.form_submit_button("Check configuration")
        save = st.form_submit_button("Save configuration" if editing else "Add bot", type="primary")
    try:
        if validate:
            result = request(socket, "/config/validate", {"strategy": strategy, "text": text})
            st.success(result["note"])
        if save:
            request(socket, "/bots", {"name": name, "strategy": strategy, "text": text,
                                      "display_name": display, "update": editing})
            st.success("Configuration saved. Open Deployments to preview and deploy it.")
            st.session_state.pop(prefix + "source", None)
    except (ValueError, OSError) as exc:
        st.error(str(exc))


def bots_view(bots, history, con, root, socket, snapshot):
    st.header("Bots")
    with st.expander("Add a bot", expanded=not bots):
        config_form(socket, snapshot)
    if not bots:
        return
    by_name = {b["instance"]: b for b in bots}
    name = st.selectbox("Choose a bot", list(by_name), format_func=lambda n: by_name[n]["name"])
    bot = by_name[name]
    st.subheader(bot["name"])
    st.write(f"**{bot['status']}** · {bot['explanation']}")
    st.caption(f"{STRATEGY_NAMES.get(bot['strategy'])} · {bot['release']} · Desired: {bot['desired']} · Container: {bot['container']}")
    if st.button("Manage this bot", on_click=navigate, args=("Deployments", name)):
        st.rerun()
    summary_tab, activity_tab, config_tab, history_tab = st.tabs(["Summary", "Activity", "Configuration", "Run history"])
    run = bot["run"]
    with summary_tab:
        if not run:
            st.info("No research run yet. Open Deployments to start this bot.")
        else:
            summary = run["summary"]
            if bot["strategy"] == "lead_follower":
                labels = ["Closed paper P&L", "Closed trades", "Wins / losses", "Open or unresolved"]
                values = [money(summary.get("closed_pnl")), summary.get("closed", "Unavailable"),
                          f"{summary.get('wins', '—')} / {summary.get('losses', '—')}", summary.get("unresolved", len(summary.get("open_positions", [])))]
                st.caption("Hypothetical results for the latest run; fees excluded. Interrupted positions remain unresolved.")
            else:
                labels = ["Opportunity episodes", "Active opportunities", "Best quoted estimate", "Pair evaluations"]
                values = [summary.get("episodes_opened", "Unavailable"), summary.get("active_opportunities", "Unavailable"),
                          money(summary.get("peak_observed_profit")), summary.get("pair_evaluations", "Unavailable")]
                st.caption("Quoted estimates for the latest run. These are not earned profit and must not be summed.")
            for col, label, value in zip(st.columns(4), labels, values):
                col.metric(label, value)
            records = [{"Follower": r.get("follower"), "Signals": r.get("detected"),
                        "Closed trades": r.get("closed"), "Paper P&L": money(r.get("closed_pnl")),
                        "Unresolved": r.get("unresolved")} for r in summary.get("by_follower", [])]
            records = records or [{"Reason": k, "Count": v} for k, v in summary.get("blocked_by_reason", {}).items()]
            if records:
                st.dataframe(records, hide_index=True, width="stretch")
            if bot["strategy"] == "lead_follower":
                if all(summary.get(k) is not None for k in ("closed", "wins", "losses")):
                    st.caption(f"Breakeven trades: {summary['closed'] - summary['wins'] - summary['losses']}")
                from collections import Counter
                reasons = Counter()
                for row in summary.get("by_follower", []):
                    reasons.update(row.get("candidates", {}).get("rejection_reasons", {}))
                if reasons:
                    st.caption("Common candidate rejection reasons")
                    st.dataframe([{"Reason": reason.replace("_", " "), "Count": count} for reason, count in reasons.most_common(8)], hide_index=True)
            if summary.get("open_positions"):
                st.subheader("Current position snapshot")
                st.dataframe(summary["open_positions"], hide_index=True, width="stretch")
            if con:
                points = rows(con, "SELECT utc,healthy FROM health WHERE run_key=? ORDER BY utc DESC LIMIT 720", (run["key"],))
                if points:
                    st.caption("Observed transport health · 1 connected / 0 disconnected. Gaps remain unknown.")
                    st.line_chart(points, x="utc", y="healthy")
            with st.expander("Technical details"):
                st.json({"summary": summary, "health": run["status"]})
    with activity_tab:
        if con and run:
            kind = st.text_input("Event type contains", placeholder="signal, exit, opportunity…")
            market = st.text_input("Market contains", placeholder="Optional market name")
            events = rows(con, "SELECT utc,kind,market,amount,payload FROM events WHERE run_key=? AND coalesce(kind,'') LIKE ? AND coalesce(market,'') LIKE ? ORDER BY utc DESC,offset DESC LIMIT 500",
                          (run["key"], "%" + kind + "%", "%" + market + "%"))
            st.dataframe([{k: v for k, v in e.items() if k != "payload"} for e in events], hide_index=True, width="stretch")
            st.caption("Latest 500 matching events, shown in UTC. Downloads include full event details.")
            st.download_button("Download events", encode(events), "events.json", "application/json")
        else:
            st.info("No indexed activity yet.")
    with config_tab:
        config_form(socket, snapshot, bot=bot)
        if run:
            with st.expander("Configuration used by the current run"):
                st.json(run["metadata"])
    with history_tab:
        selected = [r for r in history if r["instance"] == name]
        st.dataframe([{"Run": r["run_id"], "Version": r["version"], "Configuration": r["config_hash"],
                       "Commit": r["git_sha"]} for r in selected], hide_index=True, width="stretch")
        if selected:
            chosen = st.selectbox("Download artifacts from run", selected, format_func=lambda r: r["run_id"])
            path = Path(chosen["path"]).resolve()
            if path.is_relative_to(root):
                for filename in ("metadata.json", "manifest.json", "summary.json", "checkpoint.json", "runtime.json", "backup_coverage.json"):
                    artifact = path / filename
                    if artifact.is_file() and artifact.stat().st_size < 2 * 1024**2:
                        st.download_button("Download " + filename, artifact.read_bytes(), filename, key=name + filename)
        st.caption("Starting or restarting creates a fresh run; interrupted positions are not resumed.")


def deployments(bots, socket, snapshot):
    st.header("Deployments")
    st.caption("Preview a release, update selected bots and follow each deployment. Platform updates use the server command.")
    if not socket:
        st.info("The host controller is not connected. Website management becomes available after server bootstrap.")
        st.code("sudo polytraderctl deploy --all --release <release-label> --preview\nsudo polytraderctl deploy --all --release <release-label>", language="bash")
        return
    with st.expander("Import a release"):
        upload = st.file_uploader("release.json from Test and publish", type="json")
        if st.button("Import release", disabled=upload is None):
            import json
            try:
                request(socket, "/releases", {"manifest": json.loads(upload.getvalue())})
                st.success("Release imported. Refresh the page to select it.")
            except (ValueError, OSError) as exc:
                st.error(str(exc))
    releases = {r["label"]: r for r in snapshot.get("releases", []) if r}
    by_name = {b["instance"]: b for b in bots}
    action = st.selectbox("Action", ["deploy", "stop", "start", "restart", "rollback"],
                          format_func=lambda a: {"deploy": "Deploy / update", "stop": "Stop", "start": "Start", "restart": "Restart", "rollback": "Roll back"}[a])
    names = st.multiselect("Bots to change", list(by_name), format_func=lambda n: by_name[n]["name"], key="deploy_bots")
    label = st.selectbox("Release", list(releases)) if action == "deploy" and releases else None
    if label:
        item = releases[label]
        st.caption(f"Built {item['built_at']} · Commit {item['commit'][:12]}")
        with st.expander("Image details"):
            st.code(item["worker_image"])
    fingerprint = encode([action, names, label])
    if st.session_state.get("deployment_selection") != fingerprint:
        st.session_state.pop("deployment_plan", None)
        st.session_state["deployment_selection"] = fingerprint
    if st.button("Preview changes", disabled=not names or (action == "deploy" and not label)):
        try:
            st.session_state["deployment_plan"] = request(socket, "/preview", {"action": action, "instances": names, "release": label})
            st.session_state["deployment_request"] = str(uuid.uuid4())
        except (ValueError, OSError) as exc:
            st.error(str(exc))
    plan = st.session_state.get("deployment_plan")
    if plan:
        st.subheader("Review changes")
        st.dataframe([{"Bot": t["instance"], "Change": "Unchanged" if t["unchanged"] else action,
                       "Current image": (t["current"] or {}).get("image", "Not deployed"), "Target image": t["image"]}
                      for t in plan["targets"]], hide_index=True, width="stretch")
        for target in plan["targets"]:
            if target["config_diff"]:
                with st.expander(target["instance"] + " · configuration changes"):
                    st.code(target["config_diff"], language="diff")
        st.caption("Bots are changed one at a time. A failure stops the batch; successful earlier updates remain. Each new process starts a fresh research run.")
        if st.button("Apply reviewed changes", type="primary"):
            try:
                job = request(socket, "/jobs", {"plan": plan, "request_id": st.session_state["deployment_request"]})
                st.success("Request accepted: " + job["id"])
                st.session_state.pop("deployment_plan", None)
            except (ValueError, OSError) as exc:
                st.error(str(exc))
    st.subheader("Deployment history")
    jobs = snapshot.get("jobs", [])
    if not jobs:
        st.info("Deployment requests and their outcomes will appear here.")
    for job in jobs[:20]:
        with st.expander(f"{job['created'][:19]} · {job['plan']['action']} · {job['state']}", expanded=job["state"] in {"queued", "running", "failed"}):
            st.write(job["result"])
            st.caption("Requested through " + job["source"])
            st.code(job["log"] or "Waiting to start…", language="text")


def reports_view(bots, con):
    st.header("Reports")
    st.caption("Research results by bot, strategy and configuration. Calendar days use Europe/London time.")
    if not con:
        st.info("Reports appear after the first collection. Daily and weekly exports are generated automatically.")
        return
    today = datetime.now(ZoneInfo("Europe/London")).date()
    preset = st.radio("Period", ["Today", "Yesterday", "Last 7 days", "Custom"], horizontal=True)
    first, last = (today, today) if preset == "Today" else (today - timedelta(days=1), today - timedelta(days=1)) if preset == "Yesterday" else (today - timedelta(days=6), today)
    if preset == "Custom":
        a, b = st.columns(2)
        first = a.date_input("From", first)
        last = b.date_input("To", last)
    names = [b["instance"] for b in bots]
    selected = st.multiselect("Include bots", names, default=names)
    if last < first:
        st.error("End date must follow start date.")
        return
    if not selected:
        st.info("Select at least one bot.")
        return
    report = period(con, first, last, instances=selected, configured={b["instance"]: b["strategy"] for b in bots})
    st.write(report["summary"])
    previous = report.get("previous_period", {})
    st.caption(f"Previous period ({previous.get('start')} to {previous.get('end')}): {previous.get('summary')}")
    for strategy, label in STRATEGY_NAMES.items():
        groups = [g for g in report["groups"] if g["strategy"] == strategy]
        if not groups:
            continue
        st.subheader(label)
        display = []
        for group in groups:
            item = {"Bot": group["instance"]}
            if strategy == "lead_follower":
                item.update({"Paper P&L": money(group["closed_paper_pnl"]), "Closed trades": group["closed_trades"],
                             "Wins / losses / even": " / ".join(str(group[k]) if group[k] is not None else "—" for k in ("wins", "losses", "breakevens")),
                             "Change vs prior": money(group.get("paper_pnl_change"))})
            else:
                item.update({"New episodes": group["opportunity_episodes"], "Best quoted estimate": money(group["max_quoted_profit"])})
            item.update({"Healthy hours": round(group["healthy_observation_hours"], 2), "Coverage": group["coverage"],
                         "Version / config": f"{group['version'] or '—'} / {str(group['config_hash'] or 'unobserved')[:8]}"})
            display.append(item)
        st.dataframe(display, hide_index=True, width="stretch")
        st.caption("Amounts rounded for reading; exports retain exact values and full configuration hashes.")
        reasons = [{"Bot": g["instance"], "Reason": k, "Count": v} for g in groups for k, v in g["rejection_reasons"].items()]
        if reasons:
            with st.expander("Common rejection reasons"):
                st.dataframe(reasons, hide_index=True, width="stretch")
    # Limit chart work; the full selected range remains in the report/downloads.
    if 0 < (last - first).days <= 31:
        trend = []
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            for group in daily(con, day, instances=selected, compare=False)["groups"]:
                key = "closed_paper_pnl" if group["strategy"] == "lead_follower" else "max_quoted_profit"
                if group[key] is not None:
                    trend.append({"Date": str(day), "Value": float(group[key]), "Series": f"{group['instance']} / v{group['version']} / {str(group['config_hash'])[:8]}", "Strategy": group["strategy"]})
        for strategy, label in STRATEGY_NAMES.items():
            points = [p for p in trend if p["Strategy"] == strategy]
            if points:
                st.caption(label + (" · daily closed paper P&L" if strategy == "lead_follower" else " · best daily quoted estimate"))
                st.line_chart(points, x="Date", y="Value", color="Series")
    st.caption(NOTE)
    stem = f"polytrader-{first}-{last}"
    for col, suffix, content, mime in zip(st.columns(3), ("html", "csv", "json"),
                                         (html_report(report), csv_report(report), encode(report)),
                                         ("text/html", "text/csv", "application/json")):
        col.download_button("Download " + suffix.upper(), content, stem + "." + suffix, mime)
    st.caption("Downloads include only the selected bots and dates. Select all bots for a fleet report.")
