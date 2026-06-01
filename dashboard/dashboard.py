"""
Amazon Tracker — modern dark dashboard (high-contrast, card-based).

Reads the live SQLite the collector writes (WAL). Two views:
  • Trends        — one product: units sold, BSR, prices, per-seller stock, sellers
  • Replenishment — SHIP/HOLD/AVOID table across the catalog

Cards use real Streamlit containers (so charts sit INSIDE them), proper date axes
(no microsecond ticks on sparse data), and a high-contrast palette.

Run (via scripts/start_dashboard.sh):
    DB_PATH=pipeline/amazon_tracker.db streamlit run dashboard/dashboard.py
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_pipeline_dir = Path(__file__).resolve().parent.parent / "pipeline"
if str(_pipeline_dir) not in sys.path:
    sys.path.insert(0, str(_pipeline_dir))

DB_PATH = os.getenv("DB_PATH", str(_pipeline_dir / "amazon_tracker.db"))

# ── Lazy seller-name resolver ─────────────────────────────────────────────────
# When the dashboard encounters seller IDs with no name, it resolves them on the
# spot via Keepa's /seller endpoint (1 token per seller, called once and stored
# permanently). st.cache_data ensures we only call the API once per unique set of
# unknown IDs — never again for the same sellers, even across refreshes.
@st.cache_data(ttl=86400, show_spinner=False)
def _resolve_seller_names(unknown_ids: tuple) -> int:
    """Fetch real names for a tuple of unknown seller IDs. 1 token each.
    Returns the number of names successfully resolved and written to the DB.
    Side-effect: writes to dim_keepa_seller. Cached so it only fires once per
    unique (unknown_ids) set regardless of how many times the page refreshes.
    """
    if not unknown_ids:
        return 0
    try:
        from dotenv import load_dotenv
        import requests as _req
        load_dotenv(_pipeline_dir / ".env")
        key = os.getenv("KEEPA_API_KEY")
        if not key:
            return 0
        resolved = 0
        ids = list(unknown_ids)
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            r = _req.get("https://api.keepa.com/seller",
                         params={"key": key, "domain": 1, "seller": ",".join(chunk)},
                         timeout=30)
            if r.status_code != 200:
                break
            sellers = r.json().get("sellers") or {}
            now = pd.Timestamp.utcnow().isoformat()
            conn = get_conn()
            for sid, info in sellers.items():
                name = info.get("sellerName") or ""
                rating = info.get("currentRating")
                if name:
                    conn.execute("""
                        INSERT INTO dim_keepa_seller
                            (seller_id, seller_name, rating_pct, is_amazon, updated_at)
                        VALUES (?, ?, ?, 0, ?)
                        ON CONFLICT(seller_id) DO UPDATE SET
                          seller_name = excluded.seller_name,
                          rating_pct  = COALESCE(excluded.rating_pct, rating_pct),
                          updated_at  = excluded.updated_at
                    """, (sid, name, rating, now))
                    resolved += 1
            conn.commit()
        return resolved
    except Exception:
        return 0
st.set_page_config(page_title="Amazon Tracker", page_icon="📦", layout="wide")

# ── High-contrast palette ────────────────────────────────────────────────────
BG       = "#080a0f"      # app background (deep)
CARD     = "#161c28"      # card surface — clearly lighter than BG
CARD2    = "#1d2533"      # raised surface (pills, KPIs)
BORDER   = "rgba(255,255,255,0.12)"
TEXT     = "#f3f5f8"      # bright primary text
MUTED    = "#9aa6b8"      # secondary text
INDIGO, GREEN, RED, ORANGE, CYAN, VIOLET = (
    "#8b95ff", "#3ddc97", "#ff6b81", "#ffb020", "#28d4ee", "#c084fc")
GRID = "rgba(255,255,255,0.09)"
AXIS = "#aab4c5"


# ── Auth ─────────────────────────────────────────────────────────────────────
def _check_password() -> bool:
    expected = os.getenv("DASHBOARD_PASSWORD")
    if not expected or st.session_state.get("_authed"):
        return True
    st.markdown("<div style='max-width:340px;margin:14vh auto 0'>", unsafe_allow_html=True)
    st.markdown("### 🔒 Amazon Tracker")
    pw = st.text_input("Password", type="password", label_visibility="collapsed",
                       placeholder="Password")
    if pw == expected:
        st.session_state["_authed"] = True
        st.rerun()
    elif pw:
        st.error("Incorrect password.")
    st.markdown("</div>", unsafe_allow_html=True)
    return False


if not _check_password():
    st.stop()


# ── Global CSS ───────────────────────────────────────────────────────────────
st.markdown(f"""
<style>
  .stApp {{ background:
      radial-gradient(1200px 600px at 12% -5%, #11161f 0%, {BG} 55%) fixed; }}
  .block-container {{ padding-top:1.2rem; padding-bottom:3rem; max-width:1480px; }}
  #MainMenu, footer, header {{ visibility:hidden; }}
  [data-testid="stSidebar"] {{ background:#0c1018; border-right:1px solid {BORDER}; }}
  [data-testid="stSidebar"] * {{ color:{TEXT}; }}
  h1,h2,h3,h4,p,span,label,div {{ color:{TEXT}; }}
  h1,h2,h3 {{ letter-spacing:-0.02em; }}

  /* Real Streamlit container → polished card that ACTUALLY wraps its charts */
  [data-testid="stVerticalBlockBorderWrapper"] {{
      background:{CARD}; border:1px solid {BORDER} !important; border-radius:16px;
      padding:14px 16px 6px !important;
      box-shadow:0 1px 0 rgba(255,255,255,0.04) inset, 0 8px 24px rgba(0,0,0,0.45);
  }}

  .ctitle {{ font-weight:750; font-size:1rem; }}
  .csub   {{ font-size:0.78rem; color:{MUTED}; margin:1px 0 6px; line-height:1.35; }}

  .kpi {{ background:{CARD2}; border:1px solid {BORDER}; border-radius:14px;
          padding:13px 16px; flex:1; min-width:120px;
          box-shadow:0 6px 18px rgba(0,0,0,0.40); }}
  .kpi .l {{ font-size:0.66rem; letter-spacing:0.09em; text-transform:uppercase;
             color:{MUTED}; font-weight:800; }}
  .kpi .v {{ font-size:1.85rem; font-weight:850; margin-top:2px; line-height:1.05; }}

  .pill {{ display:inline-flex;align-items:center;gap:8px;background:{CARD2};
           border:1px solid {BORDER};border-radius:999px;padding:6px 14px;
           font-size:0.82rem;font-weight:600; }}
  .pill b {{ color:{TEXT}; }}

  .sec {{ display:flex;align-items:center;gap:10px;margin:18px 0 8px; }}
  .sec .bar {{ width:4px;height:18px;border-radius:3px;
               background:linear-gradient(180deg,{INDIGO},{VIOLET}); }}
  .sec .lbl {{ font-size:0.72rem;font-weight:800;letter-spacing:0.14em;
               text-transform:uppercase;color:{MUTED}; }}

  @keyframes pulse {{ 0%{{opacity:1}} 50%{{opacity:.35}} 100%{{opacity:1}} }}
  .dot {{ width:9px;height:9px;border-radius:99px;animation:pulse 1.8s infinite; }}
</style>
""", unsafe_allow_html=True)


# ── DB ───────────────────────────────────────────────────────────────────────
@st.cache_resource
def get_conn():
    if not os.path.exists(DB_PATH):
        st.error(f"Database not found at `{DB_PATH}`."); st.stop()
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    return c


@st.cache_data(ttl=30)
def q(sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, get_conn(), params=params)


def table_exists(name: str) -> bool:
    return get_conn().execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


_SIZE_RE = re.compile(r"\._[A-Z]{2}\d+_+")
def img_url(url, size=400):
    if not url or "media-amazon.com/images/I/" not in url:
        return url
    return _SIZE_RE.sub("", url).replace(".jpg", f"._SL{size}_.jpg")


# ── Card (a real container that wraps its content) + section header ──────────
@contextmanager
def card(title, subtitle="", col=None):
    box = (col.container(border=True) if col is not None else st.container(border=True))
    with box:
        st.markdown(f"<div class='ctitle'>{title}</div>"
                    f"<div class='csub'>{subtitle}</div>", unsafe_allow_html=True)
        yield


def section(label):
    st.markdown(f"<div class='sec'><span class='bar'></span><span class='lbl'>{label}</span></div>",
                unsafe_allow_html=True)


# ── Charts: clean, readable, correct date axes ───────────────────────────────
def _layout(height=290):
    return dict(height=height, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color=TEXT, size=12.5, family="-apple-system,system-ui,sans-serif"),
                margin=dict(l=8, r=14, t=6, b=4), hovermode="x unified",
                legend=dict(orientation="h", y=-0.2, x=0.5, xanchor="center",
                            bgcolor="rgba(0,0,0,0)", font=dict(size=11, color=MUTED)))


def time_axis(fig, start, end, y_title="", invert=False):
    span = max((end - start).days, 1)
    fig.update_xaxes(type="date", range=[start.isoformat(), (end + timedelta(days=1)).isoformat()],
                     tickformat="%b %d", dtick="D1" if span <= 16 else None, nticks=8,
                     gridcolor=GRID, color=AXIS, showline=False, zeroline=False)
    fig.update_yaxes(title=dict(text=y_title, font=dict(color=MUTED, size=11)),
                     gridcolor=GRID, color=AXIS, zeroline=False,
                     autorange="reversed" if invert else True)
    return fig


def empty(msg, height=290):
    fig = go.Figure(); fig.update_layout(**_layout(height))
    fig.add_annotation(text="✦ " + msg, showarrow=False, font=dict(color=MUTED, size=13))
    fig.update_xaxes(visible=False); fig.update_yaxes(visible=False)
    return fig


CFG = {"displayModeBar": False}
def show(fig): st.plotly_chart(fig, use_container_width=True, config=CFG)


# ── Live status ──────────────────────────────────────────────────────────────
@st.cache_data(ttl=20)
def live_status():
    c = get_conn()
    d = {"catalog": c.execute("SELECT COUNT(*) FROM dim_product").fetchone()[0],
         "fetched": c.execute("SELECT COUNT(*) FROM asin_api_state WHERE last_fetched_at IS NOT NULL").fetchone()[0],
         "last_fetch": c.execute("SELECT MAX(last_fetched_at) FROM asin_api_state").fetchone()[0],
         "stock_events": c.execute("SELECT COUNT(*) FROM fct_keepa_seller_history WHERE stock IS NOT NULL").fetchone()[0],
         "sellers": c.execute("SELECT COUNT(*) FROM dim_keepa_seller").fetchone()[0]}
    if table_exists("api_token_log"):
        r = c.execute("SELECT tokens_left FROM api_token_log WHERE endpoint='product' ORDER BY ts DESC LIMIT 1").fetchone()
        d["tokens_left"] = r[0] if r else None
    return d


def ago(ts):
    if not ts: return "never", False
    try:
        s = (pd.Timestamp.utcnow() - pd.to_datetime(ts, utc=True)).total_seconds()
        rec = s < 45 * 60
        if s < 90: return "just now", rec
        if s < 3600: return f"{int(s//60)}m ago", rec
        if s < 86400: return f"{s/3600:.1f}h ago", rec
        return f"{int(s//86400)}d ago", rec
    except Exception:
        return str(ts), False


# ── Sidebar ──────────────────────────────────────────────────────────────────
st.sidebar.markdown("## 📦 Amazon Tracker")
view = st.sidebar.radio("View", ["📈 Trends", "🎯 Replenishment"], label_visibility="collapsed")
window = st.sidebar.select_slider("Date window", options=[7, 14, 30, 60, 90], value=30,
                                  format_func=lambda d: f"{d} days")
end_d, start_d = date.today(), date.today() - timedelta(days=window)
st.sidebar.markdown("---")
if st.sidebar.toggle("🔴 Live auto-refresh", value=True):
    secs = st.sidebar.select_slider("every", options=[15, 30, 60, 120], value=30,
                                    format_func=lambda s: f"{s}s")
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=secs * 1000, key="auto")
    except Exception:
        st.markdown(f"<meta http-equiv='refresh' content='{secs}'>", unsafe_allow_html=True)

# ── Header + live strip ──────────────────────────────────────────────────────
ls = live_status()
last_ago, is_live = ago(ls.get("last_fetch"))
dot = GREEN if is_live else ORANGE
pct = 100.0 * ls["fetched"] / ls["catalog"] if ls.get("catalog") else 0
st.markdown(f"""
<h2 style='margin:0 0 2px'>📦 Amazon Product Tracker</h2>
<div style='display:flex;gap:10px;flex-wrap:wrap;margin:12px 0 6px'>
  <span class='pill'><span class='dot' style='background:{dot};box-shadow:0 0 10px {dot}'></span>
    <b>{'COLLECTING' if is_live else 'IDLE'}</b></span>
  <span class='pill'>Backfill&nbsp;<b>{ls['fetched']:,}/{ls['catalog']:,}</b>&nbsp;({pct:.0f}%)</span>
  <span class='pill'>Stock events&nbsp;<b>{ls['stock_events']:,}</b></span>
  <span class='pill'>Sellers&nbsp;<b>{ls['sellers']:,}</b></span>
  <span class='pill'>Tokens&nbsp;<b>{ls.get('tokens_left','—')}</b></span>
  <span class='pill'>Last fetch&nbsp;<b>{last_ago}</b></span>
</div>
""", unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
# TRENDS
# ═════════════════════════════════════════════════════════════════════════════
if view == "📈 Trends":
    brands = q("SELECT COALESCE(brand,'(no brand)') brand, COUNT(*) n FROM dim_product GROUP BY 1 ORDER BY 1")
    bopts = ["🌐 All brands"] + [f"{r.brand} ({r.n})" for r in brands.itertuples()]
    pick_brand = st.sidebar.selectbox("Brand", bopts)
    where, params = "", []
    if not pick_brand.startswith("🌐"):
        where, params = "WHERE COALESCE(d.brand,'(no brand)') = ?", [pick_brand.rsplit(" (", 1)[0]]
    asins = q(f"""SELECT d.asin, COALESCE(d.title,d.asin) title, COALESCE(s.n,0) sellers
                  FROM dim_product d
                  LEFT JOIN (SELECT asin, COUNT(DISTINCT seller_id) n FROM fct_keepa_seller_history
                             WHERE stock IS NOT NULL GROUP BY asin) s ON s.asin=d.asin
                  {where} ORDER BY sellers DESC, title LIMIT 4000""", params)
    if asins.empty:
        st.info("No products for this brand yet."); st.stop()
    labels = {f"{'🏪'+str(int(r.sellers)) if r.sellers else '⏳'}  {r.asin} — {r.title[:46]}": r.asin
              for r in asins.itertuples()}
    asin = labels[st.sidebar.selectbox(f"ASIN ({len(labels)})", list(labels.keys()))]

    prod = q("SELECT * FROM dim_product WHERE asin=?", [asin])
    p = prod.iloc[0] if not prod.empty else None
    trend = q("""SELECT snapshot_date Date, sales_rank_current bsr, buy_box_price bb, fba_price,
                        fbm_price, fba_stock, total_offers, fba_offers, fbm_offers, oos_90d_pct oos
                 FROM fct_keepa_daily WHERE asin=? AND snapshot_date>=? ORDER BY snapshot_date""",
              [asin, start_d.isoformat()])
    if not trend.empty:
        trend["Date"] = pd.to_datetime(trend["Date"])
    latest = q("SELECT sales_rank_current bsr, buy_box_price bb, total_offers FROM fct_keepa_daily "
               "WHERE asin=? ORDER BY snapshot_date DESC LIMIT 1", [asin])
    L = latest.iloc[0] if not latest.empty else None

    # Hero
    if p is not None:
        iu = img_url(p["image_url"])
        img = (f"<img src='{iu}' style='width:118px;height:118px;object-fit:contain;border-radius:12px;"
               f"background:#0c1018;border:1px solid {BORDER}'/>" if iu else
               f"<div style='width:118px;height:118px;border-radius:12px;background:#0c1018;"
               f"border:1px solid {BORDER}'></div>")
        chips = "".join(f"<span class='pill'>{x}</span>" for x in
                        [asin, *([f"🎨 {p['variation_color']}"] if p.get('variation_color') else []),
                         *([f"📏 {p['variation_size']}"] if p.get('variation_size') else [])])
        with st.container(border=True):
            links = (
                f"<a href='https://www.amazon.com/dp/{asin}?th=1&psc=1' target='_blank' "
                f"style='padding:6px 14px;border-radius:8px;background:rgba(139,149,255,0.15);"
                f"border:1px solid rgba(139,149,255,0.4);color:{INDIGO};text-decoration:none;"
                f"font-size:0.82rem;font-weight:700'>↗ Amazon</a>"
                f"<a href='https://keepa.com/#!product/1-{asin}' target='_blank' "
                f"style='padding:6px 14px;border-radius:8px;background:rgba(255,255,255,0.06);"
                f"border:1px solid {BORDER};color:{TEXT};text-decoration:none;"
                f"font-size:0.82rem;font-weight:700'>↗ Keepa</a>"
            )
            st.markdown(f"""
            <div style='display:flex;gap:18px;align-items:center'>{img}
              <div style='flex:1;min-width:240px'>
                <div style='font-size:0.68rem;letter-spacing:0.12em;text-transform:uppercase;
                     color:{MUTED};font-weight:800'>{p.get('brand') or 'Unbranded'}</div>
                <div style='font-size:1.18rem;font-weight:750;margin:4px 0 9px'>{p.get('title') or asin}</div>
                <div style='display:flex;gap:7px;flex-wrap:wrap;margin-bottom:10px'>{chips}</div>
                <div style='display:flex;gap:8px'>{links}</div>
              </div></div>""", unsafe_allow_html=True)

    # KPIs (always show — per-seller fills when daily is empty)
    sk = q("""WITH latest AS (SELECT seller_id, MAX(change_time) mt FROM fct_keepa_seller_history
                 WHERE asin=? AND stock IS NOT NULL GROUP BY seller_id)
              SELECT COUNT(*) sellers, COALESCE(SUM(h.stock),0) stock FROM fct_keepa_seller_history h
              JOIN latest l ON l.seller_id=h.seller_id AND l.mt=h.change_time WHERE h.asin=?""", [asin, asin])
    sold = q("SELECT COALESCE(SUM(units_sold),0) s FROM v_asin_daily_sales WHERE asin=? AND sale_date>=?",
             [asin, start_d.isoformat()])
    n_sellers = int(sk.iloc[0]["sellers"]) if not sk.empty else 0
    cur_stock = int(sk.iloc[0]["stock"]) if not sk.empty else 0
    sold_total = int(sold.iloc[0]["s"]) if not sold.empty else 0

    def kpi(label, val, color=TEXT):
        return (f"<div class='kpi'><div class='l'>{label}</div>"
                f"<div class='v' style='color:{color}'>{val}</div></div>")
    def fnum(v, pre="", dash="—"): return f"{pre}{int(v):,}" if pd.notna(v) else dash
    bsr_v = fnum(L["bsr"], "#") if L is not None else "—"
    bb_v = (f"${L['bb']:,.2f}" if L is not None and pd.notna(L['bb']) and L['bb'] > 0 else "—")
    off_v = fnum(L["total_offers"]) if L is not None else "—"
    st.markdown("<div style='display:flex;gap:12px;margin:6px 0 4px;flex-wrap:wrap'>"
                + kpi("Current BSR", bsr_v) + kpi("Buy-box", bb_v, GREEN) + kpi("Total offers", off_v)
                + kpi("Active sellers", f"{n_sellers}", CYAN) + kpi("Total stock", f"{cur_stock:,}", INDIGO)
                + kpi(f"Sold · {window}d", f"{sold_total:,}", VIOLET) + "</div>", unsafe_allow_html=True)

    # ── Sales + BSR ──────────────────────────────────────────────────────────
    section("Sales & ranking")
    ps = q("""SELECT sale_date Date, units_sold, units_restocked
              FROM v_asin_daily_sales WHERE asin=? AND sale_date>=? ORDER BY sale_date""",
           [asin, start_d.isoformat()])
    c1, c2 = st.columns(2)
    with card("📈 Daily units sold", "Per-seller stock drops (restocks excluded) — the measured figure.", c1):
        if ps.empty:
            show(empty("No sales signal in window yet — collecting…"))
        else:
            ps["Date"] = pd.to_datetime(ps["Date"])
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=ps["Date"], y=ps["units_sold"], mode="lines+markers", name="Sold",
                line=dict(color=GREEN, width=3, shape="spline", smoothing=0.4),
                marker=dict(size=7, color=GREEN),
                fill="tozeroy", fillcolor="rgba(61,220,151,0.12)",
                hovertemplate="%{y} sold<extra></extra>"))
            if ps["units_restocked"].fillna(0).sum() > 0:
                fig.add_trace(go.Scatter(
                    x=ps["Date"], y=ps["units_restocked"], mode="lines+markers", name="Restocked",
                    line=dict(color=ORANGE, width=2, dash="dot"),
                    marker=dict(size=5), hovertemplate="%{y} restocked<extra></extra>"))
            fig.update_layout(**_layout()); time_axis(fig, start_d, end_d, "Units")
            show(fig)
            st.caption(f"**{int(ps['units_sold'].fillna(0).sum()):,}** units sold · "
                       f"**{int(ps['units_restocked'].fillna(0).sum()):,}** restocked in the last {window} days.")
    with card("📉 Best Sellers Rank", "Lower = sells more. Axis inverted so up = better.", c2):
        if trend.empty or trend["bsr"].dropna().empty:
            show(empty("No BSR yet — fills on next API fetch."))
        else:
            fig = go.Figure(go.Scatter(
                x=trend["Date"], y=trend["bsr"], mode="lines+markers", name="BSR",
                line=dict(color=INDIGO, width=3, shape="spline", smoothing=0.4),
                marker=dict(size=7), fill="tozeroy", fillcolor="rgba(139,149,255,0.10)"))
            fig.update_layout(**_layout()); time_axis(fig, start_d, end_d, "Rank", invert=True)
            show(fig)

    # ── Prices + per-seller stock ─────────────────────────────────────────────
    section("Pricing & inventory")
    c3, c4 = st.columns(2)
    with card("💵 Prices by channel", "Buy-box vs lowest FBA / FBM.", c3):
        if trend.empty or trend[["bb", "fba_price", "fbm_price"]].dropna(how="all").empty:
            show(empty("No price data in window yet — fills on next API fetch."))
        else:
            fig = go.Figure()
            for col, nm, cl in [("bb", "Buy-Box", INDIGO), ("fba_price", "FBA", GREEN), ("fbm_price", "FBM", RED)]:
                if col in trend and trend[col].notna().any():
                    fig.add_trace(go.Scatter(
                        x=trend["Date"], y=trend[col], mode="lines+markers", name=nm,
                        line=dict(color=cl, width=3, shape="spline", smoothing=0.4),
                        marker=dict(size=6)))
            fig.update_layout(**_layout()); time_axis(fig, start_d, end_d, "Price ($)")
            show(fig)

    # Per-seller stock — aggregated default + two dropdowns (source + seller)
    with card("📦 Per-seller stock", "", c4):
        ss_all = q("""SELECT h.change_time ts,
                             h.seller_id,
                             COALESCE(s.seller_name, h.seller_id) seller_name,
                             CASE WHEN h.is_fba=1 THEN 'FBA' ELSE 'FBM' END src,
                             h.stock
                      FROM fct_keepa_seller_history h
                      LEFT JOIN dim_keepa_seller s ON s.seller_id=h.seller_id
                      WHERE h.asin=? AND h.stock IS NOT NULL AND DATE(h.change_time)>=?
                      ORDER BY h.change_time""",
                   [asin, start_d.isoformat()])

        # Auto-resolve any seller IDs that still have no name — 1 token each,
        # fires once per unique set then is permanently cached.
        if not ss_all.empty:
            unnamed = tuple(sorted(set(
                row["seller_id"] for _, row in ss_all.iterrows()
                if row["seller_name"] == row["seller_id"]   # name fell back to ID
            )))
            if unnamed:
                n = _resolve_seller_names(unnamed)
                if n:
                    # Names were just written — clear data cache so the dropdown
                    # shows real names on the next Streamlit rerun.
                    st.cache_data.clear()
                    st.rerun()
        if ss_all.empty:
            show(empty("No per-seller data for this ASIN yet."))
        else:
            ss_all["ts"] = pd.to_datetime(ss_all["ts"])
            # ── two filter controls ──
            fc1, fc2 = st.columns(2)
            # Dropdown 1: Source (FBA / FBM / All)
            src_opt = ["All", "FBA", "FBM"]
            src_pick = fc1.selectbox("Source", src_opt, key=f"src_{asin}", label_visibility="collapsed",
                                      help="Filter by fulfillment type")
            fc1.caption("Source: " + src_pick)
            # Dropdown 2: Seller name
            seller_opts_df = (ss_all if src_pick == "All" else ss_all[ss_all["src"] == src_pick])
            # Sort by total events so busiest sellers are at top
            seller_counts = seller_opts_df.groupby("seller_name")["stock"].count().sort_values(ascending=False)
            seller_names = ["All sellers"] + list(seller_counts.index)
            seller_pick = fc2.selectbox("Seller", seller_names, key=f"sel_{asin}", label_visibility="collapsed",
                                        help="Pick a specific seller or view aggregate")
            fc2.caption("Seller: " + (seller_pick[:30] if seller_pick != "All sellers" else "All sellers"))

            # Apply filters
            filtered = ss_all.copy()
            if src_pick != "All":
                filtered = filtered[filtered["src"] == src_pick]
            pal = [INDIGO, GREEN, RED, ORANGE, CYAN, VIOLET, "#f472b6", "#38bdf8"]

            fig = go.Figure()
            if seller_pick == "All sellers":
                # Aggregate: resample to daily, sum latest stock per seller per day
                agg = (filtered.sort_values("ts")
                       .assign(day=filtered["ts"].dt.date)
                       .groupby(["day", "seller_name"])
                       .last()
                       .groupby("day")["stock"]
                       .sum()
                       .reset_index()
                       .rename(columns={"day": "Date"}))
                agg["Date"] = pd.to_datetime(agg["Date"])
                label = f"Total stock ({src_pick})" if src_pick != "All" else "Total stock (all sellers)"
                fig.add_trace(go.Scatter(
                    x=agg["Date"], y=agg["stock"], mode="lines+markers", name=label,
                    line=dict(color=CYAN, width=3, shape="spline", smoothing=0.4),
                    marker=dict(size=7), fill="tozeroy", fillcolor="rgba(40,212,238,0.10)"))
            else:
                # Single seller: show their stock over time
                one = filtered[filtered["seller_name"] == seller_pick]
                fig.add_trace(go.Scatter(
                    x=one["ts"], y=one["stock"], mode="lines+markers", name=seller_pick[:30],
                    line=dict(color=INDIGO, width=3, shape="hv"),
                    marker=dict(size=7)))
            fig.update_layout(**_layout()); time_axis(fig, start_d, end_d, "Units in stock")
            show(fig)

    # ── Competition + availability ────────────────────────────────────────────
    section("Competition & availability")
    c5, c6 = st.columns(2)
    with card("👥 Offers over time", "Sellers listing this ASIN, by fulfillment type.", c5):
        if trend.empty or trend[["fba_offers", "fbm_offers", "total_offers"]].dropna(how="all").empty:
            show(empty("No offer data in window — fills from the daily CSV."))
        else:
            fig = go.Figure()
            for col, nm, cl in [("fba_offers", "FBA", GREEN), ("fbm_offers", "FBM", RED),
                                 ("total_offers", "Total", MUTED)]:
                if col in trend and trend[col].notna().any():
                    fig.add_trace(go.Scatter(
                        x=trend["Date"], y=trend[col], mode="lines+markers", name=nm,
                        line=dict(color=cl, width=2.5, shape="spline", smoothing=0.4),
                        marker=dict(size=6)))
            fig.update_layout(**_layout()); time_axis(fig, start_d, end_d, "Sellers")
            show(fig)
    with card("🚫 90-day out-of-stock %", "How often the buy-box was unavailable. Higher = supply issues.", c6):
        if trend.empty or trend["oos"].dropna().empty:
            show(empty("No OOS data in window — fills from the daily CSV."))
        else:
            fig = go.Figure(go.Scatter(
                x=trend["Date"], y=trend["oos"], mode="lines+markers",
                line=dict(color=ORANGE, width=3, shape="spline", smoothing=0.4),
                marker=dict(size=6), fill="tozeroy", fillcolor="rgba(255,176,32,0.14)"))
            fig.update_layout(**_layout()); time_axis(fig, start_d, end_d, "OOS %")
            show(fig)

    # ── Sellers table ─────────────────────────────────────────────────────────
    section("Current sellers")
    with card("🏪 Sellers for this ASIN", "Most recent stock per seller — sorted by stock descending."):
        tbl = q("""WITH latest AS (SELECT seller_id, MAX(change_time) mt FROM fct_keepa_seller_history
                       WHERE asin=? AND stock IS NOT NULL GROUP BY seller_id)
                   SELECT COALESCE(s.seller_name, h.seller_id) "Seller",
                          CASE WHEN s.is_amazon=1 THEN '🛒 Amazon'
                               WHEN h.is_fba=1    THEN '🚚 FBA'
                               ELSE                    '🏠 FBM' END "Channel",
                          h.stock "Stock",
                          s.rating_pct "Rating %",
                          SUBSTR(h.change_time, 1, 10) "Last seen"
                   FROM fct_keepa_seller_history h
                   JOIN latest l ON l.seller_id=h.seller_id AND l.mt=h.change_time
                   LEFT JOIN dim_keepa_seller s ON s.seller_id=h.seller_id
                   WHERE h.asin=? ORDER BY h.stock DESC""",
                [asin, asin])
        if tbl.empty:
            st.caption("No sellers tracked yet.")
        else:
            st.dataframe(tbl, use_container_width=True, hide_index=True, height=300,
                         column_config={
                             "Stock": st.column_config.NumberColumn(format="%d"),
                             "Rating %": st.column_config.NumberColumn(format="%d%%"),
                         })


# ═════════════════════════════════════════════════════════════════════════════
# REPLENISHMENT
# ═════════════════════════════════════════════════════════════════════════════
else:
    st.markdown("### 🎯 Replenishment")
    try:
        import replenishment
        recs = replenishment.compute(get_conn())
    except Exception as e:
        st.error(f"Could not compute recommendations: {e}"); st.stop()
    if recs.empty:
        st.info("Not enough data yet — needs a few days of snapshots."); st.stop()
    counts = recs["recommendation"].value_counts().to_dict()
    html = "<div style='display:flex;gap:12px;margin-bottom:14px;flex-wrap:wrap'>"
    for lbl, key, cl in [("🚀 Ship now", "SHIP_NOW", GREEN), ("⏳ Hold", "HOLD", ORANGE),
                         ("🛑 Avoid", "AVOID_BUY", RED), ("⚠️ Watch", "WATCH", MUTED)]:
        html += f"<div class='kpi'><div class='l'>{lbl}</div><div class='v' style='color:{cl}'>{counts.get(key,0):,}</div></div>"
    st.markdown(html + "</div>", unsafe_allow_html=True)
    pick = st.multiselect("Show", ["SHIP_NOW", "HOLD", "AVOID_BUY", "WATCH"], default=["SHIP_NOW"])
    df = (recs[recs["recommendation"].isin(pick)] if pick else recs).sort_values("score", ascending=False)
    show_df = df[["asin", "title", "recommendation", "fba_stock", "velocity_used", "days_of_supply",
                  "bsr", "suggested_ship_qty"]].rename(columns={
        "asin": "ASIN", "title": "Title", "recommendation": "Call", "fba_stock": "FBA stock",
        "velocity_used": "Velocity/day", "days_of_supply": "Days supply", "bsr": "BSR",
        "suggested_ship_qty": "Ship qty"})
    st.dataframe(show_df, use_container_width=True, hide_index=True, height=560,
                 column_config={"Velocity/day": st.column_config.NumberColumn(format="%.2f"),
                                "Days supply": st.column_config.NumberColumn(format="%.0f"),
                                "BSR": st.column_config.NumberColumn(format="%d"),
                                "Ship qty": st.column_config.NumberColumn(format="%d")})
