"""Runs the whole Streamlit app offline with synthetic Yahoo data."""

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import data

APP = str(Path(__file__).resolve().parent.parent / "app.py")
TRADE_DATE = date(2026, 9, 25)


def fake_chain(spot: float, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frames = []
    for days in (0, 3, 7, 14, 28, 56, 112, 238):
        strikes = np.arange(round(spot * 0.5), round(spot * 1.5), 5.0)
        for side in ("call", "put"):
            dist = np.abs(strikes / spot - 1)
            iv = 0.30 + 0.4 * dist**2
            iv[rng.random(len(iv)) < 0.03] = 0.00001  # sprinkle in broken IVs like Yahoo does
            frames.append(pd.DataFrame({
                "type": side,
                "expiration": pd.Timestamp(TRADE_DATE + timedelta(days=days)),
                "strike": strikes,
                "volume": (rng.poisson(20000 * np.exp(-12 * dist) / (1 + days / 10)) + 0).astype(float),
                "openInterest": rng.integers(0, 50000, len(strikes)),
                "impliedVolatility": iv,
                "lastPrice": 1.0,
            }))
    return pd.concat(frames, ignore_index=True)


def fake_fetch_ticker(ticker):
    if ticker == "NOTREAL":
        raise data.DataFetchError("No price data found for 'NOTREAL'. Check the symbol is a valid US-listed ticker.")
    spot = {"AAPL": 255.4, "TSLA": 440.2, "NOOPT": 12.0}[ticker]
    chain = fake_chain(spot, len(ticker)) if ticker != "NOOPT" else fake_chain(spot, 0).iloc[0:0]
    return {
        "ticker": ticker,
        "stock": {"trade_date": TRADE_DATE, "last_close": spot, "volume": 50_000_000,
                  "avg_volume_20d": 55_000_000.0, "dividend_yield": 0.004 if ticker == "AAPL" else 0.0},
        "chain": chain,
        "failed_expirations": [],
        "fetched_at": datetime.now().isoformat(),
    }


@pytest.fixture(autouse=True)
def fresh_cache():
    st.cache_data.clear()


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "fetch_ticker", fake_fetch_ticker)
    monkeypatch.setattr(data, "fetch_risk_free_rate", lambda: (0.042, "test rate"))
    monkeypatch.setattr(data, "DB_PATH", tmp_path / "history.db")
    return tmp_path / "history.db"


def run_app(tickers: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    at.text_input[0].set_value(tickers).run()
    return at


def test_app_loads_and_saves_history(offline):
    at = run_app("AAPL, TSLA")
    assert not at.exception
    assert not at.error
    summary = at.dataframe[0].value
    assert list(summary["ticker"]) == ["AAPL", "TSLA"]
    assert (summary["delta_adj_shares"] > 0).all()

    hist = data.load_history(db_path=offline)
    assert sorted(hist["ticker"]) == ["AAPL", "TSLA"]
    assert set(hist["trade_date"]) == {TRADE_DATE.isoformat()}

    # Loading again the same day overwrites instead of duplicating.
    at.button[0].click().run()
    assert len(data.load_history(db_path=offline)) == 2


def test_bad_ticker_and_no_options_are_friendly(offline):
    at = run_app("AAPL, NOTREAL, NOOPT")
    assert not at.exception
    assert len(at.error) == 1 and "NOTREAL" in at.error[0].value
    assert list(at.dataframe[0].value["ticker"]) == ["AAPL", "NOOPT"]


def test_network_failure_is_friendly(monkeypatch, tmp_path):
    def offline_fetch(ticker):
        raise data.DataFetchError(data.friendly_error(ConnectionError("Failed to perform, curl: (7)"), ticker))
    monkeypatch.setattr(data, "fetch_ticker", offline_fetch)
    monkeypatch.setattr(data, "fetch_risk_free_rate", lambda: (0.04, "fallback"))
    monkeypatch.setattr(data, "DB_PATH", tmp_path / "history.db")
    at = run_app("AAPL")
    assert not at.exception
    assert "Couldn't reach Yahoo Finance" in at.error[0].value
