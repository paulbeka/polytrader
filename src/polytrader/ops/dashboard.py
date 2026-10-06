"""Private research dashboard with optional host-controller integration."""

from contextlib import closing, nullcontext
import os
from pathlib import Path
import sqlite3

import streamlit as st

from polytrader.ops.collector import read_json
from polytrader.ops.controller import request
from polytrader.ops.dashboard_views import overview, bots_view, deployments, reports_view
from polytrader.ops.presentation import age_seconds, fleet
from polytrader.ops.storage import connect, runs

st.set_page_config(page_title="Polytrader · Bot operations", page_icon="◈", layout="wide")
st.markdown("""<style>
.block-container{max-width:1440px;padding-top:2rem}
[data-testid="stMetric"]{border:1px solid #dce3ec;border-radius:12px;padding:18px}
[data-testid="stMetricLabel"]{font-size:.85rem}
[data-testid="stSidebar"]{border-right:1px solid #dce3ec}
h1,h2,h3{letter-spacing:-.025em}
</style>""", unsafe_allow_html=True)
ROOT = Path(os.environ.get("POLYTRADER_DATA", "data")).resolve()
DATABASE = Path(os.environ.get("POLYTRADER_DATABASE", str(ROOT / "ops/index.sqlite3")))
SOCKET = os.environ.get("POLYTRADER_CONTROL_SOCKET")

with st.sidebar:
    st.title("Polytrader")
    st.caption("BOT OPERATIONS")
    page = st.radio("Workspace", ["Overview", "Bots", "Deployments", "Reports"], label_visibility="collapsed", key="navigation")
    st.divider()
    st.caption("Research & paper trading")
    st.caption("Reports: Europe/London\n\nTechnical timestamps: UTC")
    st.button("Refresh")


@st.fragment(run_every="10s")
def dashboard():
    snapshot, socket = {}, None
    if SOCKET:
        try:
            snapshot = request(SOCKET, "/snapshot")
            socket = SOCKET
        except (OSError, ValueError) as exc:
            st.warning("Management is temporarily unavailable. Research remains accessible. " + str(exc))
    collector = read_json(ROOT / "ops/collector_status.json", {})
    age = age_seconds(collector.get("heartbeat_at"))
    if age is None or age > 60:
        st.warning("Collector has no recent heartbeat. Results may be delayed; this does not mean every bot has stopped.")
    else:
        st.caption(f"Updated {age:.0f}s ago · {'Host controller connected' if socket else 'Local research view'}")
    configured = read_json(ROOT / "ops/instances.json", {}) or collector.get("configured_instances", {})
    context = closing(connect(DATABASE, readonly=True)) if DATABASE.exists() else nullcontext(None)
    with context as con:
        history = runs(con) if con else []
        bots = fleet(history, configured, snapshot, collector_fresh=age is not None and age <= 60)
        if page == "Overview":
            overview(bots, con, ROOT)
        elif page == "Bots":
            bots_view(bots, history, con, ROOT, socket, snapshot)
        elif page == "Deployments":
            deployments(bots, socket, snapshot)
        else:
            reports_view(bots, con)
    alerts = read_json(ROOT / "ops/alerts.json", {}).get("alerts", [])
    if alerts:
        with st.expander(f"Operations notices ({len(alerts)})"):
            for alert in alerts:
                st.write(alert)


try:
    dashboard()
except sqlite3.Error as exc:
    st.error("The research index is unavailable. Check the collector and disk permissions. " + str(exc))
