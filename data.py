"""Everything that talks to the outside world: Yahoo Finance (via yfinance)
and the local SQLite history database.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError

DB_PATH = Path(__file__).resolve().parent / "history.db"

# Used only if the ^IRX (13-week T-bill) lookup fails.
FALLBACK_RISK_FREE_RATE = 0.04

# Short pause between option-chain requests so we're gentle with Yahoo.
PAUSE_BETWEEN_EXPIRATIONS = 0.15

CHAIN_COLUMNS = ["strike", "volume", "openInterest", "impliedVolatility", "lastPrice"]


class DataFetchError(Exception):
    """A problem worth showing to the user as a plain-English message."""


def friendly_error(exc: Exception, ticker: str | None = None) -> str:
    what = f"data for {ticker}" if ticker else "data"
    text = str(exc)
    if isinstance(exc, DataFetchError):
        return text
    if isinstance(exc, YFRateLimitError) or "Too Many Requests" in text or "rate limit" in text.lower():
        return (
            f"Yahoo Finance is rate-limiting requests right now, so {what} couldn't be loaded. "
            "Wait a few minutes and press Refresh (loading fewer tickers at once also helps)."
        )
    network_words = ("Failed to perform", "Could not resolve", "timed out", "Timeout",
                     "Connection", "SSL", "proxy", "Network")
    if isinstance(exc, (OSError, TimeoutError)) or any(w in text or w in type(exc).__name__ for w in network_words):
        return (
            f"Couldn't reach Yahoo Finance to load {what}. "
            "Check your internet connection and press Refresh."
        )
    return f"Something went wrong loading {what}: {text or type(exc).__name__}"


# ---------------------------------------------------------------------------
# Yahoo Finance
# ---------------------------------------------------------------------------


def fetch_risk_free_rate() -> tuple[float, str]:
    """13-week T-bill yield from ^IRX as a decimal, plus a note on its source."""
    try:
        hist = yf.Ticker("^IRX").history(period="1mo", raise_errors=True)
        closes = hist["Close"].dropna()
        if not closes.empty:
            return float(closes.iloc[-1]) / 100.0, f"13-week T-bill (^IRX) on {closes.index[-1].date()}"
    except Exception:
        pass
    return FALLBACK_RISK_FREE_RATE, (
        f"^IRX unavailable, using a fallback of {FALLBACK_RISK_FREE_RATE:.2%}"
    )


def fetch_stock(tk: yf.Ticker, ticker: str) -> dict:
    """Last close, latest session volume, 20-day average volume, dividend yield."""
    try:
        hist = tk.history(period="1y", auto_adjust=False, raise_errors=True)
    except Exception as exc:
        # yfinance reports unknown symbols as "possibly delisted" / missing.
        if "delisted" in str(exc).lower() or "no data" in str(exc).lower() or "YFPricesMissing" in type(exc).__name__ \
                or "YFTickerMissing" in type(exc).__name__ or "YFTzMissing" in type(exc).__name__:
            raise DataFetchError(
                f"No price data found for '{ticker}'. Check the symbol is a valid US-listed ticker."
            ) from exc
        raise
    hist = hist.dropna(subset=["Close"])
    # Before the open Yahoo can add a zero-volume row for today; use the last real session.
    traded = hist[hist["Volume"] > 0]
    if not traded.empty:
        hist = traded
    if hist.empty:
        raise DataFetchError(
            f"No price data found for '{ticker}'. Check the symbol is a valid US-listed ticker."
        )

    last = hist.iloc[-1]
    last_close = float(last["Close"])

    # Dividend yield = dividends paid over the last 12 months / price.
    div_yield = 0.0
    if "Dividends" in hist.columns:
        cutoff = hist.index[-1] - pd.Timedelta(days=365)
        paid = float(hist.loc[hist.index > cutoff, "Dividends"].sum())
        if paid > 0 and last_close > 0:
            div_yield = paid / last_close

    return {
        "trade_date": hist.index[-1].date(),
        "last_close": last_close,
        "volume": int(last["Volume"]),
        "avg_volume_20d": float(hist["Volume"].tail(20).mean()),
        "dividend_yield": div_yield,
    }


def fetch_option_chain(tk: yf.Ticker) -> tuple[pd.DataFrame, list[str]]:
    """Every contract across every listed expiration.

    Returns (contracts, expirations_that_failed).
    """
    expirations = list(tk.options or [])
    frames, failed = [], []
    for i, exp in enumerate(expirations):
        if i:
            time.sleep(PAUSE_BETWEEN_EXPIRATIONS)
        try:
            chain = tk.option_chain(exp)
        except YFRateLimitError:
            raise
        except Exception:
            failed.append(exp)
            continue
        for side, df in (("call", chain.calls), ("put", chain.puts)):
            if df is None or df.empty:
                continue
            part = df.reindex(columns=CHAIN_COLUMNS).copy()
            part.insert(0, "type", side)
            part.insert(1, "expiration", pd.Timestamp(exp))
            frames.append(part)

    if not frames:
        return pd.DataFrame(columns=["type", "expiration", *CHAIN_COLUMNS]), failed
    return pd.concat(frames, ignore_index=True), failed


def fetch_ticker(ticker: str) -> dict:
    """Stock stats plus the full option chain for one ticker.

    Raises DataFetchError with a friendly message on any failure.
    """
    try:
        tk = yf.Ticker(ticker)
        stock = fetch_stock(tk, ticker)
        chain, failed = fetch_option_chain(tk)
    except Exception as exc:
        raise DataFetchError(friendly_error(exc, ticker)) from exc
    return {
        "ticker": ticker,
        "stock": stock,
        "chain": chain,
        "failed_expirations": failed,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def parse_tickers(text: str) -> list[str]:
    """'aapl, tsla  nvda' -> ['AAPL', 'TSLA', 'NVDA'] (deduplicated, order kept)."""
    seen = []
    for raw in text.replace(";", ",").replace(" ", ",").split(","):
        sym = raw.strip().upper()
        if sym and sym not in seen:
            seen.append(sym)
    return seen


# ---------------------------------------------------------------------------
# SQLite history
# ---------------------------------------------------------------------------

HISTORY_COLUMNS = [
    "ticker", "trade_date", "last_close", "stock_volume", "avg_volume_20d",
    "call_volume", "put_volume", "total_contracts", "put_call_ratio",
    "notional_shares", "delta_adj_shares", "combined_volume",
    "options_to_stock_ratio", "pct_from_options", "contracts_skipped",
]


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            ticker TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            last_close REAL,
            stock_volume INTEGER,
            avg_volume_20d REAL,
            call_volume INTEGER,
            put_volume INTEGER,
            total_contracts INTEGER,
            put_call_ratio REAL,
            notional_shares INTEGER,
            delta_adj_shares REAL,
            combined_volume REAL,
            options_to_stock_ratio REAL,
            pct_from_options REAL,
            contracts_skipped INTEGER,
            saved_at TEXT,
            PRIMARY KEY (ticker, trade_date)
        )
        """
    )
    return conn


def save_snapshot(summary: dict, db_path: Path | None = None) -> None:
    """Store one ticker's summary for its trading date, replacing any earlier
    snapshot from the same day."""
    row = {c: summary.get(c) for c in HISTORY_COLUMNS}
    row["saved_at"] = datetime.now().isoformat(timespec="seconds")
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with _connect(db_path) as conn:
        conn.execute(f"INSERT OR REPLACE INTO snapshots ({cols}) VALUES ({marks})", list(row.values()))
    conn.close()


def load_history(ticker: str | None = None, db_path: Path | None = None) -> pd.DataFrame:
    conn = _connect(db_path)
    try:
        if ticker:
            df = pd.read_sql_query(
                "SELECT * FROM snapshots WHERE ticker = ? ORDER BY trade_date", conn, params=[ticker]
            )
        else:
            df = pd.read_sql_query("SELECT * FROM snapshots ORDER BY ticker, trade_date", conn)
    finally:
        conn.close()
    return df


def history_tickers(db_path: Path | None = None) -> list[str]:
    conn = _connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM snapshots ORDER BY ticker")]
    finally:
        conn.close()
