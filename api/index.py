import csv
import io
import os
import time
from pathlib import Path

import pandas as pd
import yfinance as yf
from flask import Flask, Response, jsonify, render_template, request


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "ScannerData.xlsx"

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "quant_scanner_dev_secret")

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


def fetch_history(ticker, period):
    """Fetches one ticker's history at a time via yf.Ticker().history().

    This mirrors the pattern used by a known-working reference deployment:
    single-ticker requests are far less likely to be rate-limited/blocked by
    Yahoo Finance than yf.download() calls that join many tickers into one
    batch request — which is what previously caused 'Expecting value: line 1
    column 1' errors on Render. A single quick retry covers transient blips;
    consistent failures usually mean Yahoo is rejecting the request outright,
    so retrying further wouldn't help — analyze_ticker treats no data as
    "no match" rather than a hard error, same as the working reference app.
    """
    for attempt in range(2):
        try:
            df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
        except Exception:
            df = None

        if df is not None and not df.empty:
            return df

        if attempt == 0:
            time.sleep(0.3)

    return None


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


@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template("index.html", defaults=DEFAULT_SCAN_CONFIG)


@app.route("/api/sheets")
def api_sheets():
    try:
        sheets = []
        for name in sheet_names():
            sheets.append({"name": name, "count": len(load_tickers(name))})
        return jsonify({"sheets": sheets, "defaults": DEFAULT_SCAN_CONFIG})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/tickers")
def api_tickers():
    sheet_name = request.args.get("sheet")
    try:
        tickers = load_tickers(sheet_name)
        if not tickers:
            return jsonify({"error": "Selected sheet does not contain any tickers."}), 400
        return jsonify({"sheet": sheet_name, "tickers": tickers, "total": len(tickers)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/scan_one", methods=["POST"])
def api_scan_one():
    """Scans exactly one ticker per request. The frontend calls this in a
    sequential loop (one ticker at a time, never in parallel) instead of
    one backend call doing a multi-ticker yf.download() batch. This mirrors
    a known-working deployment pattern: single-ticker yf.Ticker().history()
    requests, paced by the natural request/response cycle, are far less
    likely to trigger Yahoo Finance's rate-limiting/blocking than bursty
    multi-ticker batch downloads — which is what caused scans to get stuck
    or return empty responses on Render before."""
    payload = request.get_json(silent=True) or {}
    ticker = str(payload.get("ticker", "")).strip().upper()
    if not ticker:
        return jsonify({"error": "Ticker is required."}), 400

    config = parse_scan_config(payload)

    try:
        df = fetch_history(ticker, config["lookback_period"])
        if df is None:
            return jsonify({"ticker": ticker, "rows": []})

        analyzed = analyze_ticker(ticker, df, config)
        if not analyzed:
            return jsonify({"ticker": ticker, "rows": []})
        if analyzed.get("error"):
            return jsonify({"ticker": ticker, "rows": [], "warning": analyzed["error"]})

        rows = [
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
            for match in analyzed["matches"]
        ]
        return jsonify({"ticker": ticker, "rows": rows})
    except Exception as exc:
        # Non-fatal: the frontend logs this as a warning and moves on to the
        # next ticker, instead of aborting the whole scan.
        return jsonify({"ticker": ticker, "rows": [], "warning": str(exc)})


@app.route("/api/export", methods=["POST"])
def api_export():
    payload = request.get_json(silent=True) or {}
    rows = payload.get("rows") or []
    sheet_name = payload.get("sheet") or "scan"

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

    safe_sheet = "".join(c if c.isalnum() else "_" for c in str(sheet_name))[:40] or "scan"
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"quant_scan_{safe_sheet}_{timestamp}.csv"
    return Response(
        si.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename={filename}"},
    )
