"""
Amazon Seller Tracker — Analytics Dashboard (Streamlit)
========================================================
Run from project root:
    DB_PATH=pipeline/amazon_tracker.db streamlit run dashboard/dashboard.py

Two data sources, joined by ASIN:
  - fct_keepa_daily  : cumulative daily snapshot from Keepa viewer export
                       (BSR, buy-box price/stock, %OOS, monthly sold, ...)
  - fct_asin_daily   : per-day per-ASIN seller breakdown from the AOD scraper
                       (10-day rolling, units sold, FBA/FBM seller counts)
"""

import os
import sqlite3
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

DB_PATH = os.getenv("DB_PATH", "amazon_tracker.db")

st.set_page_config(
    page_title="Amazon Tracker",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .stApp { background: #0d1117; color: #e6edf3; }
      h1, h2, h3 { color: #f0f6fc !important; }
      [data-testid="stSidebar"] { background: #161b22; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── DB helpers ────────────────────────────────────────────────────────────
@st.cache_resource
def get_conn():
    if not os.path.exists(DB_PATH):
        st.error(f"Database not found: {DB_PATH}. Run the pipeline first.")
        st.stop()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, get_conn(), params=params)


def table_exists(name: str) -> bool:
    r = get_conn().execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return r is not None


HAS_KEEPA = table_exists("fct_keepa_daily")
HAS_AOD = table_exists("fct_asin_daily")


# ── Sidebar ───────────────────────────────────────────────────────────────
st.sidebar.title("📦 Amazon Tracker")

view = st.sidebar.radio(
    "View",
    ["📊 Overview", "🔎 Per-ASIN drilldown"],
    index=0,
)

days = st.sidebar.slider("Days to show", 1, 90, 30)
cutoff = (date.today() - timedelta(days=days)).isoformat()

st.sidebar.caption(f"DB: `{DB_PATH}`")
st.sidebar.caption(
    f"Tables found: "
    f"{'✅ keepa' if HAS_KEEPA else '❌ keepa'} · "
    f"{'✅ aod' if HAS_AOD else '❌ aod'}"
)


# ══════════════════════════════════════════════════════════════════════════
# OVERVIEW
# ══════════════════════════════════════════════════════════════════════════
if view == "📊 Overview":
    st.title("📊 Tracker Overview")

    if HAS_KEEPA:
        latest = query(
            "SELECT MAX(snapshot_date) AS d FROM fct_keepa_daily"
        ).iloc[0]["d"]
        if latest:
            st.caption(f"Latest Keepa snapshot: **{latest}**")

            kpi = query(
                """
                SELECT
                  COUNT(DISTINCT asin) AS asins,
                  AVG(sales_rank_current) AS avg_rank,
                  AVG(buy_box_price) AS avg_price,
                  SUM(CASE WHEN buy_box_seller IS NULL OR buy_box_seller = '-'
                           THEN 1 ELSE 0 END) AS oos_count,
                  AVG(oos_90d_pct) AS avg_oos_90d
                FROM fct_keepa_daily WHERE snapshot_date = ?
                """,
                (latest,),
            ).iloc[0]

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("ASINs tracked", f"{int(kpi['asins']):,}")
            c2.metric("Avg BSR", f"#{int(kpi['avg_rank']):,}" if pd.notna(kpi["avg_rank"]) else "—")
            c3.metric("Avg Buy-Box $", f"${kpi['avg_price']:.2f}" if pd.notna(kpi["avg_price"]) else "—")
            c4.metric("Currently OOS", f"{int(kpi['oos_count'])}")
            c5.metric("Avg 90d OOS %", f"{kpi['avg_oos_90d']:.1f}%" if pd.notna(kpi["avg_oos_90d"]) else "—")

        st.divider()

        # ── Coverage trend ──
        cov = query(
            """
            SELECT snapshot_date,
                   COUNT(*) AS rows,
                   COUNT(DISTINCT asin) AS asins,
                   SUM(CASE WHEN sales_rank_current IS NOT NULL THEN 1 ELSE 0 END) AS with_bsr,
                   SUM(CASE WHEN buy_box_seller IS NOT NULL AND buy_box_seller != '-' THEN 1 ELSE 0 END) AS with_buybox
            FROM fct_keepa_daily
            WHERE snapshot_date >= ?
            GROUP BY snapshot_date ORDER BY snapshot_date
            """,
            (cutoff,),
        )
        if not cov.empty:
            st.subheader("Daily coverage")
            fig = go.Figure()
            fig.add_trace(go.Bar(x=cov["snapshot_date"], y=cov["asins"],
                                 name="ASINs captured", marker_color="#58a6ff"))
            fig.add_trace(go.Scatter(x=cov["snapshot_date"], y=cov["with_buybox"],
                                     name="With buy-box seller", mode="lines+markers",
                                     line=dict(color="#3fb950", width=2)))
            fig.update_layout(
                paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                font=dict(color="#e6edf3"), height=320,
                xaxis=dict(gridcolor="#21262d"), yaxis=dict(gridcolor="#21262d"),
                legend=dict(bgcolor="#161b22", bordercolor="#30363d"),
            )
            st.plotly_chart(fig, use_container_width=True)

        # ── Today's table ──
        st.subheader("Latest snapshot — all ASINs")
        latest_df = query(
            """
            SELECT k.asin, d.title, d.brand,
                   k.sales_rank_current AS bsr,
                   k.sales_rank_30d_avg AS bsr_30d,
                   k.buy_box_price AS price,
                   k.buy_box_stock  AS stock,
                   k.buy_box_seller AS seller,
                   k.oos_90d_pct    AS oos_90d,
                   k.monthly_sold,
                   k.pct_top_seller_30d,
                   k.is_fba_pct AS is_fba
            FROM fct_keepa_daily k
            LEFT JOIN dim_product d ON d.asin = k.asin
            WHERE k.snapshot_date = (SELECT MAX(snapshot_date) FROM fct_keepa_daily)
            ORDER BY k.sales_rank_current
            """
        )
        st.dataframe(latest_df, use_container_width=True, hide_index=True, height=420)

    else:
        st.warning(
            "No `fct_keepa_daily` table found yet. Run the Keepa exporter:\n\n"
            "```bash\ncd pipeline\npython keepa_viewer_export.py --asins-file ../keepa_pipeline/data/asins.txt\n```"
        )


# ══════════════════════════════════════════════════════════════════════════
# PER-ASIN DRILLDOWN
# ══════════════════════════════════════════════════════════════════════════
else:
    if not HAS_KEEPA:
        st.warning("No Keepa data yet — run the exporter first.")
        st.stop()

    asins = query(
        """
        SELECT DISTINCT k.asin, d.title
        FROM fct_keepa_daily k
        LEFT JOIN dim_product d ON d.asin = k.asin
        ORDER BY d.title
        """
    ).to_dict("records")

    if not asins:
        st.warning("No ASINs in Keepa table.")
        st.stop()

    options = {f"{a['asin']} — {(a['title'] or '(no title)')[:60]}": a["asin"] for a in asins}
    label = st.sidebar.selectbox("Pick an ASIN", list(options.keys()))
    asin = options[label]

    # Product header
    prod = query("SELECT * FROM dim_product WHERE asin = ?", (asin,))
    title = prod.iloc[0]["title"] if not prod.empty and prod.iloc[0]["title"] else asin
    st.title(title)
    st.caption(
        f"ASIN: **{asin}**"
        + (f" · Brand: **{prod.iloc[0]['brand']}**" if not prod.empty and prod.iloc[0].get("brand") else "")
        + (f" · Parent: **{prod.iloc[0]['parent_asin']}**" if not prod.empty and prod.iloc[0].get("parent_asin") else "")
    )

    # Keepa trend
    trend = query(
        """
        SELECT snapshot_date, sales_rank_current, sales_rank_30d_avg,
               buy_box_price, buy_box_stock, buy_box_seller,
               oos_90d_pct, monthly_sold, pct_top_seller_30d,
               pct_top_seller_90d, is_fba_pct
        FROM fct_keepa_daily
        WHERE asin = ? AND snapshot_date >= ?
        ORDER BY snapshot_date
        """,
        (asin, cutoff),
    )

    if trend.empty:
        st.info("No Keepa snapshots for this ASIN in the selected window.")
    else:
        # KPI row from latest
        last = trend.iloc[-1]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Current BSR", f"#{int(last['sales_rank_current']):,}" if pd.notna(last["sales_rank_current"]) else "—")
        c2.metric("Buy-Box $", f"${last['buy_box_price']:.2f}" if pd.notna(last["buy_box_price"]) else "—")
        c3.metric("Buy-Box Stock", f"{int(last['buy_box_stock'])}" if pd.notna(last["buy_box_stock"]) else "—")
        c4.metric("Buy-Box Seller", str(last["buy_box_seller"]) if last["buy_box_seller"] and last["buy_box_seller"] != "-" else "—")

        st.divider()

        # BSR + price chart
        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True,
            subplot_titles=("BSR (lower = better)", "Buy-Box price & stock"),
            specs=[[{"secondary_y": False}], [{"secondary_y": True}]],
            vertical_spacing=0.12,
        )
        fig.add_trace(
            go.Scatter(x=trend["snapshot_date"], y=trend["sales_rank_current"],
                       mode="lines+markers", name="BSR (current)",
                       line=dict(color="#58a6ff", width=2)),
            row=1, col=1,
        )
        if trend["sales_rank_30d_avg"].notna().any():
            fig.add_trace(
                go.Scatter(x=trend["snapshot_date"], y=trend["sales_rank_30d_avg"],
                           mode="lines", name="BSR (30d avg)",
                           line=dict(color="#8b949e", width=1, dash="dot")),
                row=1, col=1,
            )
        fig.update_yaxes(autorange="reversed", row=1, col=1)

        fig.add_trace(
            go.Scatter(x=trend["snapshot_date"], y=trend["buy_box_price"],
                       mode="lines+markers", name="Buy-Box $",
                       line=dict(color="#3fb950", width=2)),
            row=2, col=1, secondary_y=False,
        )
        fig.add_trace(
            go.Bar(x=trend["snapshot_date"], y=trend["buy_box_stock"],
                   name="Stock", marker_color="rgba(240,108,73,0.45)"),
            row=2, col=1, secondary_y=True,
        )

        fig.update_layout(
            paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
            font=dict(color="#e6edf3"), height=600,
            legend=dict(bgcolor="#161b22", bordercolor="#30363d"),
        )
        fig.update_xaxes(gridcolor="#21262d")
        fig.update_yaxes(gridcolor="#21262d")
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Raw Keepa snapshots")
        st.dataframe(trend, use_container_width=True, hide_index=True)

    # AOD per-seller breakdown if available
    if HAS_AOD:
        aod = query(
            "SELECT * FROM fct_asin_daily WHERE asin=? AND snapshot_date >= ? "
            "ORDER BY snapshot_date DESC",
            (asin, cutoff),
        )
        if not aod.empty:
            st.divider()
            st.subheader("AOD scraper — seller-level data (10-day rolling)")
            st.dataframe(aod, use_container_width=True, hide_index=True)
