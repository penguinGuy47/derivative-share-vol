"""Stock vs. options volume dashboard.

Run with:  streamlit run app.py
"""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import calculations as calc
import data

st.set_page_config(page_title="Stock vs Options Volume", page_icon="📊", layout="wide")

CACHE_SECONDS = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Colors (validated for color-blind separation in light and dark mode)
# ---------------------------------------------------------------------------

def _is_dark() -> bool:
    try:
        return st.context.theme.type == "dark"
    except Exception:
        return False


DARK = _is_dark()
COLORS = {
    "stock": "#3987e5" if DARK else "#2a78d6",    # blue
    "options": "#d95926" if DARK else "#eb6834",  # orange
    "call": "#199e70" if DARK else "#1baf7a",     # aqua
    "put": "#9085e9" if DARK else "#4a3aa7",      # violet
    "spot": "#c3c2b7" if DARK else "#52514e",     # neutral ink for reference line
    "gap": "#0e1117" if DARK else "#ffffff",      # page background, used as bar separator
}


def style_fig(fig: go.Figure, height: int = 380) -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=10, r=10, t=90, b=10),
        title=dict(y=0.97, yanchor="top", x=0, xanchor="left"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, traceorder="normal"),
        hoverlabel=dict(namelength=-1),
    )
    fig.update_traces(marker_line_color=COLORS["gap"], marker_line_width=1, selector=dict(type="bar"))
    return fig


# ---------------------------------------------------------------------------
# Cached data loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def load_ticker(ticker: str) -> dict:
    # Errors are returned (not raised) so they're cached too - that way a bad
    # ticker or a rate limit doesn't trigger a new request on every click.
    try:
        return data.fetch_ticker(ticker)
    except data.DataFetchError as exc:
        return {"ticker": ticker, "error": str(exc)}
    except Exception as exc:  # belt and braces: never crash the page
        return {"ticker": ticker, "error": data.friendly_error(exc, ticker)}


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def load_rate() -> tuple[float, str]:
    return data.fetch_risk_free_rate()


COLUMN_CONFIG = {
    "ticker": st.column_config.TextColumn("Ticker"),
    "trade_date": st.column_config.TextColumn("Session"),
    "last_close": st.column_config.NumberColumn("Last price", format="$%.2f"),
    "stock_volume": st.column_config.NumberColumn("Stock volume", format="localized"),
    "avg_volume_20d": st.column_config.NumberColumn("20-day avg volume", format="localized"),
    "total_contracts": st.column_config.NumberColumn("Options contracts", format="localized"),
    "call_volume": st.column_config.NumberColumn("Call contracts", format="localized"),
    "put_volume": st.column_config.NumberColumn("Put contracts", format="localized"),
    "put_call_ratio": st.column_config.NumberColumn("Put/Call", format="%.2f"),
    "notional_shares": st.column_config.NumberColumn(
        "Notional shares", format="localized", help="Contracts × 100"),
    "delta_adj_shares": st.column_config.NumberColumn(
        "Delta-adj. shares", format="localized",
        help="Σ volume × 100 × |delta| — shares needed to hedge the options flow"),
    "combined_volume": st.column_config.NumberColumn(
        "Combined volume", format="localized", help="Stock volume + delta-adjusted shares"),
    "options_to_stock_ratio": st.column_config.NumberColumn(
        "Options/Stock", format="%.2f×", help="Delta-adjusted shares ÷ stock volume"),
    "pct_from_options": st.column_config.NumberColumn(
        "% from options", format="%.1f%%", help="Share of combined volume from options"),
    "contracts_skipped": st.column_config.NumberColumn(
        "Skipped (bad IV)", format="localized",
        help="Contracts with missing or implausible implied volatility (<1% or >500%); "
             "they count toward contract totals but not the delta-adjusted total"),
}


# ---------------------------------------------------------------------------
# Header and inputs
# ---------------------------------------------------------------------------

st.title("Stock vs. Options Volume")
st.caption(
    "How much of a stock's trading activity is really happening through options? "
    "Options volume is converted to a **delta-adjusted share equivalent**: roughly how many "
    "shares market makers would need to trade to hedge that day's options flow."
)

st.info(
    "**About this data.** Prices and volumes come free from Yahoo Finance and are **delayed** "
    "(typically 15+ minutes). Everything reflects the **most recent trading session**, so on "
    "weekends and market holidays you are looking at the last session (e.g. Friday). "
    "Only listed options on each ticker itself are counted. This does **not** include OTC swaps, "
    "index options (such as SPX, or options on index products like QQQ when you're looking at a "
    "single stock), or hedging done by leveraged ETFs. Delta-adjusted figures are estimates.",
    icon="ℹ️",
)

col_input, col_button = st.columns([5, 1], vertical_alignment="bottom")
with col_input:
    ticker_text = st.text_input(
        "Tickers (separate with commas)",
        value="AAPL, TSLA, NVDA, AMC",
        help="US-listed stock or ETF symbols, e.g. AAPL, TSLA, NVDA, AMC",
    )
with col_button:
    if st.button("🔄 Refresh data", width="stretch"):
        load_ticker.clear()
        load_rate.clear()

tickers = data.parse_tickers(ticker_text)
if not tickers:
    st.warning("Type at least one ticker symbol above to get started.")
    st.stop()


# ---------------------------------------------------------------------------
# Load, compute, and save snapshots
# ---------------------------------------------------------------------------

rate, rate_note = load_rate()

results: dict[str, dict] = {}
errors: dict[str, str] = {}
notes: list[str] = []

if "saved_snapshots" not in st.session_state:
    st.session_state.saved_snapshots = set()

progress = st.progress(0.0, text="Loading…")
for i, ticker in enumerate(tickers):
    progress.progress(i / len(tickers), text=f"Loading {ticker} (stock + every option expiration)…")
    raw = load_ticker(ticker)
    if "error" in raw:
        errors[ticker] = raw["error"]
        continue

    stock = raw["stock"]
    try:
        contracts = calc.enrich_chain(
            raw["chain"], stock["last_close"], stock["trade_date"], rate, stock["dividend_yield"]
        )
        summary = calc.summarize(ticker, stock, contracts)
    except Exception as exc:
        errors[ticker] = f"Couldn't crunch the numbers for {ticker}: {exc}"
        continue

    results[ticker] = {"stock": stock, "contracts": contracts, "summary": summary,
                       "fetched_at": raw["fetched_at"]}

    if raw["chain"].empty:
        notes.append(f"**{ticker}** has no listed options on Yahoo, so its options volume is zero.")
    if raw["failed_expirations"]:
        notes.append(
            f"**{ticker}**: {len(raw['failed_expirations'])} expiration(s) couldn't be downloaded "
            f"and are left out ({', '.join(raw['failed_expirations'][:5])}"
            f"{'…' if len(raw['failed_expirations']) > 5 else ''})."
        )

    # Save one snapshot per fresh download (re-saves on the same trading day overwrite).
    key = (ticker, raw["fetched_at"])
    if key not in st.session_state.saved_snapshots:
        try:
            data.save_snapshot(summary)
            st.session_state.saved_snapshots.add(key)
        except Exception as exc:
            notes.append(f"Couldn't save {ticker} to the history database: {exc}")
progress.empty()

for ticker, msg in errors.items():
    st.error(f"**{ticker}**: {msg}", icon="⚠️")

if results:
    sessions = sorted({r["stock"]["trade_date"] for r in results.values()})
    downloaded = min(datetime.fromisoformat(r["fetched_at"]) for r in results.values())
    now_et = datetime.now(calc.MARKET_TZ)
    session_text = " / ".join(f"{d:%A, %b} {d.day}, {d.year}" for d in sessions)
    status = (
        f"📅 Showing the trading session of **{session_text}** "
        f"(downloaded at {downloaded:%I:%M %p} your time; press Refresh for newer data)."
    )
    if sessions[-1] < now_et.date():
        if now_et.weekday() >= 5:
            status += " The market is closed for the weekend."
        elif now_et.time() < time(9, 30):
            status += " Today's session opens at 9:30 AM Eastern. Refresh after the open to see it."
        else:
            status += (" Yahoo hasn't published today's session yet, or the market is closed today "
                       "for a holiday.")
    st.markdown(status)


tab_today, tab_history = st.tabs(["Latest session", "History"])

# ---------------------------------------------------------------------------
# Latest session tab
# ---------------------------------------------------------------------------

with tab_today:
    if not results:
        st.warning("No data loaded. Check the messages above, then press **Refresh data**.")
    else:
        summaries = pd.DataFrame([r["summary"] for r in results.values()])

        st.subheader("Summary")
        table = summaries[[
            "ticker", "stock_volume", "total_contracts", "delta_adj_shares", "combined_volume",
            "options_to_stock_ratio", "put_call_ratio", "pct_from_options",
            "call_volume", "put_volume", "notional_shares", "avg_volume_20d",
            "last_close", "trade_date", "contracts_skipped",
        ]].copy()
        table[["delta_adj_shares", "combined_volume"]] = table[["delta_adj_shares", "combined_volume"]].round()
        st.dataframe(
            table,
            hide_index=True,
            column_config=COLUMN_CONFIG,
        )
        st.caption("Click a column header to sort.")

        skipped = summaries[summaries["contracts_skipped"] > 0]
        with st.expander("Assumptions and data-quality notes", expanded=bool(notes)):
            st.markdown(f"- Risk-free rate: **{rate:.2%}** ({rate_note}).")
            dy = ", ".join(
                f"{t} {r['stock']['dividend_yield']:.2%}" for t, r in results.items()
            )
            st.markdown(f"- Dividend yield (trailing 12 months, 0 if none): {dy}.")
            if skipped.empty:
                st.markdown("- No contracts were skipped for bad implied volatility.")
            for _, row in skipped.iterrows():
                st.markdown(
                    f"- **{row['ticker']}**: {row['contracts_skipped']:,} of "
                    f"{row['contracts_listed']:,} listed contracts skipped for missing/implausible "
                    f"IV, carrying {row['skipped_volume']:,} contracts of volume that are left "
                    f"out of the delta-adjusted total."
                )
            for n in notes:
                st.markdown(f"- {n}")

        # --- Cross-ticker charts --------------------------------------------
        c1, c2 = st.columns(2)
        with c1:
            fig = go.Figure()
            fig.add_bar(
                name="Stock volume", x=summaries["ticker"], y=summaries["stock_volume"],
                marker_color=COLORS["stock"],
                hovertemplate="%{x}<br>Stock volume: %{y:,.0f} shares<extra></extra>",
            )
            fig.add_bar(
                name="Delta-adjusted options volume", x=summaries["ticker"],
                y=summaries["delta_adj_shares"], marker_color=COLORS["options"],
                customdata=summaries["pct_from_options"],
                hovertemplate="%{x}<br>Delta-adjusted options: %{y:,.0f} shares"
                              "<br>%{customdata:.1f}% of combined<extra></extra>",
            )
            # Label the combined total above each stack.
            for t, total in zip(summaries["ticker"], summaries["combined_volume"]):
                fig.add_annotation(x=t, y=total, text=f"{total:,.0f}", showarrow=False, yshift=10,
                                   font=dict(size=11))
            fig.update_layout(barmode="stack", title="Combined volume: stock + options (shares)",
                              yaxis_title="Shares")
            st.plotly_chart(style_fig(fig))

        with c2:
            ratio = summaries.sort_values("options_to_stock_ratio", ascending=False)
            fig = go.Figure()
            fig.add_bar(
                x=ratio["ticker"], y=ratio["options_to_stock_ratio"], marker_color=COLORS["options"],
                text=ratio["options_to_stock_ratio"].map(lambda v: "—" if pd.isna(v) else f"{v:.2f}×"),
                textposition="outside",
                hovertemplate="%{x}<br>Options/Stock: %{y:.2f}×<extra></extra>",
                showlegend=False,
            )
            fig.add_hline(y=1, line_dash="dot", line_color=COLORS["spot"],
                          annotation_text="1× = equal", annotation_position="top right")
            fig.update_layout(title="Options-to-stock ratio (delta-adjusted ÷ stock volume)",
                              yaxis_title="Ratio")
            st.plotly_chart(style_fig(fig))

        # --- Single-ticker drill-down ---------------------------------------
        st.subheader("Drill down")
        d1, d2 = st.columns([1, 2])
        with d1:
            chosen = st.selectbox("Ticker", list(results.keys()))
        with d2:
            measure = st.radio(
                "Measure", ["Contracts", "Delta-adjusted shares"], horizontal=True,
                help="Raw contract counts, or contracts × 100 × |delta|",
            )
        col = "contracts" if measure == "Contracts" else "delta_adj_shares"
        unit = "contracts" if col == "contracts" else "shares"

        res = results[chosen]
        contracts = res["contracts"]
        spot = res["stock"]["last_close"]

        if contracts.empty:
            st.info(f"{chosen} has no listed options, so there's nothing to break down.")
        else:
            b1, b2 = st.columns(2)
            for container, by, title in (
                (b1, "expiry_bucket", "by expiration"),
                (b2, "moneyness", "by moneyness"),
            ):
                bd = calc.breakdown(contracts, by)
                fig = go.Figure()
                for side, label in (("call", "Calls"), ("put", "Puts")):
                    part = bd[bd["type"] == side]
                    fig.add_bar(
                        name=label, x=part[by], y=part[col], marker_color=COLORS[side],
                        hovertemplate=f"%{{x}}<br>{label}: %{{y:,.0f}} {unit}<extra></extra>",
                    )
                fig.update_layout(barmode="group", title=f"{chosen}: calls vs puts {title}",
                                  yaxis_title=measure)
                with container:
                    st.plotly_chart(style_fig(fig, 340))

            strikes = calc.by_strike(contracts)
            fig = go.Figure()
            for side, label in (("call", "Calls"), ("put", "Puts")):
                part = strikes[strikes["type"] == side]
                fig.add_bar(
                    name=label, x=part["strike"], y=part["delta_adj_shares"],
                    marker_color=COLORS[side], customdata=part["contracts"],
                    hovertemplate=f"Strike $%{{x:,.2f}}<br>{label}: %{{y:,.0f}} delta-adj. shares"
                                  f"<br>%{{customdata:,}} contracts<extra></extra>",
                )
            fig.add_vline(x=spot, line_dash="dash", line_color=COLORS["spot"],
                          annotation_text=f"Stock ${spot:,.2f}", annotation_position="top")
            fig.update_layout(
                barmode="stack", bargap=0.1,
                title=f"{chosen}: delta-adjusted volume by strike (all expirations)",
                xaxis_title="Strike ($)", yaxis_title="Delta-adjusted shares",
                # Start zoomed to ±40% around the stock price; double-click the chart to see all strikes.
                xaxis_range=[spot * 0.6, spot * 1.4],
            )
            st.plotly_chart(style_fig(fig, 420))
            st.caption("Zoomed to strikes within ±40% of the stock price. Double-click the chart to show every strike.")

            with st.expander(f"{chosen} breakdown tables"):
                t1, t2 = st.columns(2)
                for container, by, label in ((t1, "expiry_bucket", "Expiration"), (t2, "moneyness", "Moneyness")):
                    bd = calc.breakdown(contracts, by).pivot(index=by, columns="type")
                    bd.columns = [f"{'Call' if t == 'call' else 'Put'} {m.replace('_', ' ')}" for m, t in bd.columns]
                    bd = bd.reindex(calc.EXPIRY_BUCKETS if by == "expiry_bucket" else calc.MONEYNESS_BUCKETS)
                    bd.index.name = label
                    with container:
                        st.dataframe(bd.style.format("{:,.0f}"))


# ---------------------------------------------------------------------------
# History tab
# ---------------------------------------------------------------------------

with tab_history:
    st.caption(
        "Yahoo doesn't offer historical options volume for free, so a snapshot of each ticker's "
        "numbers is saved to a local database (history.db) every time you load data - one row per "
        "ticker per trading day. The history grows the more you use the dashboard."
    )
    try:
        saved = data.history_tickers()
    except Exception as exc:
        saved = []
        st.error(f"Couldn't open the history database: {exc}")

    if not saved:
        st.info("No history saved yet. Load some tickers and it will start building up.")
    else:
        default = saved.index(tickers[0]) if tickers and tickers[0] in saved else 0
        h_ticker = st.selectbox("Ticker", saved, index=default, key="history_ticker")
        hist = data.load_history(h_ticker)
        hist["trade_date"] = pd.to_datetime(hist["trade_date"])

        fig = go.Figure()
        fig.add_scatter(
            name="Stock volume", x=hist["trade_date"], y=hist["stock_volume"], mode="lines+markers",
            line=dict(color=COLORS["stock"], width=2), marker=dict(size=8),
            hovertemplate="%{x|%b %d, %Y}<br>Stock volume: %{y:,.0f}<extra></extra>",
        )
        fig.add_scatter(
            name="Delta-adjusted options volume", x=hist["trade_date"], y=hist["delta_adj_shares"],
            mode="lines+markers", line=dict(color=COLORS["options"], width=2), marker=dict(size=8),
            hovertemplate="%{x|%b %d, %Y}<br>Delta-adj. options: %{y:,.0f}<extra></extra>",
        )
        fig.update_layout(title=f"{h_ticker}: stock vs delta-adjusted options volume over time",
                          yaxis_title="Shares", hovermode="x unified")
        fig.update_yaxes(rangemode="tozero")
        fig.update_xaxes(tickformat="%b %d\n%Y")
        if hist["trade_date"].max() - hist["trade_date"].min() < pd.Timedelta(days=14):
            fig.update_xaxes(dtick=86_400_000)  # one tick per day while the history is short
        st.plotly_chart(style_fig(fig, 420))
        if len(hist) == 1:
            st.caption("Only one day saved so far - come back on other trading days to see a trend.")

        shown = hist.drop(columns=["saved_at"]).sort_values("trade_date", ascending=False)
        shown[["delta_adj_shares", "combined_volume"]] = shown[["delta_adj_shares", "combined_volume"]].round()
        st.dataframe(
            shown,
            hide_index=True,
            column_config={**COLUMN_CONFIG, "trade_date": st.column_config.DateColumn("Session")},
        )

        e1, e2 = st.columns(2)
        with e1:
            st.download_button(
                f"⬇️ Download {h_ticker} history (CSV)",
                data.load_history(h_ticker).to_csv(index=False),
                file_name=f"{h_ticker}_options_volume_history.csv", mime="text/csv",
            )
        with e2:
            st.download_button(
                "⬇️ Download all tickers (CSV)",
                data.load_history().to_csv(index=False),
                file_name="options_volume_history_all.csv", mime="text/csv",
            )
