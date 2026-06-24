import csv
import io
import json
import os
import threading
import time
import uuid
from pathlib import Path

import pandas as pd
import yfinance as yf
from flask import Flask, Response, jsonify, render_template, request


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "ScannerData.xlsx"

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "quant_scanner_dev_secret")

scan_tasks = {}
scan_tasks_lock = threading.Lock()
TASK_TTL_SECONDS = int(os.environ.get("SCAN_TASK_TTL_SECONDS", "3600"))

DEFAULT_SCAN_CONFIG = {
    "lookback_period": "1y",
    "min_avg_volume": 0,
    "min_price": 0.0,
    "rb_u_shape_min_weeks": 12,
    "rb_depth_threshold": 0.85,
    "rb_proximity": 0.97,
    "bf_pole_return": 0.20,
    "bf_flag_days_min": 5,
    "bf_flag_days_max": 15,
    "bf_retracement_min": 0.50,
    "rect_days": 15,
    "rect_max_range": 0.07,
    "rect_top_percentile": 0.80,
}


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def cleanup_tasks():
    cutoff = time.time() - TASK_TTL_SECONDS
    with scan_tasks_lock:
        expired_ids = [
            task_id
            for task_id, task in scan_tasks.items()
            if task.get("updated_at", task.get("created_at", 0)) < cutoff
        ]
        for task_id in expired_ids:
            scan_tasks.pop(task_id, None)


def workbook():
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"Scanner workbook not found: {DATA_FILE.name}")
    return pd.ExcelFile(DATA_FILE)


def sheet_names():
    return workbook().sheet_names


def load_tickers(sheet_name):
    names = sheet_names()
    if sheet_name not in names:
        raise ValueError("Selected sheet was not found in ScannerData.xlsx.")

    frame = pd.read_excel(DATA_FILE, sheet_name=sheet_name, header=None)
    raw_values = frame.stack().dropna().astype(str).tolist()
    tickers = []
    seen = set()
    ignored_headers = {"ticker", "tickers", "symbol", "symbols", "stock", "stocks"}

    for value in raw_values:
        ticker = value.strip().upper()
        if not ticker or ticker.lower() in ignored_headers:
            continue
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)

    return tickers


def parse_scan_config(payload):
    config = DEFAULT_SCAN_CONFIG.copy()
    raw = payload.get("config", {}) if isinstance(payload, dict) else {}

    config["lookback_period"] = raw.get("lookback_period", config["lookback_period"])
    if config["lookback_period"] not in {"6mo", "1y", "2y", "5y"}:
        config["lookback_period"] = DEFAULT_SCAN_CONFIG["lookback_period"]

    int_fields = {
        "min_avg_volume": (0, 100_000_000),
        "rb_u_shape_min_weeks": (4, 80),
        "bf_flag_days_min": (1, 30),
        "bf_flag_days_max": (2, 60),
        "rect_days": (5, 90),
    }
    float_fields = {
        "min_price": (0.0, 1_000_000.0),
        "rb_depth_threshold": (0.50, 0.98),
        "rb_proximity": (0.80, 1.10),
        "bf_pole_return": (0.01, 2.0),
        "bf_retracement_min": (0.10, 0.95),
        "rect_max_range": (0.01, 0.50),
        "rect_top_percentile": (0.10, 1.0),
    }

    for field, (minimum, maximum) in int_fields.items():
        try:
            config[field] = int(clamp(int(raw.get(field, config[field])), minimum, maximum))
        except (TypeError, ValueError):
            pass

    for field, (minimum, maximum) in float_fields.items():
        try:
            config[field] = float(clamp(float(raw.get(field, config[field])), minimum, maximum))
        except (TypeError, ValueError):
            pass

    if config["bf_flag_days_min"] > config["bf_flag_days_max"]:
        config["bf_flag_days_min"], config["bf_flag_days_max"] = (
            config["bf_flag_days_max"],
            config["bf_flag_days_min"],
        )

    return config


def download_chunk(tickers, period, max_retries=3):
    """Downloads one chunk of tickers. Yahoo Finance aggressively rate-limits
    or blocks bursts of concurrent requests, especially from shared/datacenter
    IPs like Render's — that's what causes 'Expecting value: line 1 column 1'
    errors (Yahoo returning an empty body instead of JSON). To reduce that,
    requests are sequential (threads=False) and retried with backoff."""
    joined = " ".join(tickers)
    last_exc = None

    for attempt in range(max_retries):
        try:
            data = yf.download(
                joined,
                period=period,
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            if data is not None and not data.empty:
                return data
            last_exc = RuntimeError("Yahoo Finance returned no data for this batch.")
        except Exception as exc:
            last_exc = exc

        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)  # 1s, 2s, 4s backoff

    raise last_exc if last_exc else RuntimeError("Unknown download failure.")


def ticker_frame(raw_data, ticker, total_tickers):
    if raw_data.empty:
        return pd.DataFrame()

    if total_tickers == 1 and not isinstance(raw_data.columns, pd.MultiIndex):
        return raw_data.copy()

    if isinstance(raw_data.columns, pd.MultiIndex):
        level_zero = raw_data.columns.get_level_values(0)
        level_one = raw_data.columns.get_level_values(1)
        if ticker in level_zero:
            return raw_data[ticker].copy()
        if ticker in level_one:
            return raw_data.xs(ticker, level=1, axis=1).copy()

    return pd.DataFrame()


def chart_payload(df, overlays):
    preview = df.tail(90).copy()
    return {
        "labels": [idx.strftime("%Y-%m-%d") for idx in preview.index],
        "close": [round(float(value), 2) for value in preview["Close"].tolist()],
        "overlays": overlays,
    }


def analyze_ticker(ticker, df, config):
    try:
        if df.empty or len(df) < 60:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=["Close", "High", "Low"])
        if df.empty or len(df) < 60:
            return None

        current_price = float(df["Close"].iloc[-1])
        avg_volume_20 = float(df["Volume"].tail(20).mean()) if "Volume" in df else 0.0

        if current_price < config["min_price"]:
            return None
        if avg_volume_20 < config["min_avg_volume"]:
            return None

        prev_close = float(df["Close"].iloc[-2]) if len(df) >= 2 else current_price
        change_pct = ((current_price - prev_close) / prev_close) * 100 if prev_close else 0.0
        matches = []

        rect_days = min(config["rect_days"], len(df))
        rect_df = df.iloc[-rect_days:]
        box_high = float(rect_df["High"].max())
        box_low = float(rect_df["Low"].min())
        box_range = (box_high - box_low) / box_low if box_low else 0.0

        if box_range <= config["rect_max_range"]:
            position = (current_price - box_low) / (box_high - box_low) if box_high != box_low else 0
            if position >= config["rect_top_percentile"]:
                tightness_score = clamp((config["rect_max_range"] - box_range) / config["rect_max_range"], 0, 1)
                position_score = clamp(position, 0, 1)
                confidence = round(55 + (tightness_score * 25) + (position_score * 20))
                overlays = [
                    {
                        "type": "rectangle",
                        "high": round(box_high, 2),
                        "low": round(box_low, 2),
                        "start": rect_df.index[0].strftime("%Y-%m-%d"),
                        "end": rect_df.index[-1].strftime("%Y-%m-%d"),
                    }
                ]
                matches.append(
                    {
                        "pattern": "RECTANGLE",
                        "confidence": confidence,
                        "reason": f"Consolidating in a tight {box_range * 100:.1f}% range near the upper boundary.",
                        "chart": chart_payload(df, overlays),
                    }
                )

        last_30 = df.iloc[-30:]
        if len(last_30) == 30:
            pole_peak_idx = int(last_30["High"].argmax())
            pole_peak_val = float(last_30["High"].iloc[pole_peak_idx])
            flag_len = 30 - pole_peak_idx - 1

            if config["bf_flag_days_min"] <= flag_len <= config["bf_flag_days_max"] and pole_peak_idx > 0:
                pole_start_idx = int(last_30["Low"].iloc[: pole_peak_idx + 1].argmin())
                pole_start_val = float(last_30["Low"].iloc[pole_start_idx])
                pole_return = (pole_peak_val - pole_start_val) / pole_start_val if pole_start_val else 0.0
                if pole_return >= config["bf_pole_return"]:
                    retracement_level = pole_start_val + (
                        (pole_peak_val - pole_start_val) * config["bf_retracement_min"]
                    )
                    flag_slice = last_30.iloc[pole_peak_idx + 1 :]
                    flag_min = float(flag_slice["Low"].min()) if not flag_slice.empty else 0.0

                    if flag_min >= retracement_level and not flag_slice.empty:
                        pole_vol = float(last_30["Volume"].iloc[: pole_peak_idx + 1].mean())
                        flag_vol = float(flag_slice["Volume"].mean())
                        if pole_vol and flag_vol < pole_vol:
                            pole_score = clamp(pole_return / max(config["bf_pole_return"] * 2, 0.01), 0, 1)
                            volume_score = clamp((pole_vol - flag_vol) / pole_vol, 0, 1)
                            retracement_score = clamp((flag_min - retracement_level) / max(pole_peak_val - pole_start_val, 1), 0, 1)
                            confidence = round(55 + (pole_score * 25) + (volume_score * 15) + (retracement_score * 5))
                            overlays = [
                                {
                                    "type": "bull_flag",
                                    "pole_start": last_30.index[pole_start_idx].strftime("%Y-%m-%d"),
                                    "pole_peak": last_30.index[pole_peak_idx].strftime("%Y-%m-%d"),
                                    "pole_start_price": round(pole_start_val, 2),
                                    "pole_peak_price": round(pole_peak_val, 2),
                                    "flag_low": round(flag_min, 2),
                                }
                            ]
                            matches.append(
                                {
                                    "pattern": "BULL FLAG",
                                    "confidence": confidence,
                                    "reason": f"Pole rally {pole_return * 100:.1f}% followed by lower-volume consolidation.",
                                    "chart": chart_payload(df, overlays),
                                }
                            )

        weekly = df.resample("W-FRI").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
        ).dropna()
        if len(weekly) >= 40:
            hist_weeks = weekly.iloc[:-4]
            if not hist_weeks.empty:
                resistance = float(hist_weeks["High"].max())
                u_shape_weeks = hist_weeks[hist_weeks["High"] < resistance * config["rb_depth_threshold"]]

                if len(u_shape_weeks) >= config["rb_u_shape_min_weeks"]:
                    current_w_close = float(weekly["Close"].iloc[-1])
                    if current_w_close >= resistance * config["rb_proximity"]:
                        avg_vol_10 = float(weekly["Volume"].iloc[-11:-1].mean())
                        cur_vol = float(weekly["Volume"].iloc[-1])
                        if avg_vol_10 and cur_vol > avg_vol_10:
                            proximity_score = clamp(current_w_close / resistance, 0, 1.05) / 1.05
                            base_score = clamp(len(u_shape_weeks) / max(config["rb_u_shape_min_weeks"] * 2, 1), 0, 1)
                            volume_score = clamp((cur_vol - avg_vol_10) / avg_vol_10, 0, 1)
                            confidence = round(55 + (proximity_score * 20) + (base_score * 15) + (volume_score * 10))
                            overlays = [
                                {
                                    "type": "rounding_base",
                                    "resistance": round(resistance, 2),
                                    "start": weekly.index[max(len(weekly) - 40, 0)].strftime("%Y-%m-%d"),
                                }
                            ]
                            matches.append(
                                {
                                    "pattern": "ROUNDING BASE",
                                    "confidence": confidence,
                                    "reason": f"U-shaped accumulation is pressing near {resistance:.2f} with volume expansion.",
                                    "chart": chart_payload(df, overlays),
                                }
                            )

        if not matches:
            return None

        return {
            "ticker": ticker,
            "price": round(current_price, 2),
            "change_pct": round(change_pct, 2),
            "avg_volume_20": round(avg_volume_20),
            "matches": matches,
        }

    except Exception as exc:
        return {"ticker": ticker, "error": str(exc)}


def run_scan_task(task_id, tickers, config):
    """Runs in a background thread. Scans tickers in chunks and writes
    incremental progress into scan_tasks so the frontend can poll it,
    instead of holding one long-lived HTTP request open (which is what
    was causing gateway timeouts / non-JSON error pages mid-scan)."""
    results = []
    failures = []
    chunk_size = 20  # smaller batches = gentler on Yahoo's rate limiter, finer progress updates
    total = len(tickers)
    consecutive_chunk_failures = 0
    max_consecutive_chunk_failures = 3  # strong signal Yahoo is blanket-blocking this server
    max_scan_seconds = 480  # hard cap so a fully rate-limited scan can't "hang" forever

    def finish_early(reason):
        with scan_tasks_lock:
            task = scan_tasks.get(task_id)
            if task is None:
                return
            task["scanned"] = total
            task["results"] = results
            task["failures"] = failures
            task["status"] = "done"
            task["error"] = reason
            task["elapsed_seconds"] = round(time.time() - task["started_at"], 2)
            task["updated_at"] = time.time()

    try:
        for start in range(0, total, chunk_size):
            chunk = tickers[start : start + chunk_size]

            with scan_tasks_lock:
                task = scan_tasks.get(task_id)
                if task is None:
                    return  # task expired/removed; abandon quietly
                elapsed_so_far = time.time() - task["started_at"]

            if elapsed_so_far > max_scan_seconds:
                remaining = tickers[start:]
                failures.extend(
                    {"ticker": t, "error": "Skipped: scan time limit reached."} for t in remaining
                )
                finish_early(
                    "Stopped early after hitting the time limit, likely due to Yahoo Finance "
                    "rate-limiting. Results below are from tickers that completed in time."
                )
                return

            try:
                raw_data = download_chunk(chunk, config["lookback_period"])
                consecutive_chunk_failures = 0
            except Exception as exc:
                consecutive_chunk_failures += 1
                failures.extend({"ticker": ticker, "error": str(exc)} for ticker in chunk)

                if consecutive_chunk_failures >= max_consecutive_chunk_failures:
                    remaining = tickers[start + len(chunk) :]
                    failures.extend(
                        {"ticker": t, "error": "Skipped: repeated Yahoo Finance failures."}
                        for t in remaining
                    )
                    finish_early(
                        "Stopped early: Yahoo Finance repeatedly rejected requests from this "
                        "server (likely rate-limiting/blocking a shared cloud IP). Try again "
                        "later, or scan a smaller sheet."
                    )
                    return
            else:
                for ticker in chunk:
                    df = ticker_frame(raw_data, ticker, len(chunk))
                    analyzed = analyze_ticker(ticker, df, config)
                    if not analyzed:
                        continue
                    if analyzed.get("error"):
                        failures.append(analyzed)
                        continue
                    for match in analyzed["matches"]:
                        results.append(
                            {
                                "ticker": analyzed["ticker"],
                                "price": analyzed["price"],
                                "change_pct": analyzed["change_pct"],
                                "avg_volume_20": analyzed["avg_volume_20"],
                                "pattern": match["pattern"],
                                "confidence": match["confidence"],
                                "reason": match["reason"],
                                "chart": match["chart"],
                            }
                        )

            with scan_tasks_lock:
                task = scan_tasks.get(task_id)
                if task is None:
                    return  # task expired/removed; abandon quietly
                task["scanned"] = min(start + len(chunk), total)
                task["results"] = list(results)
                task["failures"] = list(failures)
                task["updated_at"] = time.time()

            if start + chunk_size < total:
                time.sleep(1)  # be polite between chunks to avoid tripping rate limits

        with scan_tasks_lock:
            task = scan_tasks.get(task_id)
            if task is None:
                return
            task["status"] = "done"
            task["scanned"] = total
            task["results"] = results
            task["failures"] = failures
            task["elapsed_seconds"] = round(time.time() - task["started_at"], 2)
            task["updated_at"] = time.time()

    except Exception as exc:
        with scan_tasks_lock:
            task = scan_tasks.get(task_id)
            if task is not None:
                task["status"] = "error"
                task["error"] = str(exc)
                task["updated_at"] = time.time()


@app.route("/")
def index():
    return render_template("index.html", defaults=DEFAULT_SCAN_CONFIG)


@app.route("/api/sheets")
def api_sheets():
    cleanup_tasks()
    try:
        sheets = []
        for name in sheet_names():
            sheets.append({"name": name, "count": len(load_tickers(name))})
        return jsonify({"sheets": sheets, "defaults": DEFAULT_SCAN_CONFIG})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/scan", methods=["POST"])
def api_scan():
    cleanup_tasks()
    payload = request.get_json(silent=True) or {}
    sheet_name = payload.get("sheet")
    config = parse_scan_config(payload)

    try:
        tickers = load_tickers(sheet_name)
        if not tickers:
            return jsonify({"error": "Selected sheet does not contain any tickers."}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    task_id = str(uuid.uuid4())
    now = time.time()
    with scan_tasks_lock:
        scan_tasks[task_id] = {
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "started_at": now,
            "sheet": sheet_name,
            "config": config,
            "total": len(tickers),
            "scanned": 0,
            "results": [],
            "failures": [],
            "error": None,
            "elapsed_seconds": 0,
        }

    thread = threading.Thread(target=run_scan_task, args=(task_id, tickers, config), daemon=True)
    thread.start()

    # Returns immediately with a task_id. The actual scan runs in the
    # background thread above; the frontend polls /api/scan/status/<task_id>
    # for progress instead of waiting on one long HTTP request.
    return jsonify({"task_id": task_id, "sheet": sheet_name, "total": len(tickers)}), 202


@app.route("/api/scan/status/<task_id>")
def api_scan_status(task_id):
    cleanup_tasks()
    with scan_tasks_lock:
        task = scan_tasks.get(task_id)
        if not task:
            return jsonify({"error": "Scan result expired or was not found."}), 404

        elapsed = task.get("elapsed_seconds") or round(time.time() - task["started_at"], 2)
        failures = task.get("failures", [])
        return jsonify(
            {
                "task_id": task_id,
                "status": task["status"],
                "total": task["total"],
                "scanned": task["scanned"],
                "results": task["results"],
                "failures": failures[:25],
                "failure_count": len(failures),
                "error": task.get("error"),
                "elapsed_seconds": elapsed,
            }
        )


@app.route("/api/export/<task_id>", methods=["POST"])
def api_export(task_id):
    cleanup_tasks()
    with scan_tasks_lock:
        task = scan_tasks.get(task_id)
        if not task:
            return jsonify({"error": "Scan result expired or was not found."}), 404
        default_rows = task.get("results", [])
        task["updated_at"] = time.time()

    payload = request.get_json(silent=True) or {}
    rows = payload.get("rows") or default_rows

    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(["Ticker", "Pattern", "Confidence", "Last Price", "Change %", "Average Volume 20D", "Reason"])
    for row in rows:
        writer.writerow(
            [
                row.get("ticker", ""),
                row.get("pattern", ""),
                row.get("confidence", ""),
                row.get("price", ""),
                f"{row.get('change_pct', '')}%",
                row.get("avg_volume_20", ""),
                row.get("reason", ""),
            ]
        )

    task["updated_at"] = time.time()
    filename = f"quant_scan_{task_id[:8]}_filtered.csv"
    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename={filename}"},
    )
