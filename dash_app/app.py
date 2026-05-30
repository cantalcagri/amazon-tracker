"""
Amazon Tracker — Plotly Dash analytics app (DuckDB-backed).

Data flows: SQLite (written by the unchanged pipeline) ← DuckDB attaches it
READ-ONLY ← this Dash app queries DuckDB. Zero ETL.

Run:
    DB_PATH=pipeline/amazon_tracker.db python3 dash_app/app.py
    # then open http://127.0.0.1:8050
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import plotly.graph_objects as go
import plotly.express as px
from dash import Dash, dcc, html, Input, Output

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402

# ── Theme ───────────────────────────────────────────────────────────────────
BG = "#0e1117"
CARD = "#161a23"
ACCENT = "#6366f1"
GREEN = "#10b981"
ORANGE = "#f97316"
TEXT = "#e6e6e6"
MUTED = "#9aa0aa"

PLOT_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color=TEXT, size=12), margin=dict(l=10, r=10, t=10, b=10),
    height=300, hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5),
)
GRID = "rgba(127,127,127,0.14)"


def _empty(msg: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(**PLOT_LAYOUT)
    fig.add_annotation(text=msg, showarrow=False, font=dict(color=MUTED, size=13))
    fig.update_xaxes(visible=False); fig.update_yaxes(visible=False)
    return fig


def _axes(fig: go.Figure, y_title: str = "") -> go.Figure:
    fig.update_xaxes(gridcolor="rgba(127,127,127,0.07)", title=None)
    fig.update_yaxes(gridcolor=GRID, title=y_title)
    return fig


# ── Reusable styled blocks ──────────────────────────────────────────────────
def card(children, **style):
    base = dict(background=CARD, borderRadius="12px", padding="14px 16px",
                border="1px solid rgba(127,127,127,0.16)")
    base.update(style)
    return html.Div(children, style=base)


def kpi(label: str, value: str):
    return card([
        html.Div(label, style=dict(fontSize="0.72rem", letterSpacing="0.05em",
                                    textTransform="uppercase", color=MUTED, fontWeight=600)),
        html.Div(value, style=dict(fontSize="1.7rem", fontWeight=700, marginTop="4px")),
    ], flex="1")


def chart_block(title: str, graph_id: str, caption_id: str | None = None):
    head = [html.Div(title, style=dict(fontWeight=700, fontSize="0.95rem"))]
    if caption_id:
        head.append(html.Div(id=caption_id, style=dict(fontSize="0.76rem", color=MUTED, marginBottom="4px")))
    return card(head + [dcc.Graph(id=graph_id, config={"displayModeBar": False})])


# ── App ─────────────────────────────────────────────────────────────────────
app = Dash(__name__, title="Amazon Tracker — DuckDB")
server = app.server

_brands = data.brands()
brand_opts = [{"label": "🌐 All brands", "value": "__all__"}] + [
    {"label": f"{r.brand} ({r.asin_count})", "value": r.brand}
    for r in _brands.itertuples()
]

app.layout = html.Div(style=dict(background=BG, color=TEXT, minHeight="100vh",
                                  fontFamily="-apple-system, system-ui, sans-serif",
                                  padding="18px 28px"), children=[
    # Header
    html.Div(style=dict(display="flex", alignItems="baseline", gap="12px"), children=[
        html.H1("📦 Amazon Tracker", style=dict(margin=0, fontWeight=800, fontSize="1.6rem")),
        html.Span("⚡ DuckDB over SQLite · zero-ETL", style=dict(
            color=ACCENT, fontSize="0.78rem", fontWeight=700,
            border=f"1px solid {ACCENT}", borderRadius="999px", padding="2px 10px")),
    ]),

    # Controls
    html.Div(style=dict(display="flex", gap="14px", margin="16px 0", alignItems="end",
                        flexWrap="wrap"), children=[
        html.Div(style=dict(minWidth="220px"), children=[
            html.Label("Brand", style=dict(fontSize="0.75rem", color=MUTED)),
            dcc.Dropdown(id="brand", options=brand_opts, value="__all__", clearable=False,
                         style=dict(color="#111")),
        ]),
        html.Div(style=dict(minWidth="360px", flex="1"), children=[
            html.Label("ASIN", style=dict(fontSize="0.75rem", color=MUTED)),
            dcc.Dropdown(id="asin", clearable=False, style=dict(color="#111")),
        ]),
        html.Div(style=dict(minWidth="260px"), children=[
            html.Label("Days of history", style=dict(fontSize="0.75rem", color=MUTED)),
            dcc.Slider(id="days", min=7, max=90, step=None,
                       marks={7: "7", 30: "30", 60: "60", 90: "90"}, value=90),
        ]),
    ]),

    # Title + KPIs
    html.Div(id="prod-title", style=dict(fontSize="1.05rem", fontWeight=700, margin="6px 0 10px")),
    html.Div(id="kpis", style=dict(display="flex", gap="12px", marginBottom="16px")),

    # Charts grid
    html.Div(style=dict(display="grid", gridTemplateColumns="1fr 1fr", gap="14px"), children=[
        chart_block("📉 Best Sellers Rank", "fig-bsr"),
        chart_block("💵 Prices by channel", "fig-price"),
        chart_block("📈 Daily units sold", "fig-sales", "cap-sales"),
        chart_block("📦 Per-seller stock", "fig-sellers"),
    ]),

    html.Div("Pipeline unchanged — this app is a read-only DuckDB analytical layer.",
             style=dict(color=MUTED, fontSize="0.74rem", marginTop="18px")),
])


# ── Callbacks ───────────────────────────────────────────────────────────────
@app.callback(
    Output("asin", "options"), Output("asin", "value"),
    Input("brand", "value"),
)
def _fill_asins(brand):
    df = data.asins_for_brand(brand)
    opts = []
    for r in df.itertuples():
        flag = f"🏪{int(r.sellers)}" if r.sellers else "⏳"
        title = (r.title or r.asin)[:48]
        opts.append({"label": f"{flag}  {r.asin} — {title}", "value": r.asin})
    value = df.iloc[0]["asin"] if not df.empty else None
    return opts, value


@app.callback(
    Output("prod-title", "children"),
    Output("kpis", "children"),
    Output("fig-bsr", "figure"),
    Output("fig-price", "figure"),
    Output("fig-sales", "figure"),
    Output("cap-sales", "children"),
    Output("fig-sellers", "figure"),
    Input("asin", "value"), Input("days", "value"),
)
def _render(asin, days):
    if not asin:
        e = _empty("Select an ASIN")
        return "", [], e, e, e, "", e

    prod = data.product(asin)
    title = f"{prod.get('brand') or ''} — {prod.get('title') or asin}".strip(" —")

    k = data.latest_kpis(asin)
    def fmt(v, money=False):
        if v is None or (isinstance(v, float) and v != v):
            return "—"
        return f"${v:,.2f}" if money else f"{int(v):,}"
    kpis = [
        kpi("BSR", fmt(k.get("bsr"))),
        kpi("Buy-box $", fmt(k.get("bb_price"), money=True)),
        kpi("FBA stock", fmt(k.get("fba_stock"))),
        kpi("Total offers", fmt(k.get("total_offers"))),
        kpi("FBA sellers", fmt(k.get("fba_offers"))),
    ]

    t = data.trend(asin, days)
    # BSR
    if t.empty or t["bsr"].dropna().empty:
        f_bsr = _empty("No BSR data")
    else:
        f_bsr = go.Figure(go.Scatter(x=t["Date"], y=t["bsr"], mode="lines+markers",
                                     line=dict(color=ACCENT, width=2.5)))
        f_bsr.update_layout(**PLOT_LAYOUT); _axes(f_bsr, "BSR")
        f_bsr.update_yaxes(autorange="reversed")  # lower rank = better

    # Prices
    if t.empty:
        f_price = _empty("No price data")
    else:
        f_price = go.Figure()
        for col, name, color in [("bb_price", "Buy-Box", ACCENT),
                                 ("fba_price", "FBA", GREEN),
                                 ("fbm_price", "FBM", "#f43f5e")]:
            if col in t and t[col].notna().any():
                f_price.add_trace(go.Scatter(x=t["Date"], y=t[col], mode="lines",
                                             name=name, line=dict(color=color, width=2)))
        f_price.update_layout(**PLOT_LAYOUT); _axes(f_price, "Price ($)")

    # Daily units sold (canonical)
    ds, source = data.daily_sales(asin, days)
    if ds.empty:
        f_sales = _empty("No sales signal in window")
        cap = ""
    else:
        f_sales = go.Figure(go.Bar(x=ds["Date"], y=ds["units_sold"], name="Sold",
                                   marker_color=GREEN))
        if source == "per-seller" and ds["units_restocked"].fillna(0).sum() > 0:
            f_sales.add_trace(go.Scatter(x=ds["Date"], y=ds["units_restocked"],
                                         name="Restocked", mode="lines+markers",
                                         line=dict(color=ORANGE, width=2, dash="dot")))
        f_sales.update_layout(**PLOT_LAYOUT); _axes(f_sales, "Units")
        total = int(ds["units_sold"].fillna(0).sum())
        cap = (f"Source: {source} · {total:,} units in {days}d"
               + (" · restocks excluded" if source == "per-seller"
                  else " · FBA-delta estimate (lower bound)"))

    # Per-seller stock
    ss = data.seller_stock(asin, days)
    if ss.empty:
        f_sellers = _empty("No per-seller data for this ASIN yet")
    else:
        f_sellers = px.line(ss, x="Date", y="stock", color="seller",
                            line_shape="hv")
        f_sellers.update_layout(**PLOT_LAYOUT, showlegend=False)
        _axes(f_sellers, "Units in stock")

    return title, kpis, f_bsr, f_price, f_sales, cap, f_sellers


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8050))
    app.run(host="127.0.0.1", port=port, debug=False)
