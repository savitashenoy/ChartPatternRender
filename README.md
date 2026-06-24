# Quant Scanner — Render Deployment

A Flask app that scans a watchlist (from `ScannerData.xlsx`) for chart patterns
(rectangle consolidation, bull flag, rounding base) using Yahoo Finance data
via `yfinance`, deployable on [Render](https://render.com).

## Architecture: one ticker per request, scanned sequentially

Earlier versions of this app batched many tickers into a single
`yf.download("AAA BBB CCC ...")` call. Yahoo Finance's unofficial API
aggressively rate-limits/blocks bursty multi-ticker batch requests from
shared cloud IPs (Render, Heroku, AWS, etc.), which caused scans to fail with
`Failed to get ticker 'X' reason: Expecting value: line 1 column 1 (char 0)`
and appear to hang.

This package instead scans **one ticker per request**, using
`yf.Ticker(ticker).history(...)` — the same low-level call a single stock
page makes — and the frontend drives a simple sequential loop:

1. `GET /api/tickers?sheet=<name>` — loads the ticker list for the chosen sheet.
2. For each ticker, in order (never in parallel):
   `POST /api/scan_one` with `{ticker, sheet, config}` → returns any pattern
   matches for that ticker (or an empty list / non-fatal `warning` if Yahoo
   had no data for it).
3. The UI renders results incrementally as each ticker completes, and shows
   live progress (`N / total`, elapsed time).

This is intentionally simple and stateless — there's no background thread,
no in-memory task store, and no polling endpoint. Each HTTP request is
self-contained, so the service scales cleanly across multiple gunicorn
workers (`--workers 2 --threads 4` in `render.yaml`), unlike an in-memory
task-store design which would require pinning to a single worker process.

If a ticker's request fails or returns no data, it's recorded as a
non-fatal warning (shown in a collapsible panel after the scan) and the loop
just continues to the next ticker — one bad ticker never aborts the whole scan.

## Deploy options

### Option A — One-click Blueprint (recommended)
1. Push this folder's contents to a GitHub/GitLab repo.
2. In the Render dashboard, click **New > Blueprint** and point it at the repo.
3. Render will read `render.yaml` and create the web service automatically.
4. Click **Apply** — it will install dependencies and start the app.

### Option B — Manual Web Service
1. Push this folder to a repo (or use Render's "Deploy from existing repo").
2. In Render: **New > Web Service**, connect the repo.
3. Settings:
   - **Runtime:** Python 3
   - **Build Command:** `python -m pip install --upgrade pip setuptools wheel && pip install --prefer-binary -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 300`
   - **Health Check Path:** `/health`
4. Add `FLASK_SECRET_KEY` as an environment variable (any random string).
5. Click **Create Web Service**.

## Notes
- `ScannerData.xlsx` is bundled in the repo and read at runtime from disk —
  no extra storage setup needed for this dataset.
- The free Render plan spins down when idle, so the first request after
  inactivity may take ~30-60s to wake up.
- If you still see Yahoo Finance failures for many tickers in a row, that
  usually means Yahoo has temporarily flagged Render's IP range — wait a
  while and try again, or scan a smaller sheet. For guaranteed reliability
  in production, consider switching to an official paid market-data API
  (Alpha Vantage, Twelve Data, Finnhub, etc.) instead of Yahoo's unofficial
  endpoint.
- `.python-version` and `runtime.txt`-equivalent pinning (`PYTHON_VERSION`
  env var in `render.yaml`) keep the build on Python 3.11, since some
  dependencies don't ship pre-built wheels for the very latest Python
  version Render may default to — avoiding slow/broken from-source builds.
