"""
Amazon Seller Tracker - Analytics Dashboard (Streamlit)
========================================================
Run: streamlit run dashboard.py

Shows: BSR trend, price trend, seller counts, daily units sold table
"""

import sqlite3
import os
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from datetime import date, timedelta

DB_PATH = os.getenv("DB_PATH", "amazon_tracker.db")

st.set_page_config(
    page_title="Amazon Tracker",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Dark theme CSS ──
st.markdown("""
<style>
  .stApp { background: #0d1117; color: #e6edf3; }
  .metric-card {
    background: #161b22; border: 1px solid #30363d;
    border-radius: 8px; padding: 16px; margin-bottom: 8px;
  }
  .metric-val { font-size: 2rem; font-weight: 700; color: #58a6ff; }
  .metric-lbl { font-size: 0.8rem; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }
  h1, h2, h3 { color: #f0f6fc !important; }
  [data-testid="stSidebar"] { background: #161b22; }
</style>
""", unsafe_allow_html=True)


# ── DB helpers ──
@st.cache_resource
def get_conn():
    if not os.path.exists(DB_PATH):
        st.error(f"Database not found: {DB_PATH}. Run the pipeline first.")
        st.stop()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql: str, params=()) -> pd.DataFrame:
    conn = get_conn()
    return pd.read_sql_query(sql, conn, params=params)


# ── Sidebar ──
st.sidebar.title("📦 Amazon Tracker")
asins = query("SELECT asin, title FROM dim_product").to_dict("records")
if not asins:
    st.sidebar.warning("No products tracked yet. Run the pipeline.")
    st.stop()

asin_options = {f"{a['asin']} — {(a['title'] or '')[:40]}": a['asin'] for a in asins}
selected_label = st.sidebar.selectbox("Product (ASIN)", list(asin_options.keys()))
selected_asin = asin_options[selected_label]

days = st.sidebar.slider("Days to show", 3, 30, 10)
cutoff = (date.today() - timedelta(days=days)).isoformat()

# Get product_id
pid_row = query("SELECT product_id FROM dim_product WHERE asin=?", (selected_asin,))
if pid_row.empty:
    st.error("Product not found")
    st.stop()
product_id = int(pid_row.iloc[0]["product_id"])

# ── Main header ──
prod_info = query("SELECT * FROM dim_product WHERE product_id=?", (product_id,))
st.title(prod_info.iloc[0]["title"] or selected_asin)
st.caption(f"ASIN: **{selected_asin}** | Last refreshed: {date.today()}")

# ── KPI Cards ──
agg = query("""
    SELECT * FROM agg_product_daily
    WHERE product_id=? ORDER BY agg_date DESC LIMIT 2
""", (product_id,))

if not agg.empty:
    today_row = agg.iloc[0]
    prev_row  = agg.iloc[1] if len(agg) > 1 else None

    col1, col2, col3, col4, col5 = st.columns(5)

    def kpi(col, label, value, delta=None, prefix="", suffix=""):
        col.metric(label, f"{prefix}{value}{suffix}", delta=delta)

    kpi(col1, "BSR Rank", f"#{today_row['bsr_rank']:,}" if today_row['bsr_rank'] else "—",
        delta=f"{today_row['bsr_rank'] - prev_row['bsr_rank']:+,}" if prev_row is not None else None)
    kpi(col2, "Avg Price (Wtd)", f"${today_row['avg_price_weighted']:.2f}" if today_row['avg_price_weighted'] else "—")
    kpi(col3, "Units Sold Today", int(today_row['total_units_sold'] or 0))
    kpi(col4, "FBA Sellers", int(today_row['fba_sellers'] or 0))
    kpi(col5, "FBM Sellers", int(today_row['fbm_sellers'] or 0))

st.divider()

# ── Charts ──
agg_df = query("""
    SELECT * FROM agg_product_daily
    WHERE product_id=? AND agg_date >= ?
    ORDER BY agg_date
""", (product_id, cutoff))

if agg_df.empty:
    st.warning("No aggregate data yet. Run pipeline for at least 2 days.")
else:
    tab1, tab2, tab3 = st.tabs(["📈 BSR & Price", "🏪 Sellers", "📦 Units Sold"])

    # ── Tab 1: BSR & Price ──
    with tab1:
        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            subplot_titles=("Best Sellers Rank (lower = better)", "Weighted Avg Price ($)"),
            vertical_spacing=0.1
        )
        fig.add_trace(go.Scatter(
            x=agg_df["agg_date"], y=agg_df["bsr_rank"],
            mode="lines+markers", name="BSR",
            line=dict(color="#58a6ff", width=2),
            fill="tozeroy", fillcolor="rgba(88,166,255,0.1)"
        ), row=1, col=1)
        fig.update_yaxes(autorange="reversed", row=1, col=1)

        fig.add_trace(go.Scatter(
            x=agg_df["agg_date"], y=agg_df["avg_price_weighted"],
            mode="lines+markers", name="Avg Price",
            line=dict(color="#3fb950", width=2),
        ), row=2, col=1)
        fig.add_trace(go.Scatter(
            x=agg_df["agg_date"], y=agg_df["min_price"],
            mode="lines", name="Min Price",
            line=dict(color="#f85149", width=1, dash="dot"),
        ), row=2, col=1)
        fig.add_trace(go.Scatter(
            x=agg_df["agg_date"], y=agg_df["max_price"],
            mode="lines", name="Max Price",
            line=dict(color="#d29922", width=1, dash="dot"),
        ), row=2, col=1)

        fig.update_layout(
            paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
            font=dict(color="#e6edf3"), height=500,
            legend=dict(bgcolor="#161b22", bordercolor="#30363d")
        )
        fig.update_xaxes(gridcolor="#21262d")
        fig.update_yaxes(gridcolor="#21262d")
        st.plotly_chart(fig, use_container_width=True)

    # ── Tab 2: Sellers ──
    with tab2:
        fig2 = go.Figure()
        fig2.add_trace(go.Bar(
            x=agg_df["agg_date"], y=agg_df["fba_sellers"],
            name="FBA Sellers", marker_color="#58a6ff"
        ))
        fig2.add_trace(go.Bar(
            x=agg_df["agg_date"], y=agg_df["fbm_sellers"],
            name="FBM Sellers", marker_color="#f85149"
        ))
        fig2.update_layout(
            barmode="stack", title="Seller Count by Type",
            paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
            font=dict(color="#e6edf3"), height=400,
            xaxis=dict(gridcolor="#21262d"),
            yaxis=dict(gridcolor="#21262d"),
        )
        st.plotly_chart(fig2, use_container_width=True)

    # ── Tab 3: Units Sold ──
    with tab3:
        sold_df = query("""
            SELECT
                d.calc_date AS Date,
                s.seller_name AS Seller,
                s.fulfillment AS Type,
                d.inv_yesterday AS "Inv Yesterday",
                d.inv_today AS "Inv Today",
                d.units_sold AS "Units Sold",
                d.oos_sold AS "Went OOS",
                d.is_new_seller AS "New Seller"
            FROM fact_daily_units_sold d
            JOIN dim_seller s ON d.seller_id = s.seller_id
            WHERE d.product_id=? AND d.calc_date >= ?
            ORDER BY d.calc_date DESC, d.units_sold DESC
        """, (product_id, cutoff))

        if sold_df.empty:
            st.info("No sales data yet (need 2+ days of snapshots)")
        else:
            # Summary bar
            summary = query("""
                SELECT calc_date AS Date,
                       SUM(CASE WHEN is_new_seller=0 THEN units_sold ELSE 0 END) AS "Total Sold",
                       SUM(CASE WHEN fulfillment='FBA' AND is_new_seller=0 THEN units_sold ELSE 0 END) AS "FBA",
                       SUM(CASE WHEN fulfillment='FBM' AND is_new_seller=0 THEN units_sold ELSE 0 END) AS "FBM"
                FROM fact_daily_units_sold
                WHERE product_id=? AND calc_date >= ?
                GROUP BY calc_date ORDER BY calc_date DESC
            """, (product_id, cutoff))

            fig3 = go.Figure()
            fig3.add_trace(go.Bar(x=summary["Date"], y=summary["FBA"], name="FBA Sold", marker_color="#58a6ff"))
            fig3.add_trace(go.Bar(x=summary["Date"], y=summary["FBM"], name="FBM Sold", marker_color="#3fb950"))
            fig3.update_layout(
                barmode="stack", title="Daily Units Sold (FBA vs FBM)",
                paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                font=dict(color="#e6edf3"), height=300,
                xaxis=dict(gridcolor="#21262d"), yaxis=dict(gridcolor="#21262d"),
            )
            st.plotly_chart(fig3, use_container_width=True)

            # Full detail table
            st.subheader("Detailed Seller-Level Table")
            def highlight_new(row):
                if row["New Seller"] == 1:
                    return ["background-color: #2d1f00"] * len(row)
                if row["Units Sold"] > 0:
                    return ["background-color: #0d2a0d"] * len(row)
                return [""] * len(row)
            st.dataframe(sold_df.style.apply(highlight_new, axis=1), use_container_width=True)

# ── Raw snapshot table ──
st.divider()
with st.expander("📋 Raw Seller Snapshots"):
    raw = query("""
        SELECT f.snapshot_date, f.snapshot_hour,
               s.seller_name, s.fulfillment,
               f.price, f.inventory, f.is_buy_box_winner
        FROM fact_seller_snapshot f
        JOIN dim_seller s ON f.seller_id = s.seller_id
        WHERE f.product_id=? AND f.snapshot_date >= ?
        ORDER BY f.snapshot_date DESC, f.price
    """, (product_id, cutoff))
    st.dataframe(raw, use_container_width=True)
