# -*- coding: utf-8 -*-
"""
Stock dashboard backend.

Prices refresh frequently (PRICE_TTL); the slow fundamentals (.info) loop is
cached separately and only refreshed hourly (FUND_TTL), so the page can update
often without re-running the expensive part every time.

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

# how long each cache lives, in seconds
PRICE_TTL = 120     # prices refresh ~every 2 min
FUND_TTL = 3600     # fundamentals refresh ~hourly (slow .info loop)

# static lookups (built once)
TICKERS, MARKET_LOOKUP = {}, {}
for _market, _items in markets.items():
    for _name, _ticker in _items.items():
        TICKERS[_name] = _ticker
        MARKET_LOOKUP[_ticker] = _market
SYMBOLS = list(dict.fromkeys(TICKERS.values()))
INV = {v: k for k, v in TICKERS.items()}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def clean(x):
    if x is None:
        return None
    try:
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
    except TypeError:
        pass
    if hasattr(x, "item"):
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
# Fundamentals cache (slow .info loop, refreshed hourly)
# ----------------------------------------------------------------------
_fund_cache = {"data": None, "ts": 0}


def get_fundamentals(force=False):
    age = time.time() - _fund_cache["ts"]
    if force or _fund_cache["data"] is None or age > FUND_TTL:
        currencies, fundamentals = {}, {}
        for ticker in SYMBOLS:
            try:
                tk = yf.Ticker(ticker)
                currencies[ticker] = tk.fast_info.get("currency", "Unknown")
                info = tk.info
                fundamentals[ticker] = {lbl: info.get(k) for k, lbl in FUND_FIELDS.items()}
            except Exception:
                currencies[ticker] = "Unknown"
                fundamentals[ticker] = {lbl: None for lbl in FUND_FIELDS.values()}
        _fund_cache["data"] = (currencies, fundamentals)
        _fund_cache["ts"] = time.time()
    return _fund_cache["data"]


# ----------------------------------------------------------------------
# Build (prices recompute each time; fundamentals come from the hourly cache)
# ----------------------------------------------------------------------
def build_data():
    currencies, fundamentals = get_fundamentals()

    data = yf.download(SYMBOLS, period="5y", interval="1d",
                       auto_adjust=False, group_by="column", progress=False)
    close = data["Close"].ffill().dropna(how="all").copy()
    adj_close = data["Adj Close"].ffill().dropna(how="all").copy()
    del data
    gc.collect()

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
            "Market": MARKET_LOOKUP.get(t, "Unknown"),
            "Name": INV.get(t, t), "Ticker": t,
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

    # ---- fundamentals table ----
    fund = []
    for t in SYMBOLS:
        f = fundamentals.get(t, {})
        fund.append({
            "Market": MARKET_LOOKUP.get(t, "Unknown"),
            "Name": INV.get(t, t), "Ticker": t,
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

    # ---- indexed + raw monthly series per market ----
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
                "name": INV.get(s, s),
                "data": [clean(v) for v in idx.tolist()],
                "raw": [clean(v) for v in col.tolist()],
            })
        if datasets:
            series_by_market[market_name] = {"labels": labels, "datasets": datasets}

    now = time.time()
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "generated_ts": now,                      # epoch seconds, formatted to local time in browser
        "markets": list(markets.keys()),
        "screen": screen,
        "fundamentals": fund,
        "series_by_market": series_by_market,
    }


# ----------------------------------------------------------------------
# Price cache (short)
# ----------------------------------------------------------------------
_data_cache = {"data": None, "ts": 0}
_lock = threading.Lock()


def get_data(force=False):
    with _lock:
        age = time.time() - _data_cache["ts"]
        if force or _data_cache["data"] is None or age > PRICE_TTL:
            if force:
                get_fundamentals(force=True)   # manual refresh also refreshes fundamentals
            _data_cache["data"] = build_data()
            _data_cache["ts"] = time.time()
        return _data_cache["data"]


# ----------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------
app = Flask(__name__)


# ---- Auth: styled login page + session cookie (a few hours) ----
import os
import secrets
from datetime import timedelta
from flask import request, session, redirect, url_for

APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")   # kept for backward compat
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.permanent_session_lifetime = timedelta(hours=4)


def load_users():
    """
    Users come from the APP_USERS env var as comma-separated user:password pairs,
    e.g.  rens:pw1,sander:pw2,emma:pw3
    Falls back to the older APP_USER/APP_PASSWORD single login if APP_USERS isn't set.
    """
    raw = os.environ.get("APP_USERS", "").strip()
    users = {}
    if raw:
        for pair in raw.split(","):
            if ":" in pair:
                u, pw = pair.split(":", 1)
                if u.strip():
                    users[u.strip()] = pw.strip()
    if not users:   # fallback to single-user vars
        users[os.environ.get("APP_USER", "admin")] = APP_PASSWORD
    return users


USERS = load_users()


@app.before_request
def gate():
    p = request.path
    if p == "/login" or p.startswith("/static"):
        return
    if session.get("auth"):
        return
    if p.startswith("/api/"):
        return ("auth required", 401)   # JS handles this by sending you to /login
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        pw = request.form.get("password", "")
        expected = USERS.get(u)
        if expected is not None and secrets.compare_digest(pw, expected):
            session.permanent = True
            session["auth"] = True
            session["user"] = u
            return redirect(url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
