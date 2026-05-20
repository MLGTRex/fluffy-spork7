"""
Prediction-accuracy comparison.

Joins each prediction log entry against the actual closing price of the ticker
at each horizon (1m/3m/6m/12m). Uses Alpaca historical bars for the per-ticker
lookup, batched once per ticker per run.

Pending forecasts (horizon not yet reached) go into a `pending` array so the
UI can show them as in-flight.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, date as date_cls
from typing import Optional

import config

logger = logging.getLogger(__name__)


# ============ Bar fetch (batched per ticker) ============

def _build_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    api_key = os.getenv(config.ALPACA_API_KEY_ENV)
    secret = os.getenv(config.ALPACA_SECRET_KEY_ENV)
    if not api_key or not secret:
        raise RuntimeError("Alpaca credentials not set")
    return StockHistoricalDataClient(api_key, secret)


def _fetch_bars(client, tickers: list[str], start: date_cls, end: date_cls) -> dict[str, dict]:
    """Returns {ticker: {date_iso: close}}."""
    if not tickers:
        return {}
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    req = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame.Day,
        start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
        end=datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc),
    )
    resp = client.get_stock_bars(req)
    out: dict[str, dict] = {t: {} for t in tickers}
    raw_data = getattr(resp, "data", {}) or {}
    for sym, bars in raw_data.items():
        for b in bars:
            ts = getattr(b, "timestamp", None)
            close = getattr(b, "close", None)
            if ts is None or close is None:
                continue
            if isinstance(ts, datetime):
                d = ts.astimezone(timezone.utc).date().isoformat()
            else:
                d = str(ts)[:10]
            out.setdefault(sym, {})[d] = float(close)
    return out


def _closest_close_at_or_before(closes: dict, target_iso: str) -> Optional[float]:
    """Returns the close on the latest date <= target. None if none qualify."""
    if not closes:
        return None
    candidates = sorted(d for d in closes if d <= target_iso)
    if not candidates:
        return None
    return closes[candidates[-1]]


# ============ Aggregation helpers ============

def _scenario_bucket(actual_return: float, expected_returns: dict, prices: dict) -> Optional[str]:
    """
    Map the realised 12m return into bull / base / bear / below_bear by
    comparing actual to the 12m bull/base/bear price targets.
    (Note: we use returns here, but bucket boundaries are derived from the
    expected_returns dict which itself was computed by Python from the price
    targets, so it's a consistent mapping.)
    """
    bull = expected_returns.get("12m")  # this is probability-weighted, so a
    # cleaner bucketing is by raw upside/base/downside returns. Stage 6 doesn't
    # have those at the per-entry level, so just compare to the prob-weighted
    # expected return as a coarse signal.
    if bull is None:
        return None
    # Coarse buckets — refine when upside/base/downside per-entry is added
    if actual_return is None:
        return None
    if actual_return >= bull * 1.25:
        return "bull"
    if actual_return >= bull * 0.5:
        return "base"
    if actual_return >= -abs(bull):
        return "bear"
    return "below_bear"


# ============ Entry point ============

def compute(prediction_log_entries: list[dict]) -> dict:
    """
    Returns:
        {
            "as_of": ISO,
            "evaluated": [{ticker, logged_at, entry_date, horizon, expected_return,
                           actual_return, error, conviction,
                           scenario_probabilities, realised_scenario_bucket}],
            "pending": [{ticker, entry_date, horizon, horizon_target_date}],
            "aggregate": {
                "by_horizon": {...},
                "by_conviction": {...},
                "n_evaluated": int,
                "n_pending": int,
            },
            "fetch_errors": [],
            "stale": bool,   # true if Alpaca couldn't be reached
        }
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    result = {
        "as_of": now.isoformat(),
        "evaluated": [],
        "pending": [],
        "aggregate": {
            "by_horizon": {},
            "by_conviction": {},
            "n_evaluated": 0,
            "n_pending": 0,
        },
        "fetch_errors": [],
        "stale": False,
    }

    if not prediction_log_entries:
        return result

    # Identify tickers + earliest entry_date to bound the bar fetch
    tickers: set[str] = set()
    earliest: Optional[date_cls] = None
    for e in prediction_log_entries:
        t = e.get("ticker")
        ed = e.get("entry_date")
        if t:
            tickers.add(t)
        if ed:
            try:
                d = datetime.fromisoformat(ed).date()
                earliest = d if (earliest is None or d < earliest) else earliest
            except Exception:
                pass

    bars_by_ticker: dict[str, dict] = {t: {} for t in tickers}
    if tickers and earliest:
        try:
            client = _build_data_client()
            bars_by_ticker = _fetch_bars(client, sorted(tickers), earliest, today)
        except Exception as e:
            logger.warning("Bar fetch for prediction accuracy failed: %s", e)
            result["fetch_errors"].append(str(e))
            result["stale"] = True

    # Walk entries × horizons
    by_horizon_acc: dict[str, list[float]] = {"1m": [], "3m": [], "6m": [], "12m": []}
    by_conviction_acc: dict[str, list[float]] = {}

    for entry in prediction_log_entries:
        ticker = entry.get("ticker")
        entry_date = entry.get("entry_date")
        entry_price = entry.get("entry_price")
        horizons = entry.get("horizon_target_dates") or {}
        expected = entry.get("expected_returns") or {}
        scen_probs = entry.get("scenario_probabilities") or {}
        conviction = entry.get("conviction") or "unspecified"
        if not ticker or not entry_date or entry_price in (None, 0):
            continue

        ticker_bars = bars_by_ticker.get(ticker, {})
        for h in ("1m", "3m", "6m", "12m"):
            target_date_str = horizons.get(h)
            if not target_date_str:
                continue
            try:
                target_date = datetime.fromisoformat(target_date_str).date()
            except Exception:
                continue

            if target_date > today:
                result["pending"].append({
                    "ticker": ticker,
                    "entry_date": entry_date,
                    "horizon": h,
                    "horizon_target_date": target_date_str,
                    "expected_return": expected.get(h),
                    "conviction": conviction,
                })
                continue

            actual_close = _closest_close_at_or_before(ticker_bars, target_date_str)
            if actual_close is None:
                continue
            try:
                actual_return = (actual_close / float(entry_price)) - 1.0
            except Exception:
                continue
            expected_h = expected.get(h)
            error = (
                (actual_return - expected_h) if (expected_h is not None) else None
            )
            evaluated_entry = {
                "prediction_id": entry.get("prediction_id"),
                "ticker": ticker,
                "logged_at": entry.get("logged_at"),
                "entry_date": entry_date,
                "horizon": h,
                "horizon_target_date": target_date_str,
                "entry_price": entry_price,
                "actual_price": actual_close,
                "actual_return": actual_return,
                "expected_return": expected_h,
                "error": error,
                "conviction": conviction,
                "scenario_probabilities": scen_probs,
                "realised_scenario_bucket": _scenario_bucket(
                    actual_return, expected, ticker_bars
                ) if h == "12m" else None,
            }
            result["evaluated"].append(evaluated_entry)
            if error is not None:
                by_horizon_acc[h].append(error)
                by_conviction_acc.setdefault(conviction, []).append(error)

    def _summarise(errs: list[float]) -> dict:
        if not errs:
            return {"n": 0, "mean_error": None, "mean_abs_error": None}
        return {
            "n": len(errs),
            "mean_error": sum(errs) / len(errs),
            "mean_abs_error": sum(abs(e) for e in errs) / len(errs),
        }

    result["aggregate"]["by_horizon"] = {
        h: _summarise(by_horizon_acc[h]) for h in by_horizon_acc
    }
    result["aggregate"]["by_conviction"] = {
        c: _summarise(errs) for c, errs in by_conviction_acc.items()
    }
    result["aggregate"]["n_evaluated"] = len(result["evaluated"])
    result["aggregate"]["n_pending"] = len(result["pending"])
    return result
