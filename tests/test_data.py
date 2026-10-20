"""Which trading session fetch_stock reports, using a stand-in for yfinance."""

from datetime import date

import pandas as pd

import data

TZ = "America/New_York"
FRIDAY, MONDAY = date(2026, 10, 16), date(2026, 10, 19)


class FakeTicker:
    def __init__(self, daily_dates, daily_volumes, quote=None):
        self.daily = pd.DataFrame(
            {"Close": 100.0, "Volume": daily_volumes, "Dividends": 0.0},
            index=pd.DatetimeIndex([pd.Timestamp(d) for d in daily_dates]).tz_localize(TZ),
        )
        self.quote = quote

    def history(self, **kwargs):
        return self.daily

    def get_history_metadata(self):
        if self.quote is None:
            return {}
        when, price, volume = self.quote
        return {
            "exchangeTimezoneName": TZ,
            "regularMarketTime": pd.Timestamp(when, tz=TZ),
            "regularMarketPrice": price,
            "regularMarketVolume": volume,
        }


def weekdays_through(last):
    return [d.date() for d in pd.bdate_range(end=last, periods=25)]


def test_quote_supplies_session_missing_from_daily_bars():
    dates = weekdays_through(FRIDAY)
    tk = FakeTicker(dates, [1_000_000] * len(dates), quote=("2026-10-19 16:00", 105.0, 2_500_000))
    stock = data.fetch_stock(tk, "AAPL")
    assert stock["trade_date"] == MONDAY
    assert stock["volume"] == 2_500_000
    assert stock["last_close"] == 105.0


def test_quote_fills_zero_volume_daily_bar():
    dates = weekdays_through(MONDAY)
    tk = FakeTicker(dates, [1_000_000] * (len(dates) - 1) + [0], quote=("2026-10-19 16:00", 105.0, 2_500_000))
    stock = data.fetch_stock(tk, "AAPL")
    assert stock["trade_date"] == MONDAY
    assert stock["volume"] == 2_500_000


def test_before_the_open_shows_previous_session():
    # Yahoo adds an empty Monday bar before the open; the quote is still Friday's.
    dates = weekdays_through(MONDAY)
    tk = FakeTicker(dates, [1_000_000] * (len(dates) - 1) + [0], quote=("2026-10-16 16:00", 100.0, 1_000_000))
    stock = data.fetch_stock(tk, "AAPL")
    assert stock["trade_date"] == FRIDAY
    assert stock["volume"] == 1_000_000


def test_without_quote_skips_trailing_zero_volume_bar():
    dates = weekdays_through(MONDAY)
    tk = FakeTicker(dates, [1_000_000] * (len(dates) - 1) + [0])
    stock = data.fetch_stock(tk, "AAPL")
    assert stock["trade_date"] == FRIDAY
    assert stock["volume"] == 1_000_000
