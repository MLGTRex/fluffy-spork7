"""
Earnings calendar fetcher — per-ticker earnings date data for the Stage 5 monitor.

For one ticker, fetches:
  - Historical earnings dates (for baselines to exclude these days from
    percentile distributions, since earnings-day moves have known cause)
  - Upcoming earnings date (informational context for the LLM gate)

Used primarily by the percentile baseline computation to filter out earnings
days. Earnings days are known to be high-volatility events with known cause;
including them in the "normal day" distribution distorts the thresholds and
causes the watcher to under-trigger on truly anomalous non-earnings moves.

The percentile baseline should be computed from "normal day" distributions
only. This fetcher provides the list of dates to exclude.

yfinance provides earnings dates via Ticker.earnings_dates and Ticker.calendar.
Both can return empty / None for various reasons (ASX tickers, delisted, etc.)
so failures are surfaced as data quality flags rather than errors.

Designed to be:
  - Independent: standalone via CLI
  - Reusable: importable by orchestrator
  - Self-contained: only consumes the ticker symbol

Usage:
    from earningsCalendar import fetch_earnings_calendar
    result = fetch_earnings_calendar("NFLX")
    if result["status"] == "ok":
        historical_dates = result["historical_earnings_dates"]

CLI:
    python3 earningsCalendar.py --ticker NFLX
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta


# ============ CONFIGURATION ============

# How many earnings dates back to fetch. yfinance's earnings_dates limit is
# limit-keyword controllable; defaults to roughly the past 4 quarters + next
# few. We ask for more so baselines can exclude all earnings days in a
# typical 90-day lookback (1-2 earnings) and beyond.
DEFAULT_EARNINGS_LIMIT = 12  # last ~3 years of quarterly earnings

RETRY_DELAYS_SECONDS = [2, 5, 15]


# ============ LOGGER ============

_logger = logging.getLogger("monitor.earningsCalendar")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


# ============ RESULT SHAPE ============

def _empty_result(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "data_source": "earningsCalendar",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "status": "unknown",
        # All historical earnings dates we found (YYYY-MM-DD strings).
        # Used by baselines to exclude these days from percentile distributions.
        "historical_earnings_dates": [],
        # The next upcoming earnings date if known (YYYY-MM-DD or None)
        "next_earnings_date": None,
        # Whether next_earnings_date is within the next 7 days (informational
        # context for the LLM gate)
        "earnings_imminent": False,
        "data_quality_flags": [],
    }


# ============ YFINANCE FETCHING ============

def _import_yfinance():
    try:
        import yfinance as yf
        return yf
    except ImportError as e:
        raise ImportError(
            "yfinance is required for earningsCalendar but is not installed. "
            "Install with: pip install yfinance"
        ) from e


def _fetch_earnings_dates(ticker_obj, limit: int) -> tuple:
    """
    Fetch earnings dates DataFrame from yfinance Ticker.earnings_dates.

    yfinance's earnings_dates can return None or empty for some tickers
    (especially ASX). Returns (list_of_date_strings, error_str_or_None).

    The list is in chronological order (oldest first). All dates are
    YYYY-MM-DD strings. Both past and future earnings dates are included.
    """
    last_error = None
    for attempt_idx, delay in enumerate([0] + RETRY_DELAYS_SECONDS):
        if delay > 0:
            time.sleep(delay)
        try:
            ed = ticker_obj.earnings_dates
        except Exception as e:
            last_error = f"earnings_dates access raised: {e}"
            _logger.warning(
                f"earnings_dates attempt {attempt_idx + 1} failed: {e}"
            )
            continue

        if ed is None:
            last_error = "earnings_dates returned None"
            return [], last_error  # no point retrying a None return

        # yfinance returns a DataFrame indexed by Timestamp
        try:
            is_empty = ed.empty
        except AttributeError:
            return [], "earnings_dates returned non-DataFrame"

        if is_empty:
            return [], None  # No earnings dates available, not an error

        # Extract dates from the index
        dates = []
        try:
            for ts in ed.index:
                try:
                    date_str = ts.strftime("%Y-%m-%d")
                    dates.append(date_str)
                except (AttributeError, ValueError):
                    continue
        except Exception as e:
            return [], f"Iterating earnings_dates index failed: {e}"

        # Sort chronologically and dedupe
        dates = sorted(set(dates))
        # Optionally truncate to limit most recent (limit applies to total
        # not just past, but in practice yfinance returns limited data anyway)
        if len(dates) > limit * 2:
            # Keep the most recent (limit * 2) entries, balancing past and future
            dates = dates[-(limit * 2):]

        return dates, None

    return [], last_error or "earnings_dates fetch failed after all retries"


def _split_past_and_future(dates: list) -> tuple:
    """
    Split a chronological list of date strings into (past, future_or_today).

    Today is considered future (the earnings event hasn't fully "happened"
    until the market processes it).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    past = [d for d in dates if d < today]
    future = [d for d in dates if d >= today]
    return past, future


def _is_imminent(next_earnings_date: str, days_threshold: int = 7) -> bool:
    """True if next_earnings_date is within days_threshold days from today."""
    if not next_earnings_date:
        return False
    try:
        next_dt = datetime.strptime(next_earnings_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = next_dt - now
        return 0 <= delta.days <= days_threshold
    except ValueError:
        return False


# ============ MAIN FETCH ============

def fetch_earnings_calendar(ticker: str, limit: int = DEFAULT_EARNINGS_LIMIT) -> dict:
    """
    Fetch earnings calendar data for one ticker.

    Args:
        ticker: ticker symbol
        limit: rough cap on number of earnings dates to retain

    Returns: a result dict (see module docstring for shape).

    Never raises. Failures surface as status + data_quality_flags. For ASX
    tickers or delisted names that may not have earnings data available,
    status='partial' or 'no_data' with appropriate flags.
    """
    result = _empty_result(ticker)

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

    dates, error = _fetch_earnings_dates(ticker_obj, limit)
    if error:
        result["data_quality_flags"].append(f"earnings_dates: {error}")

    if not dates:
        # No data available — common for some tickers, not necessarily a
        # failure. Baselines will fall back to including all days in the
        # distribution.
        result["status"] = "no_data" if not error else "fetch_failed"
        if not error:
            result["data_quality_flags"].append(
                "No earnings dates available for this ticker (common for ASX, "
                "ETFs, recently listed names). Baselines will not exclude any "
                "days for this ticker."
            )
        return result

    past, future = _split_past_and_future(dates)
    result["historical_earnings_dates"] = past
    if future:
        result["next_earnings_date"] = future[0]
        result["earnings_imminent"] = _is_imminent(future[0])

    result["status"] = "ok"
    return result


# ============ CLI ============

def _cli():
    parser = argparse.ArgumentParser(description="Fetch earnings calendar for a ticker")
    parser.add_argument("--ticker", required=True, help="Ticker symbol")
    parser.add_argument("--limit", type=int, default=DEFAULT_EARNINGS_LIMIT,
                        help=f"Earnings date limit (default {DEFAULT_EARNINGS_LIMIT})")
    args = parser.parse_args()

    result = fetch_earnings_calendar(args.ticker, args.limit)
    print(json.dumps(result, indent=2, default=str))
    # Exit 0 for ok or no_data (no_data isn't a failure for some tickers)
    return 0 if result["status"] in ("ok", "no_data") else 1


if __name__ == "__main__":
    sys.exit(_cli())