"""
Amazon Tracker — Keepa Daily Snapshot Dashboard
================================================

Visualizes the daily Keepa Product Viewer snapshot for tracked ASINs.

What this dashboard shows:
  - How your products rank on Amazon (BSR) and how that changes day-over-day
  - Current buy-box price + stock for each ASIN
  - Out-of-stock percentages
  - Monthly sales estimates (from Keepa)
  - Trends per product over time

Data source: `fct_keepa_daily` (one row per ASIN per day, cumulative).
Populated by: `pipeline/keepa_viewer_export.py`

Run from project root:
    DB_PATH=pipeline/amazon_tracker.db streamlit run dashboard/dashboard.py
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# Allow importing pipeline/replenishment.py from this script
_pipeline_dir = Path(__file__).resolve().parent.parent / "pipeline"
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))
import replenishment  # noqa: E402


# ── Amazon image URL helpers ─────────────────────────────────────────────
# Keepa gives URLs like "https://m.media-amazon.com/images/I/31kheBur85L.jpg"
# which is the small thumbnail. To get a larger version, insert a size hint:
#   .../I/<id>._SL500_.jpg   →   500px max dimension
# We strip any existing size hint first so the transformation is idempotent.
_SIZE_HINT_RE = re.compile(r"\._[A-Z]{2}\d+_+")


def resize_amazon_image(url: str | None, size: int = 500) -> str | None:
    if not url or "media-amazon.com/images/I/" not in url:
        return url
    clean = _SIZE_HINT_RE.sub("", url)
    return clean.replace(".jpg", f"._SL{size}_.jpg")

DB_PATH = os.getenv("DB_PATH", "amazon_tracker.db")

st.set_page_config(
    page_title="Amazon Tracker — Keepa Daily Snapshot",
    page_icon="📦",
    layout="wide",
)


# ── Modern CSS theme (works in both light and dark) ──────────────────────
st.markdown(
    """
    <style>
      /* Tighter top padding so the header doesn't waste space */
      .block-container { padding-top: 2rem !important; padding-bottom: 4rem !important; max-width: 1500px; }

      /* CARD: every bordered container becomes a polished card */
      div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 12px !important;
        border: 1px solid rgba(127, 127, 127, 0.16) !important;
        padding: 0.85rem 1rem !important;
        background: rgba(255, 255, 255, 0.02);
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04), 0 3px 10px rgba(0, 0, 0, 0.05);
        transition: transform 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
        height: 100%;
      }
      div[data-testid="stVerticalBlockBorderWrapper"]:hover {
        border-color: rgba(99, 102, 241, 0.55) !important;
        box-shadow: 0 2px 4px rgba(0,0,0,0.06), 0 8px 24px rgba(99,102,241,0.10);
      }
      /* Hero card: full-bleed style for the product info card */
      .hero-wrapper div[data-testid="stVerticalBlockBorderWrapper"] {
        padding: 1.5rem 1.75rem !important;
      }

      /* METRICS: bigger, more presence */
      [data-testid="stMetricLabel"] {
        font-size: 0.78rem !important;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        opacity: 0.72;
      }
      [data-testid="stMetricValue"] {
        font-size: 1.85rem !important;
        font-weight: 700 !important;
        line-height: 1.1 !important;
        margin-top: 0.25rem;
      }
      [data-testid="stMetricDelta"] {
        font-size: 0.85rem !important;
        font-weight: 600;
      }

      /* SECTION HEADER: small-caps label + accent bar */
      .section-header {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        margin: 1.5rem 0 0.6rem 0;
      }
      .section-header .dot {
        width: 8px; height: 8px; border-radius: 999px;
        background: linear-gradient(135deg, #6366f1, #8b5cf6);
        box-shadow: 0 0 12px rgba(99, 102, 241, 0.5);
        flex-shrink: 0;
      }
      .section-header .label {
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        opacity: 0.62;
        white-space: nowrap;
      }
      .section-header .rule {
        flex: 1;
        height: 1px;
        background: linear-gradient(90deg, rgba(127,127,127,0.35) 0%, transparent 100%);
      }

      /* Chart titles inside cards */
      .chart-title {
        font-size: 0.95rem;
        font-weight: 700;
        margin-bottom: 0.1rem;
      }
      .chart-caption {
        font-size: 0.78rem;
        opacity: 0.65;
        margin-bottom: 0.5rem;
        line-height: 1.4;
      }

      /* Page title styling */
      h1 { font-weight: 800 !important; letter-spacing: -0.02em; }

      /* Sidebar polish */
      [data-testid="stSidebar"] { background: rgba(127, 127, 127, 0.04); }

      /* Hide the "Made with Streamlit" footer */
      footer { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)


def section_header(label: str) -> None:
    """Renders a small-caps section divider with an accent dot and rule."""
    st.markdown(
        f'<div class="section-header"><span class="dot"></span>'
        f'<span class="label">{label}</span><span class="rule"></span></div>',
        unsafe_allow_html=True,
    )


# ── DB helpers ────────────────────────────────────────────────────────────
@st.cache_resource
def get_conn():
    if not os.path.exists(DB_PATH):
        st.error(
            f"Database not found at `{DB_PATH}`. "
            "Run the Keepa exporter first:\n\n"
            "`cd pipeline && python keepa_viewer_export.py --asins-file ../data/asins.txt`"
        )
        st.stop()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_data(ttl=300)
def query(sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, get_conn(), params=params)


@st.cache_data(ttl=300)
def load_daily_sales(asin: str, days: int):
    """Canonical daily units-sold series for an ASIN.

    Single source of truth: prefer TRUE per-seller deltas
    (`v_asin_daily_sales`); fall back to FBA-aggregate deltas
    (`v_daily_sales`) only when the ASIN has no per-seller data yet.
    Returns (DataFrame[Date, units_sold, units_restocked, sellers], source).
    """
    ps = query(
        """SELECT sale_date AS Date, units_sold, units_restocked,
                  selling_seller_count AS sellers
           FROM v_asin_daily_sales
           WHERE asin = ? AND sale_date >= date('now', ?)
           ORDER BY sale_date""",
        (asin, f"-{days} days"),
    )
    if not ps.empty:
        return ps, "per-seller"
    fb = query(
        """SELECT snapshot_date AS Date, units_sold,
                  0 AS units_restocked, NULL AS sellers
           FROM v_daily_sales
           WHERE asin = ? AND snapshot_date >= date('now', ?)
             AND units_sold IS NOT NULL
           ORDER BY snapshot_date""",
        (asin, f"-{days} days"),
    )
    return fb, "fba-delta"


def table_exists(name: str) -> bool:
    return get_conn().execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


if not table_exists("fct_keepa_daily"):
    st.error(
        "The `fct_keepa_daily` table doesn't exist yet. "
        "Run the Keepa exporter first to download and import data."
    )
    st.stop()


# ── Header ────────────────────────────────────────────────────────────────
st.title("📦 Amazon Product Tracker")
st.markdown(
    "Daily snapshot of your tracked Amazon ASINs from **Keepa**. "
    "Each row is one product on one day — BSR, buy-box price, stock, "
    "monthly sales estimate, and out-of-stock history."
)

# What's in the DB?
summary = query(
    """
    SELECT
      MIN(snapshot_date) AS first_date,
      MAX(snapshot_date) AS last_date,
      COUNT(DISTINCT snapshot_date) AS days,
      COUNT(DISTINCT asin) AS asins,
      COUNT(*) AS rows
    FROM fct_keepa_daily
    """
).iloc[0]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Products tracked", f"{int(summary['asins']):,}")
c2.metric("Days of history", f"{int(summary['days']):,}")
c3.metric("Total snapshots", f"{int(summary['rows']):,}")
c4.metric("Latest snapshot", str(summary["last_date"]) if summary["last_date"] else "—")

st.divider()

# ── Sidebar controls ──────────────────────────────────────────────────────
st.sidebar.header("Filters")
view = st.sidebar.radio(
    "Choose a view",
    ["🔎 Single product trend", "🎯 Replenishment"],
    index=0,
    help="• Single product = pick a brand + ASIN, see every trend + per-seller stock.\n"
         "• Replenishment = SHIP/HOLD/AVOID recommendation per ASIN.",
)

days = st.sidebar.slider(
    "Days of history to load",
    min_value=7, max_value=90, value=90,
    help="Affects trend charts. Storage is capped at 90 days for per-seller data.",
)
cutoff = (date.today() - timedelta(days=days)).isoformat()
st.sidebar.caption(f"Database: `{DB_PATH}`")


@st.cache_data(ttl=300)
def _health_md() -> str:
    import health_check
    return health_check.format_markdown(health_check.run_checks(get_conn()))


with st.sidebar.expander("🩺 Pipeline health", expanded=False):
    st.markdown(_health_md())
    if table_exists("pipeline_runs"):
        last_run = query(
            """SELECT finished_at, status, csv_rows_today
               FROM pipeline_runs WHERE status != 'failed'
               ORDER BY run_id DESC LIMIT 1"""
        )
        if not last_run.empty:
            r = last_run.iloc[0]
            st.caption(f"Last run: {r['finished_at']} · {r['status']} · "
                       f"{int(r['csv_rows_today'] or 0)} CSV rows today")
        else:
            st.caption("No successful pipeline run logged yet.")


# ══════════════════════════════════════════════════════════════════════════
# VIEW 1: SINGLE-PRODUCT TREND  (default — main analytical page)
# ══════════════════════════════════════════════════════════════════════════
if view == "🔎 Single product trend":
    st.markdown(
        "<div style='display:flex;align-items:baseline;justify-content:space-between;"
        "margin:0.2rem 0 0.6rem'><h2 style='margin:0;font-weight:800;letter-spacing:-0.01em'>"
        "Single product trend</h2><span style='opacity:0.5;font-size:0.8rem'>"
        "Filter by brand → ASIN; see BSR, stock, prices, sellers, daily sales</span></div>",
        unsafe_allow_html=True,
    )

    # ── Brand filter (NEW) — cascades into ASIN dropdown ──
    brands = query(
        """SELECT DISTINCT COALESCE(d.brand, '(no brand)') AS brand,
                  COUNT(DISTINCT d.asin) AS asin_count
           FROM dim_product d
           JOIN fct_keepa_daily k ON k.asin = d.asin
           GROUP BY brand
           ORDER BY brand"""
    )
    brand_options = ["🌐 All brands"] + [
        f"{row['brand']}  ({row['asin_count']})" for _, row in brands.iterrows()
    ]
    picked_brand = st.sidebar.selectbox(
        "Brand", brand_options,
        help="Filter the ASIN dropdown by brand. 'All brands' = unfiltered.",
    )

    # ── ASIN selector — filtered by brand ──
    # Also LEFT JOIN seller_count so we can show 🏪 marker + sort ASINs WITH
    # per-seller data FIRST in the dropdown.
    base_sql = """
        SELECT k.asin,
               COALESCE(d.title, k.asin) AS title,
               COALESCE(sc.sellers, 0)   AS sellers
        FROM (SELECT DISTINCT asin FROM fct_keepa_daily) k
        LEFT JOIN dim_product d ON d.asin = k.asin
        LEFT JOIN (
            SELECT asin, COUNT(DISTINCT seller_id) AS sellers
            FROM fct_keepa_seller_history
            WHERE stock IS NOT NULL
            GROUP BY asin
        ) sc ON sc.asin = k.asin
        {where}
        ORDER BY sellers DESC, title
    """
    if picked_brand.startswith("🌐"):
        options_df = query(base_sql.format(where=""))
    else:
        brand_name = picked_brand.rsplit("  (", 1)[0]
        options_df = query(
            base_sql.format(where="WHERE COALESCE(d.brand, '(no brand)') = ?"),
            (brand_name,),
        )

    if options_df.empty:
        st.sidebar.warning("No ASINs match this brand.")
        st.stop()

    # Label format:  🏪137  B00RNFPU3W — title…   (when per-seller data exists)
    #                 ⏳    B0XXXXXXXX — title…   (when not yet fetched)
    def _label_for(row):
        if row["sellers"] > 0:
            tag = f"🏪 {int(row['sellers']):>3}"
        else:
            tag = "⏳    "
        return f"{tag}  {row['asin']} — {(row['title'] or '')[:55]}"

    options = {_label_for(r): r["asin"] for _, r in options_df.iterrows()}
    n_with_seller = (options_df["sellers"] > 0).sum()
    label = st.sidebar.selectbox(
        f"ASIN ({len(options)} total · 🏪 {n_with_seller} with seller data)",
        list(options.keys()),
        help="🏪 N = N sellers tracked.  ⏳ = not yet API-fetched.  "
             "Sellers-tracked ASINs appear first.",
    )
    asin = options[label]

    prod = query("SELECT * FROM dim_product WHERE asin = ?", (asin,))
    p = prod.iloc[0] if not prod.empty else None

    trend = query(
        """SELECT snapshot_date AS Date,
                  sales_rank_current   AS bsr,
                  sales_rank_30d_avg   AS bsr_30d,
                  buy_box_price        AS bb_price,
                  buy_box_stock        AS bb_stock,
                  buy_box_seller       AS bb_seller,
                  fba_price            AS fba_price,
                  fbm_price            AS fbm_price,
                  fba_offers           AS fba_offers,
                  fbm_offers           AS fbm_offers,
                  total_offers         AS total_offers,
                  fba_stock            AS fba_stock,
                  oos_90d_pct          AS oos_90d,
                  pct_top_seller_30d   AS top_30d,
                  pct_top_seller_90d   AS top_90d,
                  monthly_sold_num     AS monthly_sold,
                  is_fba_pct           AS is_fba_pct
           FROM fct_keepa_daily
           WHERE asin = ? AND snapshot_date >= ?
           ORDER BY snapshot_date""",
        (asin, cutoff),
    )

    if trend.empty:
        st.info(f"No snapshots for this ASIN in the last {days} days.")
        st.stop()

    last = trend.iloc[-1]
    prev = trend.iloc[-2] if len(trend) >= 2 else None

    # ── helper: delta string vs previous day ──────────────────────────
    def delta_of(col, *, inverse=False, fmt="{:+,.0f}"):
        """`inverse=True` flips sign coloring (used for BSR where lower = better)."""
        if prev is None or pd.isna(last[col]) or pd.isna(prev[col]):
            return None
        diff = last[col] - prev[col]
        if diff == 0:
            return None
        # When inverse, plotly/streamlit's red/green is based on sign — we
        # negate so a falling BSR appears positive (green).
        return fmt.format(-diff if inverse else diff)

    def fmt_n(v, prefix="", suffix="", default="—"):
        return f"{prefix}{int(v):,}{suffix}" if pd.notna(v) else default

    def fmt_f(v, prefix="$", default="—"):
        return f"{prefix}{v:.2f}" if pd.notna(v) else default

    # ══ HERO CARD ═════════════════════════════════════════════════════
    # Image + metadata + 4 inline KPIs in a single dense card — no dead space.
    if p is not None:
        img_url = resize_amazon_image(p.get("image_url"), size=400)

        # Build KPI tile HTML (used inside the hero)
        def hero_kpi(label, value, delta=None, delta_good=None):
            """delta_good: True/False/None — controls color. None = neutral grey."""
            color = "#94a3b8"
            if delta and delta_good is True:
                color = "#10b981"
            elif delta and delta_good is False:
                color = "#f43f5e"
            delta_html = (
                f"<div style='font-size:0.72rem;font-weight:600;color:{color};"
                f"margin-top:0.15rem'>{delta}</div>"
                if delta else ""
            )
            return (
                f"<div style='flex:1;min-width:95px;padding:0.35rem 0;'>"
                f"<div style='font-size:0.62rem;font-weight:700;letter-spacing:0.08em;"
                f"text-transform:uppercase;opacity:0.55;margin-bottom:0.2rem;white-space:nowrap'>{label}</div>"
                f"<div style='font-size:1.35rem;font-weight:700;line-height:1.1;white-space:nowrap'>{value}</div>"
                f"{delta_html}"
                f"</div>"
            )

        # Compute KPI deltas
        def _diff(col, inverse=False):
            if prev is None or pd.isna(last[col]) or pd.isna(prev[col]):
                return None, None
            d = last[col] - prev[col]
            if d == 0:
                return None, None
            good = (d < 0) if inverse else (d > 0)
            return d, good

        bsr_d, bsr_good = _diff("bsr", inverse=True)
        price_d, price_good = _diff("bb_price")
        offers_d, offers_good = _diff("total_offers")
        fba_stock_d, fba_stock_good = _diff("fba_stock")
        fba_off_d, fba_off_good = _diff("fba_offers")

        kpi_strip_html = "".join([
            hero_kpi(
                "Current BSR",
                fmt_n(last["bsr"], prefix="#"),
                delta=f"{'▲' if bsr_d and bsr_d>0 else '▼'} {abs(int(bsr_d)):,}" if bsr_d else None,
                delta_good=bsr_good,
            ),
            hero_kpi(
                "Buy-Box price",
                fmt_f(last["bb_price"]),
                delta=f"{'▲' if price_d and price_d>0 else '▼'} ${abs(price_d):.2f}" if price_d else None,
                delta_good=price_good,
            ),
            hero_kpi(
                "Total offers",
                fmt_n(last["total_offers"], default="0"),
                delta=f"{'▲' if offers_d and offers_d>0 else '▼'} {abs(int(offers_d))}" if offers_d else None,
                delta_good=offers_good,
            ),
            hero_kpi(
                "FBA stock",
                fmt_n(last["fba_stock"], default="0"),
                delta=f"{'▲' if fba_stock_d and fba_stock_d>0 else '▼'} {abs(int(fba_stock_d))}" if fba_stock_d else None,
                delta_good=fba_stock_good,
            ),
            hero_kpi(
                "FBA sellers",
                fmt_n(last["fba_offers"], default="0"),
                delta=f"{'▲' if fba_off_d and fba_off_d>0 else '▼'} {abs(int(fba_off_d))}" if fba_off_d else None,
                delta_good=fba_off_good,
            ),
        ])

        # Tag chips for metadata
        chips = [f"<code style='background:rgba(127,127,127,0.12);padding:0.15rem 0.45rem;border-radius:5px;font-size:0.75rem'>{asin}</code>"]
        if p.get("parent_asin"):
            chips.append(f"<span style='font-size:0.78rem;opacity:0.75'>Parent <code style='background:rgba(127,127,127,0.10);padding:0.1rem 0.35rem;border-radius:4px;font-size:0.72rem'>{p['parent_asin']}</code></span>")
        if p.get("variation_color"):
            chips.append(f"<span style='font-size:0.78rem;opacity:0.75'>🎨 {p['variation_color']}</span>")
        if p.get("variation_size"):
            chips.append(f"<span style='font-size:0.78rem;opacity:0.75'>📏 {p['variation_size']}</span>")

        img_html = (
            f"<img src='{img_url}' style='width:100%;max-height:180px;object-fit:contain;"
            f"border-radius:10px;background:rgba(127,127,127,0.04)'/>"
            if img_url else
            "<div style='width:100%;height:180px;display:flex;align-items:center;"
            "justify-content:center;border:1px dashed rgba(127,127,127,0.30);"
            "border-radius:10px;opacity:0.45;font-size:0.85rem'>no image</div>"
        )

        st.markdown(
            f"""
            <div style='border:1px solid rgba(127,127,127,0.18);border-radius:14px;
                        padding:1.1rem 1.25rem;background:rgba(255,255,255,0.02);
                        box-shadow:0 1px 2px rgba(0,0,0,0.04), 0 4px 12px rgba(0,0,0,0.06);
                        margin-bottom:0.5rem;'>
              <div style='display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap'>
                <div style='flex:0 0 180px;max-width:180px'>{img_html}</div>
                <div style='flex:1;min-width:260px'>
                  <div style='font-size:0.7rem;font-weight:700;letter-spacing:0.14em;
                              text-transform:uppercase;opacity:0.55;margin-bottom:0.3rem'>
                    {(p.get('brand') or 'Unbranded').upper()}
                  </div>
                  <h3 style='margin:0 0 0.55rem 0;line-height:1.25;font-weight:700;
                             font-size:1.25rem'>
                    {p['title'] or asin}
                  </h3>
                  <div style='display:flex;gap:0.6rem;flex-wrap:wrap;margin-bottom:0.75rem'>
                    {''.join(chips)}
                  </div>
                  <div style='display:flex;gap:0.5rem'>
                    <a href='https://www.amazon.com/dp/{asin}?th=1&psc=1' target='_blank'
                       style='padding:0.35rem 0.75rem;border-radius:7px;
                              background:rgba(99,102,241,0.12);
                              border:1px solid rgba(99,102,241,0.35);
                              color:inherit;text-decoration:none;
                              font-size:0.78rem;font-weight:600'>Open on Amazon ↗</a>
                    <a href='https://keepa.com/#!product/1-{asin}' target='_blank'
                       style='padding:0.35rem 0.75rem;border-radius:7px;
                              background:rgba(127,127,127,0.10);
                              border:1px solid rgba(127,127,127,0.25);
                              color:inherit;text-decoration:none;
                              font-size:0.78rem;font-weight:600'>Open on Keepa ↗</a>
                  </div>
                </div>
                <div style='flex:1.4;min-width:520px;display:flex;gap:1rem;flex-wrap:wrap;
                            padding-left:1.1rem;border-left:1px solid rgba(127,127,127,0.18)'>
                  {kpi_strip_html}
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Data-availability hint for fully-OOS ASINs
    if trend[["bb_price", "fba_offers", "fbm_offers", "fba_stock"]].isna().all().all():
        st.warning(
            "This ASIN currently has **zero active offers** on Amazon, so most charts "
            "below will be empty. Only the BSR chart will show data. Pick a different "
            "ASIN from the sidebar to see fully-populated charts."
        )

    # ── chart styling helpers ─────────────────────────────────────────
    CHART_FONT = dict(family="Inter, -apple-system, system-ui, sans-serif", size=12)

    def _styled_layout(height=300, show_legend=True):
        return dict(
            height=height,
            margin=dict(l=8, r=8, t=8, b=8),
            font=CHART_FONT,
            legend=dict(orientation="h", yanchor="bottom", y=-0.28,
                        xanchor="center", x=0.5, bgcolor="rgba(0,0,0,0)",
                        font=dict(size=11)) if show_legend else dict(visible=False),
            hovermode="x unified",
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
        )

    def _styled_axes(fig, y_title="", invert_y=False, y_range=None):
        fig.update_yaxes(title_text=y_title, gridcolor="rgba(127,127,127,0.14)",
                         zerolinecolor="rgba(127,127,127,0.2)",
                         autorange="reversed" if invert_y else True,
                         range=y_range)
        fig.update_xaxes(title_text=None, gridcolor="rgba(127,127,127,0.08)",
                         zerolinecolor="rgba(127,127,127,0.2)")

    def chart_card(col, *, title, caption, traces, y_title="", invert_y=False,
                   height=255, fill=False, single_color=None):
        with col.container(border=True):
            st.markdown(f'<div class="chart-title">{title}</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="chart-caption">{caption}</div>', unsafe_allow_html=True)
            fig = go.Figure()
            for tr in traces:
                if tr["col"] not in trend.columns or trend[tr["col"]].notna().sum() == 0:
                    continue
                kwargs = dict(
                    x=trend["Date"], y=trend[tr["col"]],
                    mode="lines+markers", name=tr["name"],
                    line=dict(color=tr.get("color"), width=tr.get("width", 2.5),
                              dash=tr.get("dash"), shape="spline", smoothing=0.6),
                    marker=dict(size=6, line=dict(width=0)),
                    connectgaps=False,
                )
                if fill and len(traces) == 1:
                    kwargs["fill"] = "tozeroy"
                    kwargs["fillcolor"] = single_color or "rgba(99,102,241,0.12)"
                fig.add_trace(go.Scatter(**kwargs))
            if not fig.data:
                st.markdown(
                    "<div style='display:flex;align-items:center;justify-content:center;"
                    f"height:{height-30}px;opacity:0.45;font-size:0.9rem'>"
                    "no data in window</div>", unsafe_allow_html=True,
                )
                return
            fig.update_layout(**_styled_layout(height=height, show_legend=len(fig.data) > 1))
            _styled_axes(fig, y_title=y_title, invert_y=invert_y)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    # ══ SECTION: RANKING ══════════════════════════════════════════════
    section_header("Ranking visibility")
    rank_col, _ = st.columns([1, 0.001])  # full-width single card
    chart_card(
        rank_col,
        title="📈 Best Sellers Rank over time",
        caption="Lower BSR = the product sells more often. Y-axis is inverted so a line going <b>up</b> means rank improved.",
        traces=[
            {"col": "bsr",     "name": "BSR (current)",  "color": "#6366f1"},
            {"col": "bsr_30d", "name": "BSR 30-day avg", "color": "#94a3b8", "dash": "dot", "width": 1.5},
        ],
        y_title="Rank", invert_y=True, height=290,
    )

    # ══ SECTION: COMPETITION ══════════════════════════════════════════
    section_header("Competition & seller mix")
    a1, a2, a3 = st.columns(3, gap="medium")
    chart_card(
        a1,
        title="👥 Seller offer count",
        caption="Sellers listing this ASIN, split by fulfillment type.",
        traces=[
            {"col": "fba_offers",   "name": "FBA",   "color": "#10b981"},
            {"col": "fbm_offers",   "name": "FBM",   "color": "#f43f5e"},
            {"col": "total_offers", "name": "Total", "color": "#94a3b8", "dash": "dot", "width": 1.5},
        ],
        y_title="Sellers",
    )
    chart_card(
        a2,
        title="👑 Top-seller dominance",
        caption="What share of the buy-box the dominant seller has held. Rising = one seller is consolidating control.",
        traces=[
            {"col": "top_30d", "name": "30-day", "color": "#06b6d4"},
            {"col": "top_90d", "name": "90-day", "color": "#3b82f6", "dash": "dot"},
        ],
        y_title="% of buy-box",
    )
    # FBA market share (derived)
    if trend[["fba_offers", "fbm_offers"]].notna().any().any():
        share = trend.copy()
        denom = (share["fba_offers"].fillna(0) + share["fbm_offers"].fillna(0))
        share["fba_share"] = (share["fba_offers"].fillna(0) / denom.where(denom > 0)) * 100
        with a3.container(border=True):
            st.markdown('<div class="chart-title">⚙️ FBA share of offers</div>', unsafe_allow_html=True)
            st.markdown('<div class="chart-caption">FBA offers ÷ (FBA + FBM). Tracks how Amazon-fulfilled the listing is.</div>', unsafe_allow_html=True)
            fig = go.Figure(go.Scatter(
                x=share["Date"], y=share["fba_share"],
                mode="lines+markers", line=dict(color="#10b981", width=2.5, shape="spline", smoothing=0.6),
                marker=dict(size=6), fill="tozeroy", fillcolor="rgba(16,185,129,0.13)",
            ))
            fig.update_layout(**_styled_layout(show_legend=False))
            _styled_axes(fig, y_title="FBA share (%)", y_range=[0, 100])
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    else:
        with a3.container(border=True):
            st.markdown('<div class="chart-title">⚙️ FBA share of offers</div>', unsafe_allow_html=True)
            st.markdown('<div class="chart-caption">No offer data for this ASIN.</div>', unsafe_allow_html=True)

    # ══ SECTION: INVENTORY ════════════════════════════════════════════
    section_header("Inventory & availability")
    b1, b2 = st.columns(2, gap="medium")
    chart_card(
        b1,
        title="📦 Stock on hand",
        caption="<b>FBA aggregate</b> = combined inventory across all FBA sellers. <b>Buy-box</b> = current buy-box winner's stock. Keepa doesn't expose FBM stock.",
        traces=[
            {"col": "fba_stock", "name": "FBA aggregate", "color": "#10b981"},
            {"col": "bb_stock",  "name": "Buy-Box seller", "color": "#f97316"},
        ],
        y_title="Units",
    )
    chart_card(
        b2,
        title="🚫 90-day out-of-stock %",
        caption="How often the product was OOS in the trailing 90 days. Higher = supply chain problems.",
        traces=[{"col": "oos_90d", "name": "OOS %", "color": "#ec4899"}],
        y_title="OOS share (%)",
        fill=True, single_color="rgba(236,72,153,0.13)",
    )

    # ══ SECTION: PRICING & SALES ══════════════════════════════════════
    section_header("Pricing & sales velocity")
    d1, d2, d3 = st.columns(3, gap="medium")
    chart_card(
        d1,
        title="💵 Prices by channel",
        caption="<b>Buy-Box</b> = what Amazon shows the customer. <b>FBA/FBM</b> = lowest 3rd-party price for each channel.",
        traces=[
            {"col": "bb_price",  "name": "Buy-Box", "color": "#6366f1"},
            {"col": "fba_price", "name": "FBA",     "color": "#10b981"},
            {"col": "fbm_price", "name": "FBM",     "color": "#f43f5e"},
        ],
        y_title="Price ($)",
    )
    chart_card(
        d2,
        title="📊 Monthly sales — Keepa estimate",
        caption="Keepa's <i>own estimate</i> of units sold in 30 days (only when Amazon publishes it). For the measured figure, see the <b>Daily units sold</b> section below.",
        traces=[{"col": "monthly_sold", "name": "Units / month", "color": "#a855f7"}],
        y_title="Units / month",
        fill=True, single_color="rgba(168,85,247,0.13)",
    )
    # Daily velocity (Keepa estimate ÷ 30 — distinct from the measured section below)
    if trend["monthly_sold"].notna().any():
        velocity = trend.copy()
        velocity["daily_velocity"] = velocity["monthly_sold"] / 30.0
        with d3.container(border=True):
            st.markdown('<div class="chart-title">⚡ Daily velocity — Keepa estimate</div>', unsafe_allow_html=True)
            st.markdown('<div class="chart-caption">Keepa Monthly Sold ÷ 30. An estimate — compare against the measured <b>Daily units sold</b> section below.</div>', unsafe_allow_html=True)
            fig = go.Figure(go.Scatter(
                x=velocity["Date"], y=velocity["daily_velocity"],
                mode="lines+markers", line=dict(color="#eab308", width=2.5, shape="spline", smoothing=0.6),
                marker=dict(size=6), fill="tozeroy", fillcolor="rgba(234,179,8,0.13)",
            ))
            fig.update_layout(**_styled_layout(show_legend=False))
            _styled_axes(fig, y_title="Units / day")
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    else:
        with d3.container(border=True):
            st.markdown('<div class="chart-title">⚡ Daily sales velocity</div>', unsafe_allow_html=True)
            st.markdown('<div class="chart-caption">Requires Keepa\'s Monthly Sold field — Amazon isn\'t showing it for this ASIN.</div>', unsafe_allow_html=True)

    # ══ DAILY UNITS SOLD (canonical, always shown) ════════════════════
    # Single source of truth: prefer true per-seller deltas
    # (v_asin_daily_sales), fall back to FBA-aggregate deltas (v_daily_sales).
    has_seller_data = table_exists("fct_keepa_seller_history") and query(
        "SELECT COUNT(*) AS n FROM fct_keepa_seller_history WHERE asin = ?",
        (asin,),
    ).iloc[0]["n"] > 0

    section_header("Daily units sold")
    sales_df, sales_source = load_daily_sales(asin, days)
    is_ps = sales_source == "per-seller"
    if is_ps:
        st.caption(
            "**Source: per-seller** — computed from each seller's stock drops "
            "(restocks excluded). This is the accurate figure, not an estimate."
        )
    else:
        st.caption(
            "**Source: FBA-delta** — derived from day-over-day drops in the FBA "
            "aggregate stock. No per-seller data for this ASIN yet, so this is a "
            "lower-bound estimate (restocks can mask sales)."
        )

    if sales_df.empty:
        st.info(f"No sales signal in the last {days} days for this ASIN.")
    else:
        sales_df["Date"] = pd.to_datetime(sales_df["Date"])
        total_sold = int(sales_df["units_sold"].fillna(0).sum())
        active_days = int((sales_df["units_sold"].fillna(0) > 0).sum())
        avg_per_active = total_sold / active_days if active_days else 0
        # KPI strip — restock KPI only meaningful for the per-seller source
        kcols = st.columns(4 if is_ps else 3, gap="medium")
        with kcols[0].container(border=True):
            st.metric(f"⚡ Total sold ({days}d)", f"{total_sold:,}",
                      help="Units sold across this ASIN in the window.")
        idx = 1
        if is_ps:
            total_restocked = int(sales_df["units_restocked"].fillna(0).sum())
            with kcols[idx].container(border=True):
                st.metric(f"📦 Total restocked ({days}d)", f"{total_restocked:,}",
                          help="Sum of every seller's stock increases (restocks).")
            idx += 1
        with kcols[idx].container(border=True):
            st.metric("📅 Days with sales", f"{active_days}",
                      help="How many days had at least 1 unit sold.")
        idx += 1
        with kcols[idx].container(border=True):
            st.metric("📊 Avg sold per active day", f"{avg_per_active:.1f}",
                      help="Total sold ÷ days that had any sales.")

        with st.container(border=True):
            title = ("📈 Daily units sold (bars) vs restocked (line)" if is_ps
                     else "📈 Daily units sold (FBA-delta estimate)")
            st.markdown(f'<div class="chart-title">{title}</div>', unsafe_allow_html=True)
            cap = ("Green bars = units sold per day. Orange line = restocks "
                   "(Keepa observations of seller stock going UP)." if is_ps else
                   "Green bars = estimated units sold per day from FBA stock drops.")
            st.markdown(f'<div class="chart-caption">{cap}</div>', unsafe_allow_html=True)
            fig = go.Figure()
            fig.add_trace(go.Bar(
                x=sales_df["Date"], y=sales_df["units_sold"],
                name="Sold", marker_color="#10b981",
            ))
            if is_ps and sales_df["units_restocked"].fillna(0).sum() > 0:
                fig.add_trace(go.Scatter(
                    x=sales_df["Date"], y=sales_df["units_restocked"],
                    name="Restocked", mode="lines+markers",
                    line=dict(color="#f97316", width=2, dash="dot"),
                    marker=dict(size=5),
                ))
            fig.update_layout(
                height=320, plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                margin=dict(l=8, r=8, t=8, b=8), barmode="group",
                legend=dict(orientation="h", yanchor="bottom", y=-0.25,
                            xanchor="center", x=0.5, bgcolor="rgba(0,0,0,0)"),
                hovermode="x unified",
            )
            fig.update_xaxes(title=None, gridcolor="rgba(127,127,127,0.08)")
            fig.update_yaxes(title="Units", gridcolor="rgba(127,127,127,0.14)")
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    if has_seller_data:
        section_header("Per-seller inventory")

        # Optional seller filter (defaults to all)
        sellers_for_asin = query(
            """SELECT h.seller_id,
                      COALESCE(s.seller_name, '(name not fetched)') AS name,
                      MAX(h.is_fba)   AS is_fba,
                      MAX(s.is_amazon) AS is_amazon,
                      COUNT(*) AS events
               FROM fct_keepa_seller_history h
               LEFT JOIN dim_keepa_seller s ON s.seller_id = h.seller_id
               WHERE h.asin = ? AND h.stock IS NOT NULL
               GROUP BY h.seller_id
               ORDER BY events DESC""",
            (asin,),
        )

        seller_options = {"📊 All sellers (one line each)": None}
        for _, sr in sellers_for_asin.iterrows():
            flag = "🛒" if sr["is_amazon"] else ("🚚" if sr["is_fba"] else "🏠")
            lbl = f"{flag}  {sr['name'][:30]}  ({sr['seller_id']})"
            seller_options[lbl] = sr["seller_id"]

        picked_seller_label = st.selectbox(
            f"Seller filter — {len(sellers_for_asin)} sellers for this ASIN",
            options=list(seller_options.keys()),
            key=f"single_prod_seller_{asin}",
            help="Default = each seller as its own colored line. "
                 "Pick a seller to drill into just their stock + price.",
        )
        sel_seller = seller_options[picked_seller_label]

        if sel_seller is None:
            # ── Multi-line chart: one line per seller ──
            with st.container(border=True):
                st.markdown(
                    '<div class="chart-title">📦 Stock over time — every seller</div>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    '<div class="chart-caption">Each color = one seller. Hover any line to see who owns the stock.</div>',
                    unsafe_allow_html=True,
                )
                history = query(
                    """SELECT h.change_time AS Date,
                              h.seller_id,
                              COALESCE(s.seller_name, h.seller_id) AS name,
                              h.stock
                       FROM fct_keepa_seller_history h
                       LEFT JOIN dim_keepa_seller s ON s.seller_id = h.seller_id
                       WHERE h.asin = ? AND h.stock IS NOT NULL
                         AND h.change_time >= datetime('now', ?)
                       ORDER BY h.change_time""",
                    (asin, f"-{days} days"),
                )
                if history.empty:
                    st.info(f"No stock events in the last {days} days. Try a wider window in the sidebar.")
                else:
                    history["legend"] = history.apply(
                        lambda r: f"{(r['name'] or '')[:22]} ({r['seller_id']})", axis=1
                    )
                    fig = px.line(
                        history, x="Date", y="stock", color="legend", markers=True,
                        labels={"stock": "Stock (units)", "legend": "Seller"},
                    )
                    fig.update_traces(line=dict(width=2, shape="hv"), marker=dict(size=4))
                    fig.update_layout(
                        height=440, plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                        margin=dict(l=8, r=8, t=8, b=8),
                        legend=dict(orientation="v", yanchor="top", y=1, xanchor="left",
                                    x=1.02, bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
                    )
                    fig.update_xaxes(gridcolor="rgba(127,127,127,0.08)")
                    fig.update_yaxes(gridcolor="rgba(127,127,127,0.14)")
                    st.plotly_chart(fig, use_container_width=True)

            # Daily TOTAL stock across all sellers
            with st.container(border=True):
                st.markdown('<div class="chart-title">📊 Daily total stock (all sellers combined)</div>',
                            unsafe_allow_html=True)
                st.markdown('<div class="chart-caption">Sum of latest stock per seller per day.</div>',
                            unsafe_allow_html=True)
                daily = query(
                    """WITH per_day_seller AS (
                          SELECT DATE(change_time) AS day, seller_id, stock,
                                 ROW_NUMBER() OVER (PARTITION BY DATE(change_time), seller_id
                                                    ORDER BY change_time DESC) AS rn
                          FROM fct_keepa_seller_history
                          WHERE asin = ? AND stock IS NOT NULL
                            AND change_time >= datetime('now', ?)
                       )
                       SELECT day AS Date, SUM(stock) AS total_stock,
                              COUNT(seller_id) AS sellers
                       FROM per_day_seller WHERE rn = 1
                       GROUP BY day ORDER BY day""",
                    (asin, f"-{days} days"),
                )
                if not daily.empty:
                    fig = px.area(daily, x="Date", y="total_stock",
                                  hover_data={"sellers": True},
                                  color_discrete_sequence=["#6366f1"])
                    fig.update_traces(line=dict(width=2.5))
                    fig.update_layout(height=260, plot_bgcolor="rgba(0,0,0,0)",
                                      paper_bgcolor="rgba(0,0,0,0)", showlegend=False,
                                      margin=dict(l=8, r=8, t=8, b=8))
                    fig.update_xaxes(title=None, gridcolor="rgba(127,127,127,0.08)")
                    fig.update_yaxes(title="Total units", gridcolor="rgba(127,127,127,0.14)")
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("No data in window.")
        else:
            # ── Single seller drill ──
            single = query(
                """SELECT change_time AS Date, stock, price_cents/100.0 AS price_usd
                   FROM fct_keepa_seller_history
                   WHERE asin = ? AND seller_id = ?
                     AND change_time >= datetime('now', ?)
                   ORDER BY change_time""",
                (asin, sel_seller, f"-{days} days"),
            )
            cs = st.columns(2, gap="medium")
            with cs[0].container(border=True):
                st.markdown('<div class="chart-title">📦 Stock</div>', unsafe_allow_html=True)
                st.markdown(f'<div class="chart-caption">Every stock change for <code>{sel_seller}</code>.</div>',
                            unsafe_allow_html=True)
                stk = single[single["stock"].notna()]
                if stk.empty:
                    st.info("No stock data.")
                else:
                    fig = px.line(stk, x="Date", y="stock", markers=True,
                                  color_discrete_sequence=["#10b981"])
                    fig.update_traces(line=dict(width=2.5, shape="hv"))
                    fig.update_layout(height=320, plot_bgcolor="rgba(0,0,0,0)",
                                      paper_bgcolor="rgba(0,0,0,0)",
                                      margin=dict(l=8, r=8, t=8, b=8))
                    fig.update_xaxes(title=None, gridcolor="rgba(127,127,127,0.08)")
                    fig.update_yaxes(title="Stock", gridcolor="rgba(127,127,127,0.14)")
                    st.plotly_chart(fig, use_container_width=True)
            with cs[1].container(border=True):
                st.markdown('<div class="chart-title">💵 Price</div>', unsafe_allow_html=True)
                st.markdown('<div class="chart-caption">Every price change this seller has shown.</div>',
                            unsafe_allow_html=True)
                pr = single[single["price_usd"].notna()]
                if pr.empty:
                    st.info("No price data.")
                else:
                    fig = px.line(pr, x="Date", y="price_usd", markers=True,
                                  color_discrete_sequence=["#6366f1"])
                    fig.update_traces(line=dict(width=2.5, shape="hv"))
                    fig.update_layout(height=320, plot_bgcolor="rgba(0,0,0,0)",
                                      paper_bgcolor="rgba(0,0,0,0)",
                                      margin=dict(l=8, r=8, t=8, b=8))
                    fig.update_xaxes(title=None, gridcolor="rgba(127,127,127,0.08)")
                    fig.update_yaxes(title="Price ($)", gridcolor="rgba(127,127,127,0.14)")
                    st.plotly_chart(fig, use_container_width=True)

            # Quick KPIs for this seller
            sold = query(
                """WITH ev AS (
                      SELECT change_time, stock,
                             LAG(stock) OVER (ORDER BY change_time) AS prev_stock
                      FROM fct_keepa_seller_history
                      WHERE asin = ? AND seller_id = ? AND stock IS NOT NULL
                        AND change_time >= datetime('now', ?)
                   )
                   SELECT SUM(CASE WHEN prev_stock > stock THEN prev_stock - stock ELSE 0 END) AS sold,
                          SUM(CASE WHEN prev_stock < stock THEN stock - prev_stock ELSE 0 END) AS restocked,
                          COUNT(*) AS observations
                   FROM ev""",
                (asin, sel_seller, f"-{days} days"),
            ).iloc[0]
            m1, m2, m3 = st.columns(3, gap="medium")
            with m1.container(border=True):
                st.metric(f"⚡ Units sold ({days}d)", f"{int(sold['sold'] or 0):,}",
                          help="Sum of stock decreases — 'stock down = sold' logic.")
            with m2.container(border=True):
                st.metric(f"📦 Units restocked ({days}d)", f"{int(sold['restocked'] or 0):,}")
            with m3.container(border=True):
                st.metric("👁 Observations", f"{int(sold['observations'] or 0):,}")

        # Sellers table — always shown
        section_header("Current sellers for this ASIN")
        st.caption("Most recent stock observation per seller. Click any column header to sort.")
        latest_sellers = query(
            """WITH latest AS (
                  SELECT seller_id, MAX(change_time) AS last_t
                  FROM fct_keepa_seller_history
                  WHERE asin = ? AND stock IS NOT NULL
                  GROUP BY seller_id
               )
               SELECT h.seller_id              AS "Seller ID",
                      COALESCE(s.seller_name, '(name not fetched)') AS "Name",
                      CASE WHEN s.is_amazon = 1 THEN '🛒 Amazon'
                           WHEN h.is_fba = 1    THEN '🚚 FBA'
                           ELSE                       '🏠 FBM' END AS "Channel",
                      h.stock                  AS "Stock",
                      h.change_time            AS "Last seen"
               FROM fct_keepa_seller_history h
               JOIN latest l
                 ON l.seller_id = h.seller_id AND l.last_t = h.change_time
               LEFT JOIN dim_keepa_seller s ON s.seller_id = h.seller_id
               WHERE h.asin = ?
               ORDER BY h.stock DESC""",
            (asin, asin),
        )
        st.dataframe(
            latest_sellers, use_container_width=True, hide_index=True, height=380,
            column_config={
                "Seller ID": st.column_config.TextColumn(width="small"),
                "Name": st.column_config.TextColumn(width="medium"),
                "Channel": st.column_config.TextColumn(width="small"),
                "Stock": st.column_config.NumberColumn(format="%d", width="small"),
            },
        )
    else:
        section_header("Per-seller inventory")
        st.info(
            "No per-seller data for this ASIN yet. "
            "Run `cd pipeline && python keepa_api_offers.py --tick` to fetch it from Keepa API. "
            "Each tick processes ~100 ASINs."
        )

    # ══ Raw history (collapsible) ═════════════════════════════════════
    section_header("Underlying data")
    with st.expander("📋 Raw snapshot history (table)", expanded=False):
        st.dataframe(
            trend.iloc[::-1].reset_index(drop=True),
            use_container_width=True, hide_index=True,
        )


# ══════════════════════════════════════════════════════════════════════════
# VIEW 3: REPLENISHMENT RECOMMENDATIONS
# ══════════════════════════════════════════════════════════════════════════
else:
    st.markdown(
        "<div style='display:flex;align-items:baseline;justify-content:space-between;"
        "margin:0.2rem 0 0.6rem'><h2 style='margin:0;font-weight:800;letter-spacing:-0.01em'>"
        "Replenishment recommendations</h2><span style='opacity:0.5;font-size:0.8rem'>"
        "Per-ASIN ship/hold/avoid call from BSR, velocity, and FBA stock</span></div>",
        unsafe_allow_html=True,
    )

    # ── Sidebar thresholds ──
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 🎯 Replenishment thresholds")
    th = replenishment.Thresholds(
        target_days_of_supply=st.sidebar.slider(
            "Target days of FBA supply", 7, 90, 30,
            help="How much FBA inventory you want to keep on hand."
        ),
        ship_when_days_below=st.sidebar.slider(
            "Ship when days-of-supply <", 3, 45, 21,
            help="Below this DoS the system flags SHIP_NOW."
        ),
        hold_when_days_above=st.sidebar.slider(
            "Hold when days-of-supply >", 30, 120, 60,
            help="Above this DoS the system says HOLD (already enough)."
        ),
        min_velocity_to_ship=st.sidebar.slider(
            "Min velocity to ship (units/day)", 0.0, 5.0, 0.5, step=0.1,
            help="Below this daily velocity, SHIP_NOW won't trigger."
        ),
        bad_velocity=st.sidebar.slider(
            "Avoid-buy velocity (units/day)", 0.0, 2.0, 0.3, step=0.1,
            help="Below this AND poor BSR AND locked buy-box → AVOID_BUY."
        ),
        bad_bsr=st.sidebar.slider(
            "Avoid-buy BSR worse than", 100_000, 2_000_000, 500_000, step=50_000,
            help="High BSR = sells rarely. Combined with bad velocity triggers AVOID."
        ),
        locked_buybox_pct=st.sidebar.slider(
            "Locked buy-box % (top seller dominance)", 50, 100, 80,
            help="If one seller holds the buy-box more than this %, we HOLD."
        ),
        velocity_window_days=st.sidebar.slider(
            "Velocity window (days)", 3, 60, 14,
            help="How many recent days to average velocity over."
        ),
    )

    # ── Compute ──
    with st.spinner("Scoring all ASINs..."):
        recs = replenishment.compute(get_conn(), thresholds=th)

    if recs.empty:
        st.warning("No data to score. Run the Keepa exporter first.")
        st.stop()

    # ── Top KPIs by recommendation class ──
    counts = recs["recommendation"].value_counts().to_dict()
    ship_qty_total = int(recs.loc[recs["recommendation"] == "SHIP_NOW",
                                  "suggested_ship_qty"].fillna(0).sum())

    section_header("Summary")

    def class_card(col, *, label, count, color, icon, sub=""):
        with col.container(border=True):
            st.markdown(
                f"<div style='font-size:0.65rem;font-weight:700;letter-spacing:0.1em;"
                f"text-transform:uppercase;opacity:0.55;margin-bottom:0.2rem'>"
                f"{icon} {label}</div>"
                f"<div style='font-size:2rem;font-weight:800;color:{color};"
                f"line-height:1.05;margin:0.15rem 0'>{count:,}</div>"
                f"<div style='font-size:0.78rem;opacity:0.65'>{sub}</div>",
                unsafe_allow_html=True,
            )

    c1, c2, c3, c4 = st.columns(4, gap="medium")
    class_card(c1, label="Ship now", count=counts.get("SHIP_NOW", 0),
               color="#10b981", icon="🚀",
               sub=f"≈ {ship_qty_total:,} units total recommended")
    class_card(c2, label="Hold", count=counts.get("HOLD", 0),
               color="#f59e0b", icon="⏳",
               sub="Enough stock, locked buy-box, or slow")
    class_card(c3, label="Avoid buying more", count=counts.get("AVOID_BUY", 0),
               color="#ef4444", icon="🛑",
               sub="Slow + poor BSR + monopolized")
    class_card(c4, label="Watch", count=counts.get("WATCH", 0),
               color="#94a3b8", icon="⚠️",
               sub="Not enough days of data yet")

    # ── Filter row ──
    section_header("Filtered list")
    f1, f2, f3 = st.columns([2, 2, 1], gap="medium")
    with f1:
        chosen = st.multiselect(
            "Recommendation",
            options=["SHIP_NOW", "HOLD", "AVOID_BUY", "WATCH"],
            default=["SHIP_NOW"],
        )
    with f2:
        sort_by = st.selectbox(
            "Sort by",
            options=["score", "suggested_ship_qty", "days_of_supply",
                     "velocity_used", "bsr", "fba_stock"],
            index=1,
            format_func=lambda c: {
                "score": "Score (high → low)",
                "suggested_ship_qty": "Suggested ship quantity (high → low)",
                "days_of_supply": "Days of supply (low → high)",
                "velocity_used": "Velocity (high → low)",
                "bsr": "BSR (low → high)",
                "fba_stock": "FBA stock (low → high)",
            }[c],
        )
    with f3:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        limit = st.number_input("Show top", min_value=10, max_value=2000, value=100, step=10)

    filt = recs[recs["recommendation"].isin(chosen)] if chosen else recs

    # Sort direction depends on field semantics
    ascending = sort_by in ("days_of_supply", "bsr", "fba_stock")
    filt = filt.sort_values(sort_by, ascending=ascending, na_position="last").head(int(limit))

    # ── Table ──
    if filt.empty:
        st.info("Nothing matches the current filters.")
    else:
        table = filt.copy()
        table["Image"] = table["image_url"].apply(lambda u: resize_amazon_image(u, size=120))
        table["Reasons"] = table["reasons"].apply(lambda lst: "  •  ".join(lst) if lst else "")
        display = table[[
            "Image", "asin", "title", "recommendation", "score",
            "suggested_ship_qty", "fba_stock", "velocity_used", "days_of_supply",
            "bsr", "buy_box_price", "top_seller_30d", "velocity_source", "Reasons",
        ]].rename(columns={
            "asin": "ASIN", "title": "Title", "recommendation": "Call",
            "score": "Score", "suggested_ship_qty": "Ship qty",
            "fba_stock": "FBA stock", "velocity_used": "Velocity (u/d)",
            "days_of_supply": "Days of supply", "bsr": "BSR",
            "buy_box_price": "Buy-Box $", "top_seller_30d": "Top seller 30d %",
            "velocity_source": "Velocity src",
        })

        st.caption(
            "👇 To see trend charts for any ASIN, **scroll below the table** and pick it "
            "from the drill-down dropdown. (Clicking a row in the table also selects it.)"
        )
        event = st.dataframe(
            display,
            use_container_width=True, hide_index=True, height=520,
            on_select="rerun",
            selection_mode="single-row",
            key="replen_table",
            column_config={
                "Image": st.column_config.ImageColumn("📷", width="small"),
                "ASIN": st.column_config.TextColumn(width="small"),
                "Title": st.column_config.TextColumn(width="large"),
                "Call": st.column_config.TextColumn(width="small"),
                "Score": st.column_config.NumberColumn(format="%d", width="small"),
                "Ship qty": st.column_config.NumberColumn(format="%d", width="small"),
                "FBA stock": st.column_config.NumberColumn(format="%d", width="small"),
                "Velocity (u/d)": st.column_config.NumberColumn(format="%.2f"),
                "Days of supply": st.column_config.NumberColumn(format="%.0f"),
                "BSR": st.column_config.NumberColumn(format="%d"),
                "Buy-Box $": st.column_config.NumberColumn(format="$%.2f"),
                "Top seller 30d %": st.column_config.NumberColumn(format="%.0f%%"),
                "Velocity src": st.column_config.TextColumn(width="small"),
                "Reasons": st.column_config.TextColumn(width="large"),
            },
        )
        # CSV download
        csv = filt.drop(columns=["image_url"]).to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download this list as CSV",
            data=csv, file_name=f"replenishment_{date.today():%Y%m%d}.csv",
            mime="text/csv",
        )

        # ══════════════════════════════════════════════════════════════
        # DRILL-DOWN: pick an ASIN → trend charts appear
        # ══════════════════════════════════════════════════════════════
        section_header("Drill into one ASIN")

        # Two paths to pick an ASIN:
        # 1. Click a row in the table above (event.selection.rows)
        # 2. Pick from the dropdown below (always visible / reliable)
        clicked_idx = (
            event.selection.rows[0]
            if event and event.selection and event.selection.rows
            else None
        )
        clicked_asin = display.iloc[clicked_idx]["ASIN"] if clicked_idx is not None else None

        # Build dropdown options from the filtered list
        asin_options: dict[str, str] = {}
        for _, row in filt.iterrows():
            label = (
                f"[{row['recommendation']}]  {row['asin']}  —  "
                f"{(row['title'] or '')[:65]}"
                + (f"  (ship {int(row['suggested_ship_qty'])})"
                   if pd.notna(row.get("suggested_ship_qty")) else "")
            )
            asin_options[label] = row["asin"]

        default_index = 0
        if clicked_asin:
            for i, (_, a) in enumerate(asin_options.items()):
                if a == clicked_asin:
                    default_index = i
                    break

        picked_label = st.selectbox(
            "Pick an ASIN to see its trend charts",
            options=list(asin_options.keys()),
            index=default_index if asin_options else None,
            key="replen_drill_select",
            help="Defaults to whichever row you last clicked in the table above.",
        )

        if picked_label:
            sel_asin = asin_options[picked_label]
            sel_full = filt[filt["asin"] == sel_asin].iloc[0]

            # Pull trend history for this ASIN (cumulative from fct_keepa_daily + v_daily_sales)
            drill = pd.read_sql_query(
                """
                SELECT
                    k.snapshot_date         AS Date,
                    k.sales_rank_current    AS bsr,
                    k.sales_rank_30d_avg    AS bsr_30d,
                    k.buy_box_price         AS bb_price,
                    k.buy_box_stock         AS bb_stock,
                    k.fba_stock             AS fba_stock,
                    k.fba_offers            AS fba_offers,
                    k.fbm_offers            AS fbm_offers,
                    k.total_offers          AS total_offers,
                    k.oos_90d_pct           AS oos_90d,
                    k.pct_top_seller_30d    AS top_30d,
                    v.units_sold_per_day    AS daily_sold,
                    v.units_sold            AS units_sold_raw
                FROM fct_keepa_daily k
                LEFT JOIN v_daily_sales v
                       ON v.asin = k.asin
                      AND v.snapshot_date = k.snapshot_date
                WHERE k.asin = ?
                ORDER BY k.snapshot_date
                """,
                get_conn(),
                params=(sel_asin,),
            )

            # Compute days-of-supply per snapshot: fba_stock / daily_sold (NaN if missing)
            drill["days_of_supply"] = (
                drill["fba_stock"] / drill["daily_sold"].where(drill["daily_sold"] > 0)
            )

            # ── Hero strip: image + recommendation + reasons ──────────
            img_url = resize_amazon_image(sel_full.get("image_url"), size=400)
            img_html = (
                f"<img src='{img_url}' style='width:100%;max-height:160px;"
                f"object-fit:contain;border-radius:10px;background:rgba(127,127,127,0.04)'/>"
                if img_url else
                "<div style='width:100%;height:160px;display:flex;align-items:center;"
                "justify-content:center;border:1px dashed rgba(127,127,127,0.30);"
                "border-radius:10px;opacity:0.45;font-size:0.85rem'>no image</div>"
            )
            call_colors = {
                "SHIP_NOW": "#10b981", "HOLD": "#f59e0b",
                "AVOID_BUY": "#ef4444", "WATCH": "#94a3b8",
            }
            call_color = call_colors.get(sel_full["recommendation"], "#94a3b8")
            reasons_html = "".join(
                f"<li style='margin-bottom:0.2rem'>{r}</li>" for r in (sel_full.get("reasons") or [])
            )
            ship_qty = sel_full.get("suggested_ship_qty")
            ship_qty_block = (
                f"<div style='font-size:0.65rem;font-weight:700;letter-spacing:0.1em;"
                f"text-transform:uppercase;opacity:0.55'>Suggested ship quantity</div>"
                f"<div style='font-size:2.5rem;font-weight:800;color:{call_color};"
                f"line-height:1.05;margin:0.2rem 0'>{int(ship_qty):,}</div>"
                f"<div style='font-size:0.78rem;opacity:0.7'>units to send to FBA</div>"
                if pd.notna(ship_qty) else
                f"<div style='font-size:0.65rem;font-weight:700;letter-spacing:0.1em;"
                f"text-transform:uppercase;opacity:0.55'>Recommendation</div>"
                f"<div style='font-size:1.6rem;font-weight:800;color:{call_color};"
                f"line-height:1.1;margin:0.3rem 0'>{sel_full['recommendation']}</div>"
                f"<div style='font-size:0.78rem;opacity:0.7'>No ship quantity (not a SHIP_NOW)</div>"
            )
            st.markdown(
                f"""
                <div style='border:1px solid rgba(127,127,127,0.18);border-radius:12px;
                            padding:1rem 1.15rem;background:rgba(255,255,255,0.02);
                            box-shadow:0 1px 2px rgba(0,0,0,0.04), 0 3px 10px rgba(0,0,0,0.05);
                            margin-bottom:0.6rem;'>
                  <div style='display:flex;gap:1.25rem;align-items:center;flex-wrap:wrap'>
                    <div style='flex:0 0 160px'>{img_html}</div>
                    <div style='flex:1.6;min-width:280px'>
                      <div style='font-size:0.62rem;font-weight:700;letter-spacing:0.12em;
                                  text-transform:uppercase;opacity:0.55;margin-bottom:0.15rem'>
                        {(sel_full.get('brand') or '').upper()}
                      </div>
                      <h3 style='margin:0 0 0.5rem 0;line-height:1.25;font-weight:700;font-size:1.1rem'>
                        {sel_full['title'] or sel_asin}
                      </h3>
                      <code style='background:rgba(127,127,127,0.12);padding:0.15rem 0.45rem;
                                   border-radius:5px;font-size:0.75rem'>{sel_asin}</code>
                      <div style='display:flex;gap:0.5rem;margin-top:0.7rem'>
                        <a href='https://www.amazon.com/dp/{sel_asin}?th=1&psc=1' target='_blank'
                           style='padding:0.3rem 0.7rem;border-radius:7px;
                                  background:rgba(99,102,241,0.12);
                                  border:1px solid rgba(99,102,241,0.35);
                                  color:inherit;text-decoration:none;
                                  font-size:0.75rem;font-weight:600'>Amazon ↗</a>
                        <a href='https://keepa.com/#!product/1-{sel_asin}' target='_blank'
                           style='padding:0.3rem 0.7rem;border-radius:7px;
                                  background:rgba(127,127,127,0.10);
                                  border:1px solid rgba(127,127,127,0.25);
                                  color:inherit;text-decoration:none;
                                  font-size:0.75rem;font-weight:600'>Keepa ↗</a>
                      </div>
                    </div>
                    <div style='flex:1;min-width:200px;padding:0 1rem;
                                border-left:1px solid rgba(127,127,127,0.18)'>
                      {ship_qty_block}
                    </div>
                    <div style='flex:1.4;min-width:280px;padding-left:1rem;
                                border-left:1px solid rgba(127,127,127,0.18)'>
                      <div style='font-size:0.62rem;font-weight:700;letter-spacing:0.12em;
                                  text-transform:uppercase;opacity:0.55;margin-bottom:0.4rem'>
                        Why · {sel_full['recommendation']}
                      </div>
                      <ul style='margin:0;padding-left:1.1rem;font-size:0.82rem;line-height:1.5'>
                        {reasons_html or '<li>—</li>'}
                      </ul>
                    </div>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # ── Trend charts (3 × 2 grid) ──────────────────────────────
            def trend_card(col, *, title, caption, traces, y_title="",
                           invert_y=False, height=240, fill=False, fill_color=None):
                with col.container(border=True):
                    st.markdown(f'<div class="chart-title">{title}</div>',
                                unsafe_allow_html=True)
                    st.markdown(f'<div class="chart-caption">{caption}</div>',
                                unsafe_allow_html=True)
                    fig = go.Figure()
                    for tr in traces:
                        if tr["col"] not in drill.columns or drill[tr["col"]].notna().sum() == 0:
                            continue
                        kwargs = dict(
                            x=drill["Date"], y=drill[tr["col"]],
                            mode="lines+markers", name=tr["name"],
                            line=dict(color=tr.get("color"), width=tr.get("width", 2.5),
                                      dash=tr.get("dash"), shape="spline", smoothing=0.6),
                            marker=dict(size=6), connectgaps=False,
                        )
                        if fill and len(traces) == 1:
                            kwargs["fill"] = "tozeroy"
                            kwargs["fillcolor"] = fill_color or "rgba(99,102,241,0.12)"
                        fig.add_trace(go.Scatter(**kwargs))
                    if not fig.data:
                        st.markdown(
                            f"<div style='display:flex;align-items:center;justify-content:center;"
                            f"height:{height-30}px;opacity:0.45;font-size:0.85rem'>"
                            "no data</div>", unsafe_allow_html=True,
                        )
                        return
                    fig.update_yaxes(
                        title_text=y_title, gridcolor="rgba(127,127,127,0.14)",
                        zerolinecolor="rgba(127,127,127,0.2)",
                        autorange="reversed" if invert_y else True,
                    )
                    fig.update_xaxes(title_text=None, gridcolor="rgba(127,127,127,0.08)")
                    fig.update_layout(
                        height=height, margin=dict(l=8, r=8, t=8, b=8),
                        font=dict(family="Inter, -apple-system, system-ui, sans-serif", size=11),
                        legend=dict(orientation="h", yanchor="bottom", y=-0.3,
                                    xanchor="center", x=0.5, bgcolor="rgba(0,0,0,0)",
                                    font=dict(size=10)) if len(fig.data) > 1
                                    else dict(visible=False),
                        hovermode="x unified",
                        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                    )
                    st.plotly_chart(fig, use_container_width=True,
                                    config={"displayModeBar": False})

            # Row 1: BSR | Buy-Box price | Daily sold
            r1c1, r1c2, r1c3 = st.columns(3, gap="medium")
            trend_card(
                r1c1, title="📈 BSR", caption="Lower = better. Y-axis inverted.",
                traces=[
                    {"col": "bsr", "name": "BSR", "color": "#6366f1"},
                    {"col": "bsr_30d", "name": "30d avg", "color": "#94a3b8",
                     "dash": "dot", "width": 1.5},
                ],
                y_title="Rank", invert_y=True,
            )
            trend_card(
                r1c2, title="🏷️ Buy-Box price",
                caption="Price Amazon shows the customer.",
                traces=[{"col": "bb_price", "name": "Buy-Box $", "color": "#10b981"}],
                y_title="Price ($)",
                fill=True, fill_color="rgba(16,185,129,0.12)",
            )
            trend_card(
                r1c3, title="⚡ Daily units sold",
                caption="Derived from FBA stock drops (v_daily_sales).",
                traces=[{"col": "daily_sold", "name": "Units/day", "color": "#eab308"}],
                y_title="Units/day",
                fill=True, fill_color="rgba(234,179,8,0.15)",
            )

            # Row 2: Stock (FBA + Buy-Box) | Offer count (FBA/FBM/Total) | Days of supply
            r2c1, r2c2, r2c3 = st.columns(3, gap="medium")
            trend_card(
                r2c1, title="📦 Stock levels",
                caption="FBA aggregate (all FBA sellers) vs current Buy-Box winner.",
                traces=[
                    {"col": "fba_stock", "name": "FBA stock", "color": "#10b981"},
                    {"col": "bb_stock",  "name": "Buy-Box stock", "color": "#f97316"},
                ],
                y_title="Units",
            )
            trend_card(
                r2c2, title="👥 Seller offer count",
                caption="FBA = fulfilled by Amazon, FBM = by merchant.",
                traces=[
                    {"col": "fba_offers", "name": "FBA", "color": "#10b981"},
                    {"col": "fbm_offers", "name": "FBM", "color": "#f43f5e"},
                    {"col": "total_offers", "name": "Total",
                     "color": "#94a3b8", "dash": "dot", "width": 1.5},
                ],
                y_title="Sellers",
            )
            trend_card(
                r2c3, title="⏳ Days of supply",
                caption="FBA stock ÷ daily velocity. Lower = ship sooner.",
                traces=[{"col": "days_of_supply", "name": "Days", "color": "#a855f7"}],
                y_title="Days",
                fill=True, fill_color="rgba(168,85,247,0.13)",
            )

            # Raw data expander
            with st.expander("📋 Raw snapshot history for this ASIN"):
                st.dataframe(drill.iloc[::-1].reset_index(drop=True),
                             use_container_width=True, hide_index=True)

    # ── Methodology expander ──
    with st.expander("ℹ️ How recommendations are computed", expanded=False):
        st.markdown(f"""
**Inputs per ASIN (from `fct_keepa_daily`, `v_asin_daily_sales`, `v_daily_sales`):**
- Current BSR, buy-box price, FBA stock, FBA/FBM offers, top-seller share
- **Velocity (units/day)** — measured over the last **{th.velocity_window_days} days**.
  Source priority (first available wins):
  1. **Per-seller** (canonical): true units sold from each seller's stock drops (`v_asin_daily_sales`). Restocks excluded.
  2. **FBA-delta**: day-over-day drops in aggregate `fba_stock` (`v_daily_sales`). Used only when no per-seller data exists yet for the ASIN.
  3. **Keepa monthly**: last-resort fallback — Keepa's "Monthly Sold" ÷ 30.
- **Days of supply** = `fba_stock ÷ velocity`

**Decision tree (in order):**
1. **WATCH** — if velocity is unknown (need more days of data).
2. **AVOID_BUY** — velocity < {th.bad_velocity}/d AND BSR > {th.bad_bsr:,} AND top-seller ≈ {th.locked_buybox_pct}%.
3. **SHIP_NOW** — FBA stock = 0 AND BSR healthy AND velocity ≥ {th.min_velocity_to_ship}/d.
4. **HOLD** — days_of_supply > {th.hold_when_days_above}, OR top-seller > {th.locked_buybox_pct}%.
5. **SHIP_NOW** — days_of_supply < {th.ship_when_days_below} AND velocity ≥ {th.min_velocity_to_ship}/d.
6. **HOLD** — velocity below ship threshold (slow seller).
7. **WATCH** — velocity known but FBA stock not.
8. **HOLD** — default (healthy stock, healthy velocity).

**Suggested ship quantity** (when `SHIP_NOW`):
`qty = max(0, target_days_of_supply × velocity − current_fba_stock)`
With target = **{th.target_days_of_supply}** days. *(Assumes unlimited warehouse stock — gets capped to your real warehouse inventory once that table is wired in.)*

**Limitations today:**
- Only **{len(recs[recs['recommendation'] != 'WATCH']):,} of {len(recs):,} ASINs** have enough data for a confident call (rest are WATCH). This improves automatically as you run the exporter more days.
- We can't see FBM stock (Keepa doesn't expose it).
- For ASINs without per-seller data yet, "sold" falls back to **FBA stock drops** — restocks between snapshots are invisible, so velocity may be slightly understated. This resolves automatically as the per-seller backfill reaches the ASIN.
""")


