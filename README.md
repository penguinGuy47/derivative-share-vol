# Stock vs. Options Volume Dashboard

A small dashboard that runs on your own computer. It shows how much trading in a stock is really
happening through options. For each ticker you type in, it compares:

- **Stock volume**: shares traded in the most recent session.
- **Delta-adjusted options volume**: every option contract traded that session, converted into a
  rough estimate of the number of shares market makers would have to trade to hedge it
  (`contracts × 100 × |delta|`).

All data is free from Yahoo Finance through the `yfinance` library. You don't need an account or an API key.

---

## One-time setup

You need **Python 3.10 or newer**. If you don't have it, download it from
<https://www.python.org/downloads/>.
**Windows:** in the installer, tick **"Add python.exe to PATH"** before clicking Install.

### 1. Get the code

Either download the ZIP from GitHub (green **Code** button → **Download ZIP**) and unzip it, or
if you have git:

```
git clone https://github.com/penguinGuy47/derivative-share-vol.git
```

### 2. Open a terminal in the project folder

- **Mac:** open the **Terminal** app, type `cd ` (with a space), drag the project folder into the
  Terminal window, and press Enter.
- **Windows:** open the project folder in File Explorer, click the address bar, type
  `powershell`, and press Enter.

### 3. Create a virtual environment and install the packages

This keeps the dashboard's packages separate from everything else on your computer.

**Mac:**

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**

```
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> If Windows says *"running scripts is disabled on this system"*, run this once and then try the
> `Activate.ps1` line again:
>
> ```
> Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
> ```

When the environment is active, you'll see `(.venv)` at the start of the terminal prompt.

---

## Running the dashboard

Every time you want to use it, open a terminal in the project folder (step 2 above) and run:

**Mac:**

```
source .venv/bin/activate
streamlit run app.py
```

**Windows (PowerShell):**

```
.venv\Scripts\Activate.ps1
streamlit run app.py
```

Your browser opens at <http://localhost:8501>. If it doesn't, open that address yourself.
To stop the dashboard, go back to the terminal and press **Ctrl + C**.

> The first time you run Streamlit it may ask for your email. Just press Enter to skip it.

---

## Using it

- Type tickers separated by commas (e.g. `AAPL, TSLA, NVDA, AMC`) and press Enter.
- Data is cached for **10 minutes** so Yahoo doesn't rate-limit you. Press **Refresh data** to
  force a fresh download.
- The line above the tabs shows which trading session you're looking at. Before 9:30 AM
  Eastern on a weekday it's still the previous session; refresh after the open to see today's.
- Loading takes a few seconds per ticker, because the dashboard downloads every option
  expiration date.
- **Latest session** tab:
  - Summary table (click a column header to sort)
  - Combined stock + options volume chart
  - Options-to-stock ratio chart
  - Per-ticker drill-down by expiration, moneyness, and strike
- **History** tab: each time you load data, a snapshot of every ticker's numbers is saved to
  `history.db` in the project folder, one row per ticker per trading day. Loading again on the
  same day overwrites that day's row. Over time this builds a chart of stock vs options volume.
  Use the download buttons to export it to CSV.

### What the numbers mean

| Column | Meaning |
|---|---|
| Stock volume | Shares traded in the most recent session |
| Options contracts | Calls + puts traded, across all expirations |
| Notional shares | Contracts × 100 |
| Delta-adj. shares | Σ contracts × 100 × \|delta\|, roughly the shares needed to hedge the options flow |
| Combined volume | Stock volume + delta-adjusted shares |
| Options/Stock | Delta-adjusted shares ÷ stock volume (above 1× means options hedging exceeds stock trading) |
| % from options | Delta-adjusted shares ÷ combined volume |
| Put/Call | Put contracts ÷ call contracts |
| Skipped (bad IV) | Contracts whose implied volatility was missing or implausible (under 1% or over 500%). They count in contract totals but not in the delta-adjusted total |

Delta is calculated with the Black-Scholes formula. It uses each contract's implied volatility,
the 13-week T-bill yield (`^IRX`) as the risk-free rate, and the stock's trailing 12-month
dividend yield (0 if it pays none). Contracts expiring today (0DTE) get a minimum time to
expiry of one hour, so the formula doesn't break.

### Limitations

- Yahoo data is **delayed** and reflects the **most recent trading session**. On weekends and
  holidays you'll see the last session (e.g. Friday).
- It only counts listed options on the ticker itself. It does **not** include OTC swaps, index
  options (such as SPX), or hedging by leveraged ETFs.
- Delta-adjusted volume is an estimate. It assumes every contract traded is fully hedged by a
  market maker, which isn't always true.
- Yahoo sometimes rate-limits heavy use. If that happens, wait a few minutes and press Refresh.

---

## Running the tests (optional)

With the virtual environment active:

```
python -m pytest
```

The tests check the Black-Scholes delta against textbook values from Hull's *Options, Futures,
and Other Derivatives*. They also run the whole dashboard on made-up data, so no internet is needed.

## Files

| File | What it does |
|---|---|
| `app.py` | The Streamlit dashboard |
| `data.py` | Downloads from Yahoo Finance and saves/loads the history database |
| `calculations.py` | Black-Scholes delta and all the volume math |
| `tests/` | Automated tests |
