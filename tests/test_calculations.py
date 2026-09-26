import math
from datetime import date, datetime

import pandas as pd
import pytest

import calculations as calc


# --- Black-Scholes delta vs textbook values ---------------------------------

def test_delta_matches_hull_example():
    # Hull, "Options, Futures, and Other Derivatives": S=42, K=40, r=10%,
    # sigma=20%, T=0.5 -> d1 = 0.7693, N(d1) = 0.7791.
    call = calc.bs_delta(42, 40, 0.5, 0.10, 0.0, 0.20, "call")
    put = calc.bs_delta(42, 40, 0.5, 0.10, 0.0, 0.20, "put")
    assert call == pytest.approx(0.7791, abs=1e-4)
    assert put == pytest.approx(0.7791 - 1, abs=1e-4)


def test_delta_matches_hull_delta_hedging_example():
    # Hull's delta-hedging example: S=49, K=50, r=5%, sigma=20%, T=20 weeks -> delta 0.522.
    assert calc.bs_delta(49, 50, 20 / 52, 0.05, 0.0, 0.20, "call") == pytest.approx(0.522, abs=1e-3)


def test_dividend_yield_scales_delta():
    # With a continuous dividend yield q, call delta = e^(-qT) N(d1).
    s, k, t, r, q, v = 100, 100, 1.0, 0.05, 0.03, 0.25
    d1 = (math.log(s / k) + (r - q + 0.5 * v * v) * t) / (v * math.sqrt(t))
    expected = math.exp(-q * t) * 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    assert calc.bs_delta(s, k, t, r, q, v, "call") == pytest.approx(expected, rel=1e-12)


def test_zero_time_to_expiry_does_not_blow_up():
    assert 0.0 <= calc.bs_delta(100, 100, 0.0, 0.05, 0.0, 0.3, "call") <= 1.0


# --- Chain-level math -------------------------------------------------------

TRADE_DATE = date(2026, 9, 25)  # a Friday
AFTER_CLOSE = datetime(2026, 9, 26, 12, 0, tzinfo=calc.MARKET_TZ)  # Saturday


def make_chain():
    rows = [
        # type, expiration, strike, volume, IV
        ("call", "2026-09-25", 100, 1000, 0.30),   # 0DTE, near the money
        ("put", "2026-09-25", 100, 500, 0.30),     # 0DTE, near the money
        ("call", "2026-10-02", 80, 200, 0.40),     # 7 days, in the money
        ("put", "2026-10-16", 80, 300, 0.40),      # 21 days, out of the money
        ("call", "2026-12-18", 150, 400, 0.35),    # >30 days, out of the money
        ("put", "2026-12-18", 150, 100, 0.00001),  # broken IV -> skipped
        ("call", "2026-12-18", 160, 50, float("nan")),  # missing IV -> skipped
    ]
    df = pd.DataFrame(rows, columns=["type", "expiration", "strike", "volume", "impliedVolatility"])
    df["expiration"] = pd.to_datetime(df["expiration"])
    df["openInterest"] = 0
    df["lastPrice"] = 1.0
    return df


def test_summary_and_buckets():
    contracts = calc.enrich_chain(make_chain(), 100.0, TRADE_DATE, 0.04, 0.0, now=AFTER_CLOSE)
    stock = {"trade_date": TRADE_DATE, "last_close": 100.0, "volume": 100_000, "avg_volume_20d": 90_000}
    s = calc.summarize("TEST", stock, contracts)

    assert s["call_volume"] == 1650
    assert s["put_volume"] == 900
    assert s["total_contracts"] == 2550
    assert s["put_call_ratio"] == pytest.approx(900 / 1650)
    assert s["notional_shares"] == 255_000
    assert s["contracts_skipped"] == 2
    assert s["skipped_volume"] == 150

    expected = float((contracts["volume"] * 100 * contracts["delta"].abs()).sum())
    assert s["delta_adj_shares"] == pytest.approx(expected)
    assert 0 < s["delta_adj_shares"] < s["notional_shares"]
    assert s["combined_volume"] == pytest.approx(100_000 + s["delta_adj_shares"])
    assert s["options_to_stock_ratio"] == pytest.approx(s["delta_adj_shares"] / 100_000)
    assert s["pct_from_options"] == pytest.approx(100 * s["delta_adj_shares"] / s["combined_volume"])

    by_exp = calc.breakdown(contracts, "expiry_bucket").set_index(["expiry_bucket", "type"])
    assert by_exp.loc[("0DTE", "call"), "contracts"] == 1000
    assert by_exp.loc[("1-7 days", "call"), "contracts"] == 200
    assert by_exp.loc[("8-30 days", "put"), "contracts"] == 300
    assert by_exp.loc[("Over 30 days", "call"), "contracts"] == 450

    by_m = calc.breakdown(contracts, "moneyness").set_index(["moneyness", "type"])
    assert by_m.loc[("Near the money", "call"), "contracts"] == 1000
    assert by_m.loc[("In the money", "call"), "contracts"] == 200
    assert by_m.loc[("Out of the money", "put"), "contracts"] == 300
    assert by_m.loc[("In the money", "put"), "contracts"] == 100  # $150 put with stock at $100


def test_no_options():
    empty = calc.enrich_chain(make_chain().iloc[0:0], 100.0, TRADE_DATE, 0.04, 0.0)
    stock = {"trade_date": TRADE_DATE, "last_close": 100.0, "volume": 5_000, "avg_volume_20d": 5_000}
    s = calc.summarize("NOOPT", stock, empty)
    assert s["total_contracts"] == 0
    assert s["delta_adj_shares"] == 0
    assert s["put_call_ratio"] is None
    assert s["options_to_stock_ratio"] == 0
