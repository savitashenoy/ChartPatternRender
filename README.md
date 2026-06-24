# Quant Scanner — Render Deployment

This package has been converted from the Vercel serverless setup to a standard
Flask app you can deploy on [Render](https://render.com).

## What changed from the Vercel version
- Removed `vercel.json` (Vercel-specific routing config).
- Added `gunicorn` to `requirements.txt` as the production WSGI server.
- Added `render.yaml` so Render can auto-configure the service (Blueprint deploy).

No application code changed — `app.py`, `api/index.py`, and the templates are untouched.

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
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT`
4. Add environment variables if you want custom values:
   - `FLASK_SECRET_KEY` (any random string)
   - `SCAN_TASK_TTL_SECONDS` (default `3600`)
5. Click **Create Web Service**.

## Fixing "Unexpected token '<', is not valid JSON" mid-scan
This happened because the original `/api/scan` endpoint ran the entire scan
synchronously inside a single HTTP request. For large sheets this could take
longer than the gunicorn worker timeout (or hit transient network hiccups
with yfinance), causing the connection to be killed mid-response. The browser
then received an HTML error page instead of JSON, and `res.json()` threw the
confusing "Unexpected token '<'" error.

**Fix:** the scan now runs in a background thread. `POST /api/scan` returns
immediately with a `task_id`, and the frontend polls
`GET /api/scan/status/<task_id>` every ~1.5s for progress until the scan
finishes. No single request stays open for more than a second or two.

Because progress is tracked in an in-memory dict (`scan_tasks`) inside one
Python process, the service **must run with a single worker** (multiple
threads are fine — they share memory; multiple worker *processes* do not).
This is already set in `render.yaml`:
```
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```
If you ever need to scale beyond one instance, move `scan_tasks` to Redis or
a database so all instances can see the same task state.

## Fixing a "pandas build failed" error
If your build log shows pandas trying to compile from source and failing
with a Cython/C++ error, it means Render picked a Python version (e.g. 3.14)
for which pandas has no pre-built wheel. This package pins the runtime via
a `.python-version` file (`3.11.9`) at the repo root, which Render reads
automatically regardless of how the service was created (Blueprint or
manual Web Service). If you still see this error:
1. Confirm `.python-version` exists at the **root** of the repo Render is
   building (not inside a subfolder).
2. In the Render dashboard, go to your service > **Environment** and check
   there isn't a conflicting `PYTHON_VERSION` value overriding it.
3. Trigger a fresh deploy with **Clear build cache & deploy**.

## Notes
- `ScannerData.xlsx` is bundled in the repo and read at runtime from disk —
  no extra storage setup needed for this dataset.
- The free Render plan spins down when idle, so the first request after
  inactivity may take ~30-60s to wake up.
- Scan results are stored in memory (`scan_tasks` dict) — on the free plan
  with a single instance this works fine, but if you ever scale to multiple
  instances/workers you'd want to move this to Redis or a database, since
  in-memory state isn't shared across processes.
