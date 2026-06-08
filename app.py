# -*- coding: utf-8 -*-
"""
Stock dashboard backend.

Runs the investment-screen / fundamentals logic and serves it as JSON to a
web frontend. Data is built once and cached for CACHE_TTL seconds so page
reloads are instant and Yahoo isn't hammered (the .info calls are slow).

Run:
    pip install flask yfinance pandas
    python app.py
Then open http://localhost:5000
"""

import time
import threading
import math
import gc

import yfinance as yf
import pandas as pd
from flask import Flask, jsonify, render_template


# ----------------------------------------------------------------------
# Universe
# ----------------------------------------------------------------------
markets = {
    "Indices": {
        "AEX": "^AEX", "S&P 500": "^GSPC", "Nasdaq Composite": "^IXIC",
        "Dow Jones": "^DJI", "DAX": "^GDAXI", "FTSE 100": "^FTSE",
    },
    "European ETFs - EUR": {
        "MSCI World UCITS": "EUNL.DE", "S&P 500 UCITS": "SXR8.DE",
        "Nasdaq100 UCITS": "SXRV.DE", "FTSE All World UCITS": "VWCE.DE",
    },
    "Dutch Stocks - EUR": {
        "ASML": "ASML.AS", "Adyen": "ADYEN.AS", "Prosus": "PRX.AS",
        "ING": "INGA.AS", "ABN AMRO": "ABN.AS", "Philips": "PHIA.AS",
        "Heineken": "HEIA.AS", "Wolters Kluwer": "WKL.AS",
        "Akzo Nobel": "AKZA.AS", "KPN": "KPN.AS", "Shell": "SHELL.AS",
        "Unilever": "UNA.AS",
    },
    "German Stocks - EUR": {
        "BMW": "BMW.DE", "Adidas": "ADS.DE", "Mercedes-Benz": "MBG.DE",
        "Volkswagen": "VOW3.DE", "Volkswagen Pref": "VOW.DE", "BASF": "BAS.DE",
    },
    "US Quality Stocks - USD": {
        "Apple": "AAPL", "Microsoft": "MSFT", "Amazon": "AMZN",
        "Alphabet A": "GOOGL", "Alphabet C": "GOOG", "Visa": "V",
        "Mastercard": "MA", "Costco": "COST", "Berkshire Hathaway B": "BRK-B",
        "Procter & Gamble": "PG", "McCormick": "MKC", "Nike": "NKE",
    },
    "US Semiconductors - USD": {
        "NVIDIA": "NVDA", "AMD": "AMD", "Intel": "INTC", "NXP": "NXPI",
        "Applied Materials": "AMAT", "Lam Research": "LRCX", "KLA": "KLAC",
    },
    "European Semiconductors - EUR": {"ASML": "ASML.AS"},
    "US Energy - USD": {
        "Chevron": "CVX", "Exxon Mobil": "XOM", "BP": "BP",
        "TotalEnergies ADR": "TTE",
    },
    "European Energy - EUR": {"Shell": "SHELL.AS"},
    "Crypto - USD": {
        "Bitcoin": "BTC-USD", "Ethereum": "ETH-USD", "Cardano": "ADA-USD",
    },
}

FUND_FIELDS = {
    "marketCap": "Market Cap", "trailingPE": "P/E (TTM)",
    "forwardPE": "P/E (Fwd)", "trailingEps": "EPS (TTM)",
    "forwardEps": "EPS (Fwd)", "priceToBook": "P/B",
    "totalRevenue": "Revenue (TTM)", "grossMargins": "Gross Margin",
    "profitMargins": "Net Margin", "returnOnEquity": "ROE",
    "revenueGrowth": "Rev Growth", "earningsGrowth": "EPS Growth",
    "dividendYield": "Div Yield", "debtToEquity": "Debt/Equity",
}

CACHE_TTL = 600  # seconds (10 min)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def clean(x):
    """Make a value JSON-safe: NaN/inf -> None, numpy -> python scalar."""
    if x is None:
        return None
    try:
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
    except TypeError:
        pass
    if hasattr(x, "item"):           # numpy scalar
        x = x.item()
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def r2(x):
    x = clean(x)
    return round(x, 2) if isinstance(x, (int, float)) else None


def pct(x):
    x = clean(x)
    return round(x * 100, 2) if isinstance(x, (int, float)) else None


def millions(x):
    x = clean(x)
    return round(x / 1e6, 1) if isinstance(x, (int, float)) else None


def win(s, years):
    cutoff = s.index[-1] - pd.DateOffset(years=years)
    w = s[s.index >= cutoff]
    return w if len(w) else None


def mom_date(prices, lookback_years=1, skip_days=21):
    out = {}
    for t in prices.columns:
        col = prices[t].dropna()
        if len(col) < 30:
            out[t] = None
            continue
        end_date = col.index[-1] - pd.Timedelta(days=skip_days)
        start_date = end_date - pd.DateOffset(years=lookback_years)
        try:
            end_px = col[col.index <= end_date].iloc[-1]
            start_px = col[col.index <= start_date].iloc[-1]
            out[t] = end_px / start_px - 1
        except IndexError:
            out[t] = None
    return pd.Series(out)


# ----------------------------------------------------------------------
# Core build (this is the expensive part - cached)
# ----------------------------------------------------------------------
def build_data():
    tickers, market_lookup = {}, {}
    for market, items in markets.items():
        for name, ticker in items.items():
            tickers[name] = ticker
            market_lookup[ticker] = market
    symbols = list(dict.fromkeys(tickers.values()))
    inv = {v: k for k, v in tickers.items()}

    # 5y is plenty for the 1Y/5Y windows and momentum; lighter on memory than 10y.
    data = yf.download(symbols, period="5y", interval="1d",
                       auto_adjust=False, group_by="column", progress=False)
    close = data["Close"].ffill().dropna(how="all").copy()
    adj_close = data["Adj Close"].ffill().dropna(how="all").copy()
    del data                       # free the full OHLCV frame
    gc.collect()

    # currency + fundamentals (one Ticker call per symbol)
    currencies, fundamentals = {}, {}
    for ticker in symbols:
        try:
            tk = yf.Ticker(ticker)
            currencies[ticker] = tk.fast_info.get("currency", "Unknown")
            info = tk.info
            fundamentals[ticker] = {lbl: info.get(k) for k, lbl in FUND_FIELDS.items()}
        except Exception:
            currencies[ticker] = "Unknown"
            fundamentals[ticker] = {lbl: None for lbl in FUND_FIELDS.values()}

    mom_latest = mom_date(adj_close)
    ret = close.pct_change()

    # ---- screen ----
    screen = []
    for t in close.columns:
        s = close[t].dropna()
        if len(s) < 252:
            continue
        price, dt = s.iloc[-1], s.index[-1]
        w1, w5, w10 = win(s, 1), win(s, 5), win(s, 10)
        low1, high1 = w1.min(), w1.max()
        low5, high5 = (w5.min(), w5.max()) if w5 is not None else (None, None)
        low10, high10 = (w10.min(), w10.max()) if w10 is not None else (None, None)

        def above(lo):  return (price / lo - 1) * 100 if lo not in (None, 0) else None
        def below(hi):  return (price / hi - 1) * 100 if hi not in (None, 0) else None

        mv = mom_latest.get(t)
        dm = ret[t].iloc[-1] if t in ret.columns else None

        screen.append({
            "Market": market_lookup.get(t, "Unknown"),
            "Name": inv.get(t, t), "Ticker": t,
            "Currency": currencies.get(t, "Unknown"),
            "Date": str(dt.date()), "Price": r2(price),
            "Daily Move (%)": pct(dm),
            "Momentum 12-1 (%)": pct(mv),
            "Signal": "BUY" if clean(mv) is not None and mv > 0 else "FLAT",
            "1Y Low": r2(low1), "% Above 1Y Low": r2(above(low1)),
            "1Y High": r2(high1), "% Below 1Y High": r2(below(high1)),
            "5Y Low": r2(low5), "% Above 5Y Low": r2(above(low5)),
            "5Y High": r2(high5), "% Below 5Y High": r2(below(high5)),
            "10Y Low": r2(low10), "% Above 10Y Low": r2(above(low10)),
            "10Y High": r2(high10), "% Below 10Y High": r2(below(high10)),
        })

    # ---- fundamentals ----
    fund = []
    for t in symbols:
        f = fundamentals.get(t, {})
        fund.append({
            "Market": market_lookup.get(t, "Unknown"),
            "Name": inv.get(t, t), "Ticker": t,
            "Currency": currencies.get(t, "Unknown"),
            "Market Cap (M)": millions(f.get("Market Cap")),
            "Revenue TTM (M)": millions(f.get("Revenue (TTM)")),
            "P/E (TTM)": r2(f.get("P/E (TTM)")), "P/E (Fwd)": r2(f.get("P/E (Fwd)")),
            "EPS (TTM)": r2(f.get("EPS (TTM)")), "EPS (Fwd)": r2(f.get("EPS (Fwd)")),
            "P/B": r2(f.get("P/B")),
            "Gross Margin %": pct(f.get("Gross Margin")),
            "Net Margin %": pct(f.get("Net Margin")),
            "ROE %": pct(f.get("ROE")),
            "Rev Growth %": pct(f.get("Rev Growth")),
            "EPS Growth %": pct(f.get("EPS Growth")),
            "Div Yield %": pct(f.get("Div Yield")),
            "Debt/Equity": r2(f.get("Debt/Equity")),
        })

    # ---- indexed monthly series per market (for line charts) ----
    series_by_market = {}
    monthly = close.resample("ME").last()
    for market_name, items in markets.items():
        syms = [s for s in dict.fromkeys(items.values()) if s in monthly.columns]
        if not syms:
            continue
        block = monthly[syms].dropna(how="all")
        if block.empty:
            continue
        labels = [str(d.date()) for d in block.index]
        datasets = []
        for s in syms:
            col = block[s]
            first_valid = col.dropna()
            if first_valid.empty:
                continue
            base = first_valid.iloc[0]
            idx = (col / base * 100)
            datasets.append({
                "name": inv.get(s, s),
                "data": [clean(v) for v in idx.tolist()],
            })
        if datasets:
            series_by_market[market_name] = {"labels": labels, "datasets": datasets}

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "markets": list(markets.keys()),
        "screen": screen,
        "fundamentals": fund,
        "series_by_market": series_by_market,
    }


# ----------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------
_cache = {"data": None, "ts": 0}
_lock = threading.Lock()


def get_data(force=False):
    with _lock:
        age = time.time() - _cache["ts"]
        if force or _cache["data"] is None or age > CACHE_TTL:
            _cache["data"] = build_data()
            _cache["ts"] = time.time()
        return _cache["data"]


# ----------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    return jsonify(get_data())


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    return jsonify(get_data(force=True))


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
