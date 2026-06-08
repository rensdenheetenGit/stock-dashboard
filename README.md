# Market Screen — live dashboard

A small Flask web app that runs your yfinance investment-screen + fundamentals
logic and displays it as an interactive dashboard: summary cards, charts
(momentum ranking, distance below 1Y high, indexed performance), and a
sortable/filterable table with both the price screen and fundamentals.

## Run locally

```bash
pip install flask yfinance pandas openpyxl
python app.py
```

Then open <http://localhost:5000>.

First load takes ~1–2 minutes because it pulls fundamentals (`.info`) for every
ticker. After that it's cached for 10 minutes, so reloads are instant. The
**Refresh data** button forces a fresh pull.

## Files

- `app.py` — backend. The universe (`markets`) and all the screen logic live
  here; it's the same date-based-window logic as your script, refactored to
  return JSON instead of printing/Excel. Routes:
  - `/` serves the dashboard
  - `/api/data` returns the cached JSON
  - `/api/refresh` (POST) forces a rebuild
- `templates/index.html` — the dashboard (HTML + CSS + JS, Chart.js from CDN).

## Editing the universe

Add or remove tickers in the `markets` dict in `app.py` — the frontend adapts
automatically (market filters, charts, and the per-market line chart are all
driven by whatever's in there).

## Caching

`CACHE_TTL` at the top of `app.py` controls how long data is reused (default
600s). Lower it for fresher data, raise it to go easier on Yahoo.

## Deploying (when you're ready)

Because data is fetched server-side, you need a host that runs Python, not a
static host like GitHub Pages. Free/cheap options that work with no code
changes: **Render**, **Railway**, or **PythonAnywhere**. For production swap the
dev server for gunicorn:

```bash
pip install gunicorn
gunicorn app:app
```

## Notes / caveats (carried over from the script)

- Indices and crypto have no fundamentals; ETFs only partial — those cells show
  "—", which is correct, not an error.
- Revenue / market cap are in each instrument's native currency and are **not**
  FX-converted; ratios (P/E, margins, momentum, % moves) are currency-neutral.
- `.info` is an unaudited current snapshot with no point-in-time history — fine
  for a personal screen, not for anything that needs to be reproducible.
