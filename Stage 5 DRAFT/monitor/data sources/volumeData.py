"""
Volume data fetcher — single-ticker volume data gatherer for the Stage 5 monitor.

For one ticker, fetches:
  - Today's volume so far (sum of intraday bars during the day, or daily bar
    volume after close)
  - Yesterday's volume (the most recent completed trading day)
  - N days of daily historical volume (for percentile baselines)

Volume signals are useful because unusual volume often precedes price moves
and provides corroboration for price-based triggers. A 5% move on 3x normal
volume is materially different from a 5% move on routine volume.

Does NOT classify signals, set thresholds, or decide anything. Pure data
gatherer.

Designed to be:
  - Independent: can be run standalone via CLI
  - Reusable: importable as a library by the orchestrator
  - Self-contained: only consumes the ticker symbol it's given

Returns a consistently-shaped dict. Failures surface as status fields, not
exceptions.

Usage:
    from volumeData import fetch_volume_data
    result = fetch_volume_data("NFLX")
    if result["status"] == "ok":
        print(result["today"]["volume_so_far"])

CLI:
    python3 volumeData.py --ticker NFLX
    python3 volumeData.py --ticker NFLX --lookback-days 120
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone


# ============ CONFIGURATION ============

DEFAULT_LOOKBACK_DAYS = 90
RETRY_DELAYS_SECONDS = [2, 5, 15]
INTRADAY_INTERVAL = "5m"


# ============ LOGGER ============

_logger = logging.getLogger("monitor.volumeData")
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
    return {
        "ticker": ticker,
        "data_source": "volumeData",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": "unknown",
        "today": {
            "volume_so_far": None,
            "is_partial_day": None,  # True if intraday-only, False if full daily bar present
        },
        "previous_day": {
            "date": None,
            "volume": None,
        },
        "change_vs_previous_pct": None,  # today's volume relative to yesterday's
        "historical": {
            "daily_volumes": [],  # [{"date": "YYYY-MM-DD", "volume": int}, ...]
            "lookback_days_requested": lookback_days,
            "lookback_days_received": 0,
        },
        "data_quality_flags": [],
    }


# ============ YFINANCE FETCHING ============

def _import_yfinance():
    try:
        import yfinance as yf
        return yf
    except ImportError as e:
        raise ImportError(
            "yfinance is required for volumeData but is not installed. "
            "Install with: pip install yfinance"
        ) from e


def _fetch_daily_history(ticker_obj, lookback_days: int) -> tuple:
    """
    Fetch lookback_days of daily volume bars via yfinance.

    Returns (bars_list, error_str_or_None).
    bars_list is chronological, oldest first:
        {"date": "YYYY-MM-DD", "volume": int, "close": float}

    We keep close alongside volume because some downstream uses (e.g.,
    correlating volume with prior-close-to-open gaps) benefit from it.
    """
    period_str = f"{lookback_days}d"

    last_error = None
    for attempt_idx, delay in enumerate([0] + RETRY_DELAYS_SECONDS):
        if delay > 0:
            time.sleep(delay)
        try:
            hist = ticker_obj.history(period=period_str, interval="1d",
                                       auto_adjust=False, raise_errors=True)
        except Exception as e:
            last_error = f"yfinance daily volume fetch raised: {e}"
            _logger.warning(
                f"Daily volume fetch attempt {attempt_idx + 1} failed: {e}"
            )
            continue

        if hist.empty:
            last_error = "yfinance returned empty daily history"
            _logger.warning(f"Daily volume empty on attempt {attempt_idx + 1}")
            continue

        bars = []
        for date_idx, row in hist.iterrows():
            try:
                date_str = date_idx.strftime("%Y-%m-%d")
                volume = row["Volume"]
                # NaN check (yfinance sometimes returns NaN volume for holidays etc.)
                if volume != volume:  # NaN test
                    continue
                bars.append({
                    "date": date_str,
                    "volume": int(volume),
                    "close": float(row["Close"]),
                })
            except (ValueError, KeyError) as e:
                _logger.warning(f"Skipping malformed daily bar at {date_idx}: {e}")
                continue

        return bars, None

    return [], last_error or "Daily volume fetch failed after all retries"


def _fetch_intraday_today(ticker_obj) -> tuple:
    """
    Fetch today's intraday bars to compute volume-so-far during the trading day.

    Returns (bars_list, error_str_or_None).
        {"timestamp": ISO, "volume": int}

    Empty list with no error means markets are closed / no bars yet —
    not a fetch failure.
    """
    last_error = None
    for attempt_idx, delay in enumerate([0] + RETRY_DELAYS_SECONDS):
        if delay > 0:
            time.sleep(delay)
        try:
            intra = ticker_obj.history(period="1d", interval=INTRADAY_INTERVAL,
                                        auto_adjust=False, raise_errors=True)
        except Exception as e:
            last_error = f"yfinance intraday volume fetch raised: {e}"
            _logger.warning(
                f"Intraday volume fetch attempt {attempt_idx + 1} failed: {e}"
            )
            continue

        if intra.empty:
            return [], None

        bars = []
        for ts_idx, row in intra.iterrows():
            try:
                ts_str = ts_idx.isoformat() if hasattr(ts_idx, "isoformat") else str(ts_idx)
                volume = row["Volume"]
                if volume != volume:  # NaN
                    continue
                bars.append({
                    "timestamp": ts_str,
                    "volume": int(volume),
                })
            except (ValueError, KeyError) as e:
                _logger.warning(f"Skipping malformed intraday bar at {ts_idx}: {e}")
                continue

        return bars, None

    return [], last_error


# ============ MAIN FETCH ============

def fetch_volume_data(ticker: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    """
    Fetch all volume data for one ticker.

    Args:
        ticker: ticker symbol
        lookback_days: how many days of daily volume history to fetch

    Returns: a result dict (see module docstring for shape).

    Never raises. Failures appear in status + data_quality_flags.
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
    result["historical"]["daily_volumes"] = [
        {"date": b["date"], "volume": b["volume"]} for b in daily_bars
    ]
    result["historical"]["lookback_days_received"] = len(daily_bars)
    if daily_error:
        result["data_quality_flags"].append(f"daily_history: {daily_error}")

    # Determine if today's bar is in daily history
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_in_daily = False
    if daily_bars:
        last_bar = daily_bars[-1]
        if last_bar["date"] == today_str:
            today_in_daily = True
            # Today's daily bar is complete — use it for today's volume
            result["today"]["volume_so_far"] = last_bar["volume"]
            result["today"]["is_partial_day"] = False
            # Previous day is the bar before
            if len(daily_bars) >= 2:
                result["previous_day"]["date"] = daily_bars[-2]["date"]
                result["previous_day"]["volume"] = daily_bars[-2]["volume"]
        else:
            # Today's daily bar not present (during market hours or pre-open)
            result["previous_day"]["date"] = last_bar["date"]
            result["previous_day"]["volume"] = last_bar["volume"]

    # ---- Intraday today (only if we need it) ----
    if not today_in_daily:
        intraday_bars, intraday_error = _fetch_intraday_today(ticker_obj)
        if intraday_error:
            result["data_quality_flags"].append(f"intraday: {intraday_error}")

        if intraday_bars:
            # Sum intraday volumes for today's volume-so-far
            total_intraday_volume = sum(b["volume"] for b in intraday_bars)
            result["today"]["volume_so_far"] = total_intraday_volume
            result["today"]["is_partial_day"] = True

    # ---- Computed fields ----
    if (result["previous_day"]["volume"] is not None
            and result["today"]["volume_so_far"] is not None
            and result["previous_day"]["volume"] != 0):
        today_vol = result["today"]["volume_so_far"]
        prev_vol = result["previous_day"]["volume"]
        result["change_vs_previous_pct"] = ((today_vol - prev_vol) / prev_vol) * 100.0

    # ---- Determine final status ----
    has_today_volume = result["today"]["volume_so_far"] is not None
    has_history = result["historical"]["lookback_days_received"] > 0
    has_previous = result["previous_day"]["volume"] is not None

    if has_today_volume and has_history and has_previous:
        result["status"] = "ok"
    elif has_today_volume or has_history:
        result["status"] = "partial"
    else:
        result["status"] = "fetch_failed"

    # Flag insufficient history
    if 0 < result["historical"]["lookback_days_received"] < 60:
        result["data_quality_flags"].append(
            f"Insufficient daily history: only {result['historical']['lookback_days_received']} "
            f"days received (60+ recommended for stable baselines)"
        )

    return result


# ============ CLI ============

def _cli():
    parser = argparse.ArgumentParser(description="Fetch volume data for a ticker")
    parser.add_argument("--ticker", required=True, help="Ticker symbol (e.g. NFLX)")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                        help=f"Days of daily history (default {DEFAULT_LOOKBACK_DAYS})")
    parser.add_argument("--brief", action="store_true",
                        help="Omit daily_volumes from output")
    args = parser.parse_args()

    result = fetch_volume_data(args.ticker, args.lookback_days)

    if args.brief:
        result = dict(result)
        result["historical"] = {
            k: v if k != "daily_volumes" else f"<{len(v)} bars omitted>"
            for k, v in result["historical"].items()
        }

    print(json.dumps(result, indent=2, default=str))
    return 0 if result["status"] in ("ok", "partial") else 1


if __name__ == "__main__":
    sys.exit(_cli())