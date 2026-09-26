"""Black-Scholes deltas and the per-ticker volume math.

Nothing in here touches the network, so it can be tested offline.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

MARKET_TZ = ZoneInfo("America/New_York")
MARKET_CLOSE = time(16, 0)

SHARES_PER_CONTRACT = 100

# Implied volatility outside this range is treated as broken data.
MIN_VALID_IV = 0.01
MAX_VALID_IV = 5.0

# Floor on time to expiry (in years) so 0DTE contracts don't divide by zero.
# One hour of calendar time.
MIN_T_YEARS = 1.0 / (365.0 * 24.0)

# Strikes within this fraction of the stock price count as "near the money".
NEAR_MONEY_BAND = 0.05

EXPIRY_BUCKETS = ["0DTE", "1-7 days", "8-30 days", "Over 30 days"]
MONEYNESS_BUCKETS = ["In the money", "Near the money", "Out of the money"]


# ---------------------------------------------------------------------------
# Black-Scholes
# ---------------------------------------------------------------------------

_erf = np.vectorize(math.erf, otypes=[float])


def norm_cdf(x):
    """Standard normal cumulative distribution (works on scalars and arrays)."""
    return 0.5 * (1.0 + _erf(np.asarray(x, dtype=float) / math.sqrt(2.0)))


def bs_delta(spot, strike, t_years, rate, div_yield, sigma, option_type):
    """Black-Scholes-Merton delta with a continuous dividend yield.

    Call delta = e^(-qT) * N(d1)
    Put delta  = e^(-qT) * (N(d1) - 1)

    Accepts scalars or numpy arrays. `option_type` is "call"/"put" (or an
    array of them).
    """
    spot = np.asarray(spot, dtype=float)
    strike = np.asarray(strike, dtype=float)
    t = np.maximum(np.asarray(t_years, dtype=float), MIN_T_YEARS)
    sigma = np.asarray(sigma, dtype=float)

    d1 = (np.log(spot / strike) + (rate - div_yield + 0.5 * sigma**2) * t) / (
        sigma * np.sqrt(t)
    )
    disc = np.exp(-div_yield * t)
    call_delta = disc * norm_cdf(d1)
    is_call = np.char.lower(np.asarray(option_type, dtype=str)) == "call"
    delta = np.where(is_call, call_delta, call_delta - disc)
    return float(delta) if delta.ndim == 0 else delta


# ---------------------------------------------------------------------------
# Per-contract enrichment
# ---------------------------------------------------------------------------


def valuation_time(trade_date: date, now: datetime | None = None) -> datetime:
    """The moment the option data is "as of".

    During a session that's now; after the close (or on a weekend) it's the
    close of the most recent trading day, so Friday's data on a Saturday is
    valued at Friday 4pm rather than as if two days had passed.
    """
    now = now or datetime.now(MARKET_TZ)
    session_close = datetime.combine(trade_date, MARKET_CLOSE, tzinfo=MARKET_TZ)
    return min(now, session_close)


def expiry_bucket(dte: int) -> str:
    if dte <= 0:
        return "0DTE"
    if dte <= 7:
        return "1-7 days"
    if dte <= 30:
        return "8-30 days"
    return "Over 30 days"


def moneyness(option_type: str, strike: float, spot: float) -> str:
    if abs(strike / spot - 1.0) <= NEAR_MONEY_BAND:
        return "Near the money"
    in_the_money = strike < spot if option_type == "call" else strike > spot
    return "In the money" if in_the_money else "Out of the money"


def enrich_chain(
    chain: pd.DataFrame,
    spot: float,
    trade_date: date,
    rate: float,
    div_yield: float,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Add DTE, buckets, delta and share equivalents to every contract.

    Expects columns: type ("call"/"put"), strike, expiration (date),
    volume, openInterest, impliedVolatility, lastPrice.
    """
    df = chain.copy()
    if df.empty:
        for col in ["dte", "t_years", "expiry_bucket", "moneyness", "iv_valid",
                    "delta", "notional_shares", "delta_adj_shares"]:
            df[col] = pd.Series(dtype=float)
        return df

    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)
    df["impliedVolatility"] = pd.to_numeric(df["impliedVolatility"], errors="coerce")

    as_of = valuation_time(trade_date, now)
    expiry_close = pd.to_datetime(df["expiration"]).map(
        lambda d: datetime.combine(d.date(), MARKET_CLOSE, tzinfo=MARKET_TZ)
    )
    df["dte"] = pd.to_datetime(df["expiration"]).map(lambda d: (d.date() - trade_date).days)
    df["t_years"] = expiry_close.map(lambda e: (e - as_of).total_seconds()) / (365.0 * 86400.0)
    df["t_years"] = df["t_years"].clip(lower=MIN_T_YEARS)

    df["expiry_bucket"] = df["dte"].map(expiry_bucket)
    df["moneyness"] = [moneyness(t, k, spot) for t, k in zip(df["type"], df["strike"])]

    iv = df["impliedVolatility"]
    df["iv_valid"] = iv.notna() & (iv >= MIN_VALID_IV) & (iv <= MAX_VALID_IV)

    df["delta"] = np.nan
    valid = df["iv_valid"]
    if valid.any():
        df.loc[valid, "delta"] = bs_delta(
            spot,
            df.loc[valid, "strike"].to_numpy(),
            df.loc[valid, "t_years"].to_numpy(),
            rate,
            div_yield,
            df.loc[valid, "impliedVolatility"].to_numpy(),
            df.loc[valid, "type"].to_numpy(),
        )

    df["notional_shares"] = df["volume"] * SHARES_PER_CONTRACT
    # Contracts with broken IV contribute nothing to the delta-adjusted total.
    df["delta_adj_shares"] = (df["notional_shares"] * df["delta"].abs()).fillna(0.0)
    return df


# ---------------------------------------------------------------------------
# Per-ticker summary and breakdowns
# ---------------------------------------------------------------------------


def summarize(ticker: str, stock: dict, contracts: pd.DataFrame) -> dict:
    """Roll one ticker's enriched chain up into the headline numbers."""
    calls = contracts[contracts["type"] == "call"] if not contracts.empty else contracts
    puts = contracts[contracts["type"] == "put"] if not contracts.empty else contracts

    call_volume = int(calls["volume"].sum()) if not calls.empty else 0
    put_volume = int(puts["volume"].sum()) if not puts.empty else 0
    total_contracts = call_volume + put_volume
    delta_adj = float(contracts["delta_adj_shares"].sum()) if not contracts.empty else 0.0
    stock_volume = int(stock["volume"])
    combined = stock_volume + delta_adj

    if contracts.empty:
        skipped = skipped_volume = 0
    else:
        bad = ~contracts["iv_valid"]
        skipped = int(bad.sum())
        skipped_volume = int(contracts.loc[bad, "volume"].sum())

    return {
        "ticker": ticker,
        "trade_date": stock["trade_date"].isoformat(),
        "last_close": float(stock["last_close"]),
        "stock_volume": stock_volume,
        "avg_volume_20d": float(stock["avg_volume_20d"]),
        "call_volume": call_volume,
        "put_volume": put_volume,
        "total_contracts": total_contracts,
        "put_call_ratio": (put_volume / call_volume) if call_volume else None,
        "notional_shares": total_contracts * SHARES_PER_CONTRACT,
        "delta_adj_shares": delta_adj,
        "combined_volume": combined,
        "options_to_stock_ratio": (delta_adj / stock_volume) if stock_volume else None,
        "pct_from_options": (100.0 * delta_adj / combined) if combined else None,
        "contracts_listed": int(len(contracts)),
        "contracts_skipped": skipped,
        "skipped_volume": skipped_volume,
    }


def breakdown(contracts: pd.DataFrame, by: str) -> pd.DataFrame:
    """Contracts and delta-adjusted shares by `by` ("expiry_bucket" or
    "moneyness") and option type, in a fixed bucket order."""
    order = EXPIRY_BUCKETS if by == "expiry_bucket" else MONEYNESS_BUCKETS
    index = pd.MultiIndex.from_product([order, ["call", "put"]], names=[by, "type"])
    if contracts.empty:
        out = pd.DataFrame(0, index=index, columns=["contracts", "delta_adj_shares"])
    else:
        out = (
            contracts.groupby([by, "type"])[["volume", "delta_adj_shares"]]
            .sum()
            .rename(columns={"volume": "contracts"})
            .reindex(index, fill_value=0)
        )
    return out.reset_index()


def by_strike(contracts: pd.DataFrame) -> pd.DataFrame:
    """Delta-adjusted shares per strike, split into calls and puts."""
    if contracts.empty:
        return pd.DataFrame(columns=["strike", "type", "contracts", "delta_adj_shares"])
    out = (
        contracts.groupby(["strike", "type"])[["volume", "delta_adj_shares"]]
        .sum()
        .rename(columns={"volume": "contracts"})
        .reset_index()
    )
    return out[out["contracts"] > 0]
