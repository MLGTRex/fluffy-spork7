"""
Price data fetcher — single-ticker price data gatherer for the Stage 5 monitor.

For one ticker, fetches:
  - Current price (last traded price)
  - Today's open / high / low / current price
  - Today's move from open
  - Yesterday's close + today's change vs yesterday
  - N days of daily historical OHLCV bars (for percentile baselines)

Does NOT classify signals, set thresholds, or decide anything. Pure data
gatherer.

Designed to be:
  - Independent: can be run standalone via CLI (`python3 priceData.py --ticker NFLX`)
  - Reusable: importable as a library by the orchestrator
  - Self-contained: doesn't reach into other Stage 5 components or upstream
    stages' caches. Only consumes the ticker symbol it's given.

Returns a consistently-shaped dict. Failures are surfaced as status fields,
not exceptions, so the orchestrator can continue with partial data.

Usage:
    from priceData import fetch_price_data
    result = fetch_price_data("NFLX")
    if result["status"] == "ok":
        print(result["current"]["price"])

Or CLI:
    python3 priceData.py --ticker NFLX
    python3 priceData.py --ticker NFLX --lookback-days 120
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone


# ============ CONFIGURATION ============

# How many days of daily history to fetch by default. Baselines need at least
# 60 days; we fetch a bit more so percentile distributions have headroom and
# the orchestrator can use the data for other purposes (cumulative-move
# trigger, etc.).
DEFAULT_LOOKBACK_DAYS = 90

# yfinance can be flaky; retry transient failures with exponential backoff.
# Same pattern used by Stage 4's price cache.
RETRY_DELAYS_SECONDS = [2, 5, 15]

# Politeness delay before each fetch (yfinance throttles aggressive callers).
# Used only when running multiple tickers via this module's batch helper; a
# single fetch doesn't apply it.
POLITENESS_DELAY_SECONDS = 0.5

# Intraday bar interval. yfinance supports: 1m (last 7 days only),
# 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d. 5m is a reasonable balance of
# granularity and history range; we only need today's bars anyway.
INTRADAY_INTERVAL = "5m"


# ============ LOGGER ============

_logger = logging.getLogger("monitor.priceData")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


# ============ RESULT SHAPE ============

def _empty_result(ticker: str, lookback_days: int) -> dict:
    """Initial result dict; populated as fetch progresses."""
    return {
        "ticker": ticker,
        "data_source": "priceData",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": "unknown",
        "current": {
            "price": None,
            "timestamp": None,
        },
        "today": {
            "open": None,
            "high": None,
            "low": None,
            "close_so_far": None,
            "move_from_open_pct": None,
            "move_from_open_abs": None,
        },
        "previous_close": None,
        "daily_change_pct": None,
        "daily_change_abs": None,
        "historical": {
            "daily_bars": [],
            "lookback_days_requested": lookback_days,
            "lookback_days_received": 0,
        },
        "data_quality_flags": [],
    }


# ============ YFINANCE FETCHING ============

def _import_yfinance():
    """Lazy import so the module loads cleanly even if yfinance isn't installed."""
    try:
        import yfinance as yf
        return yf
    except ImportError as e:
        raise ImportError(
            "yfinance is required for priceData but is not installed. "
            "Install with: pip install yfinance"
        ) from e


def _fetch_daily_history(ticker_obj, lookback_days: int) -> tuple:
    """
    Fetch lookback_days of daily OHLCV bars for this ticker via yfinance.

    Returns (bars_list, error_str_or_None).
    bars_list is in chronological order, oldest first. Each entry:
        {"date": "YYYY-MM-DD", "open": float, "high": float, "low": float,
         "close": float, "volume": int}
    """
    # yfinance's `period` syntax accepts strings like "90d" or "60d". For
    # values larger than 60d we use the "Xd" format which works fine.
    period_str = f"{lookback_days}d"

    last_error = None
    for attempt_idx, delay in enumerate([0] + RETRY_DELAYS_SECONDS):
        if delay > 0:
            time.sleep(delay)
        try:
            hist = ticker_obj.history(period=period_str, interval="1d",
                                       auto_adjust=False, raise_errors=True)
        except Exception as e:
            last_error = f"yfinance daily history fetch raised: {e}"
            _logger.warning(
                f"Daily history fetch attempt {attempt_idx + 1} failed: {e}"
            )
            continue

        if hist.empty:
            last_error = "yfinance returned empty daily history"
            _logger.warning(f"Daily history empty on attempt {attempt_idx + 1}")
            continue

        # Convert to list of dicts. Index is date.
        bars = []
        for date_idx, row in hist.iterrows():
            try:
                # date_idx is a Timestamp; format as YYYY-MM-DD
                date_str = date_idx.strftime("%Y-%m-%d")
                bars.append({
                    "date": date_str,
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
                })
            except (ValueError, KeyError) as e:
                _logger.warning(f"Skipping malformed daily bar at {date_idx}: {e}")
                continue

        return bars, None

    return [], last_error or "Daily history fetch failed after all retries"


def _fetch_intraday_today(ticker_obj) -> tuple:
    """
    Fetch today's intraday bars via yfinance.

    Returns (bars_list, error_str_or_None).
    bars_list is in chronological order. Each entry:
        {"timestamp": ISO, "open": float, "high": float, "low": float,
         "close": float, "volume": int}

    If markets are closed (weekend, holiday), or it's pre-open before any
    intraday data is available, returns an empty list with no error.
    """
    last_error = None
    for attempt_idx, delay in enumerate([0] + RETRY_DELAYS_SECONDS):
        if delay > 0:
            time.sleep(delay)
        try:
            # period="1d" gets only today; combined with 5m interval returns
            # today's 5-min bars. If markets are closed, may return last
            # trading day's bars; we filter to today below.
            intra = ticker_obj.history(period="1d", interval=INTRADAY_INTERVAL,
                                        auto_adjust=False, raise_errors=True)
        except Exception as e:
            last_error = f"yfinance intraday fetch raised: {e}"
            _logger.warning(
                f"Intraday fetch attempt {attempt_idx + 1} failed: {e}"
            )
            continue

        if intra.empty:
            # Not necessarily an error — could be a quiet market. Empty bars
            # is a valid state, not a fetch failure.
            return [], None

        bars = []
        for ts_idx, row in intra.iterrows():
            try:
                # Convert timestamp to ISO (with timezone if present)
                if hasattr(ts_idx, "isoformat"):
                    ts_str = ts_idx.isoformat()
                else:
                    ts_str = str(ts_idx)
                bars.append({
                    "timestamp": ts_str,
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
                })
            except (ValueError, KeyError) as e:
                _logger.warning(f"Skipping malformed intraday bar at {ts_idx}: {e}")
                continue

        return bars, None

    return [], last_error


def _fetch_current_price_fallback(ticker_obj) -> tuple:
    """
    Try to get a current price via Ticker.info / fast_info as a fallback when
    intraday data is unavailable.

    Returns (price_float_or_None, source_str, error_str_or_None).
    """
    # Try fast_info first (cheaper)
    try:
        fast = ticker_obj.fast_info
        if fast is not None:
            last_price = getattr(fast, "last_price", None)
            if last_price is not None and isinstance(last_price, (int, float)) and last_price > 0:
                return float(last_price), "fast_info.last_price", None
    except Exception as e:
        _logger.debug(f"fast_info access failed: {e}")

    # Fall back to .info (more expensive but more comprehensive)
    try:
        info = ticker_obj.info
        # Try multiple fields in order of preference
        for field in ["currentPrice", "regularMarketPrice", "previousClose"]:
            val = info.get(field) if isinstance(info, dict) else None
            if val is not None and isinstance(val, (int, float)) and val > 0:
                return float(val), f"info.{field}", None
    except Exception as e:
        return None, None, f"info/fast_info access failed: {e}"

    return None, None, "No current price available from any source"


# ============ MAIN FETCH ============

def fetch_price_data(ticker: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    """
    Fetch all price data for one ticker.

    Args:
        ticker: ticker symbol (e.g. "NFLX", "BIP", "SEK.AX")
        lookback_days: how many days of daily history to fetch

    Returns: a result dict (see module docstring for shape).

    Never raises — all failures are surfaced as status fields and data
    quality flags. Status values:
        "ok" - all data fetched successfully
        "partial" - some data fetched, some failures (still usable)
        "fetch_failed" - no usable data
    """
    result = _empty_result(ticker, lookback_days)

    try:
        yf = _import_yfinance()
    except ImportError as e:
        result["status"] = "fetch_failed"
        result["data_quality_flags"].append(str(e))
        return result

    try:
        ticker_obj = yf.Ticker(ticker)
    except Exception as e:
        result["status"] = "fetch_failed"
        result["data_quality_flags"].append(f"Could not create yfinance Ticker: {e}")
        return result

    # ---- Daily history ----
    daily_bars, daily_error = _fetch_daily_history(ticker_obj, lookback_days)
    result["historical"]["daily_bars"] = daily_bars
    result["historical"]["lookback_days_received"] = len(daily_bars)
    if daily_error:
        result["data_quality_flags"].append(f"daily_history: {daily_error}")

    # Set previous_close from the most recent daily bar (excluding today if
    # today appears in the daily history)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if daily_bars:
        # daily_bars sorted chronologically; check last bar for today
        last_bar = daily_bars[-1]
        if last_bar["date"] == today_str:
            # Today's daily bar is present — previous close is the bar before
            if len(daily_bars) >= 2:
                result["previous_close"] = daily_bars[-2]["close"]
            # Today's open/high/low/close also come from this bar if it's
            # complete (market closed). We'll override these from intraday
            # if available below.
            result["today"]["open"] = last_bar["open"]
            result["today"]["high"] = last_bar["high"]
            result["today"]["low"] = last_bar["low"]
            result["today"]["close_so_far"] = last_bar["close"]
        else:
            # Today's daily bar not yet present (e.g., during the trading day)
            result["previous_close"] = last_bar["close"]

    # ---- Intraday today ----
    intraday_bars, intraday_error = _fetch_intraday_today(ticker_obj)
    if intraday_error:
        result["data_quality_flags"].append(f"intraday: {intraday_error}")

    if intraday_bars:
        # Override today's OHLC from intraday bars (more accurate during the day)
        opens = [b["open"] for b in intraday_bars]
        highs = [b["high"] for b in intraday_bars]
        lows = [b["low"] for b in intraday_bars]
        closes = [b["close"] for b in intraday_bars]
        if opens and highs and lows and closes:
            result["today"]["open"] = opens[0]
            result["today"]["high"] = max(highs)
            result["today"]["low"] = min(lows)
            result["today"]["close_so_far"] = closes[-1]

        # Current price from latest intraday bar
        result["current"]["price"] = intraday_bars[-1]["close"]
        result["current"]["timestamp"] = intraday_bars[-1]["timestamp"]

    # ---- Current price fallback if intraday didn't give us one ----
    if result["current"]["price"] is None:
        fallback_price, fallback_source, fallback_error = _fetch_current_price_fallback(ticker_obj)
        if fallback_price is not None:
            result["current"]["price"] = fallback_price
            result["current"]["timestamp"] = datetime.now(timezone.utc).isoformat()
            result["data_quality_flags"].append(
                f"current price from fallback source: {fallback_source}"
            )
        elif fallback_error:
            result["data_quality_flags"].append(f"current_price_fallback: {fallback_error}")

    # ---- Computed fields ----
    # Today's move from open
    if (result["today"]["open"] is not None
            and result["today"]["close_so_far"] is not None
            and result["today"]["open"] != 0):
        open_p = result["today"]["open"]
        close_p = result["today"]["close_so_far"]
        result["today"]["move_from_open_abs"] = close_p - open_p
        result["today"]["move_from_open_pct"] = ((close_p - open_p) / open_p) * 100.0

    # Daily change vs previous close
    if (result["previous_close"] is not None
            and result["current"]["price"] is not None
            and result["previous_close"] != 0):
        cur = result["current"]["price"]
        prev = result["previous_close"]
        result["daily_change_abs"] = cur - prev
        result["daily_change_pct"] = ((cur - prev) / prev) * 100.0

    # ---- Determine final status ----
    has_current_price = result["current"]["price"] is not None
    has_history = result["historical"]["lookback_days_received"] > 0
    has_previous_close = result["previous_close"] is not None

    if has_current_price and has_history and has_previous_close:
        result["status"] = "ok"
    elif has_current_price or has_history:
        result["status"] = "partial"
    else:
        result["status"] = "fetch_failed"

    # Flag insufficient history for downstream baselines if we got less than
    # 60 days (the minimum needed for stable percentile baselines).
    if 0 < result["historical"]["lookback_days_received"] < 60:
        result["data_quality_flags"].append(
            f"Insufficient daily history: only {result['historical']['lookback_days_received']} "
            f"days received (60+ recommended for stable baselines)"
        )

    return result


# ============ CLI ============

def _cli():
    parser = argparse.ArgumentParser(description="Fetch price data for a ticker")
    parser.add_argument("--ticker", required=True, help="Ticker symbol (e.g. NFLX)")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                        help=f"Days of daily history (default {DEFAULT_LOOKBACK_DAYS})")
    parser.add_argument("--brief", action="store_true",
                        help="Omit daily_bars from output (just show summary)")
    args = parser.parse_args()

    result = fetch_price_data(args.ticker, args.lookback_days)

    if args.brief:
        result = dict(result)
        result["historical"] = {
            k: v if k != "daily_bars" else f"<{len(v)} bars omitted>"
            for k, v in result["historical"].items()
        }

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] in ("ok", "partial") else 1


if __name__ == "__main__":
    sys.exit(_cli())