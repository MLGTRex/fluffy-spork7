"""
Benchmark history: daily SPY bars from Alpaca Market Data, covering the same
date range as the portfolio history equity series.

Falls back to cache/benchmark_bars.json on failure.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import config

logger = logging.getLogger(__name__)


def _build_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    api_key = os.getenv(config.ALPACA_API_KEY_ENV)
    secret = os.getenv(config.ALPACA_SECRET_KEY_ENV)
    if not api_key or not secret:
        raise RuntimeError(
            f"{config.ALPACA_API_KEY_ENV}/{config.ALPACA_SECRET_KEY_ENV} not set"
        )
    return StockHistoricalDataClient(api_key, secret)


def _read_cache() -> Optional[dict]:
    path = config.BENCHMARK_CACHE
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Could not parse benchmark cache: %s", e)
        return None


def _write_cache(payload: dict) -> None:
    os.makedirs(os.path.dirname(config.BENCHMARK_CACHE), exist_ok=True)
    tmp = config.BENCHMARK_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, config.BENCHMARK_CACHE)


def _resolve_range(portfolio_history: Optional[dict]) -> tuple[datetime, datetime]:
    """
    Pick a date range that covers the portfolio_history series. Falls back to
    a 1-year window ending today if portfolio_history is absent.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)  # default fallback window

    if portfolio_history:
        ts_list = portfolio_history.get("timestamp") or []
        if ts_list:
            first = ts_list[0]
            try:
                # Alpaca's portfolio_history.timestamp is unix seconds
                if isinstance(first, (int, float)):
                    start = datetime.fromtimestamp(int(first), tz=timezone.utc)
                elif isinstance(first, str):
                    # ISO string fallback
                    start = datetime.fromisoformat(first.replace("Z", "+00:00"))
            except Exception:
                pass

    # Pad a few days to be safe
    start = start - timedelta(days=2)
    return start, end


def fetch_benchmark(portfolio_history: Optional[dict]) -> dict:
    """
    Returns:
        {
            "fetched_at": ISO,
            "symbol": "SPY",
            "bars": [{"date": "YYYY-MM-DD", "close": float}, ...] or None,
            "stale": bool,
            "errors": [...],
        }
    """
    now = datetime.now(timezone.utc).isoformat()
    result = {
        "fetched_at": now,
        "symbol": config.BENCHMARK_SYMBOL,
        "bars": None,
        "stale": False,
        "errors": [],
    }

    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        client = _build_data_client()
    except Exception as e:
        logger.warning("Could not build market-data client: %s — using cache", e)
        result["errors"].append(f"client_build_failed: {e}")
        cached = _read_cache() or {}
        result["bars"] = cached.get("bars")
        result["stale"] = True
        return result

    start, end = _resolve_range(portfolio_history)

    try:
        req = StockBarsRequest(
            symbol_or_symbols=config.BENCHMARK_SYMBOL,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars_response = client.get_stock_bars(req)
        # bars_response.data is {symbol: [Bar, ...]}; pull our symbol
        raw_bars = bars_response.data.get(config.BENCHMARK_SYMBOL, [])
        bars = []
        for b in raw_bars:
            ts = getattr(b, "timestamp", None)
            close = getattr(b, "close", None)
            if ts is None or close is None:
                continue
            if isinstance(ts, datetime):
                date_str = ts.astimezone(timezone.utc).date().isoformat()
            else:
                date_str = str(ts)[:10]
            bars.append({"date": date_str, "close": float(close)})
        result["bars"] = bars
        _write_cache({
            "fetched_at": now,
            "symbol": config.BENCHMARK_SYMBOL,
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "bars": bars,
        })
    except Exception as e:
        logger.warning("Benchmark fetch failed: %s — using cache", e)
        result["errors"].append(str(e))
        cached = _read_cache() or {}
        result["bars"] = cached.get("bars")
        result["stale"] = True

    return result
