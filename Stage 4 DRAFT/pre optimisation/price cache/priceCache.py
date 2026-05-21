"""
Price cache for Stage 4 pre-optimization.

Maintains a local cache of daily historical prices for:
    - Stage 3 candidate companies (read from /stage 3 DRAFT/output/)
    - 7 macro factor proxies (defined in macroAnalysis.py's FACTOR_PROXIES dict)

The cache uses one CSV per ticker at /stage 4 DRAFT/cache/prices/{ticker}.csv.
Each CSV has columns: Date, Close (split-adjusted via yfinance's default auto_adjust=True).

On each run:
    - If cache for a ticker doesn't exist: fetch full 3y history
    - If cache exists and last date < today: fetch incremental data, append
    - If last date == today: skip

The cache is shared by correlationAnalysis.py and macroAnalysis.py.
"""

import logging
import os
import time
import random
from datetime import datetime, timedelta, timezone
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ============ CONFIG ============

LOOKBACK_YEARS = 3
POLITENESS_DELAY_SECONDS = 0.5
RETRY_DELAYS = [2, 5, 15, 30, 60]


# ============ HELPERS ============

def _safe_ticker_filename(ticker: str) -> str:
    """Convert a ticker to a safe filename (handles ^TNX, CL=F, etc.)."""
    return ticker.replace("/", "_").replace("\\", "_")


def _cache_path(ticker: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"{_safe_ticker_filename(ticker)}.csv")


def _load_cached_prices(ticker: str, cache_dir: str) -> pd.DataFrame:
    """Load cached prices for one ticker. Returns empty DataFrame if not cached."""
    path = _cache_path(ticker, cache_dir)
    if not os.path.exists(path):
        return pd.DataFrame(columns=["Date", "Close"])
    try:
        df = pd.read_csv(path, parse_dates=["Date"])
        return df
    except Exception as e:
        logger.warning(f"[{ticker}] failed to read cache at {path}: {e}; treating as empty")
        return pd.DataFrame(columns=["Date", "Close"])


def _save_cached_prices(ticker: str, df: pd.DataFrame, cache_dir: str):
    """Write cached prices for one ticker."""
    path = _cache_path(ticker, cache_dir)
    df.to_csv(path, index=False)


def _fetch_history_with_retry(ticker: str, start_date: str = None, period: str = None) -> pd.DataFrame:
    """
    Fetch price history from yfinance with retry-on-rate-limit logic.
    Provide either start_date (for incremental) or period (for full backfill).
    Returns DataFrame with columns [Date, Close], or empty on failure.
    """
    attempts = len(RETRY_DELAYS) + 1

    for attempt_num in range(attempts):
        # Politeness delay before each attempt
        jitter = random.uniform(0, POLITENESS_DELAY_SECONDS * 0.5)
        time.sleep(POLITENESS_DELAY_SECONDS + jitter)

        try:
            t = yf.Ticker(ticker)
            if start_date:
                # Explicit future end so Yahoo's period1 (midnight in the
                # ticker's exchange tz) is always < period2. Without this,
                # runs in the 00:00–04:00 UTC window fail with
                # "start date cannot be after end date" because yfinance
                # defaults end to now-UTC, which can be before midnight ET.
                end_date = (datetime.now(timezone.utc).date() + timedelta(days=1)).strftime("%Y-%m-%d")
                hist = t.history(start=start_date, end=end_date, interval="1d", auto_adjust=True)
            else:
                hist = t.history(period=period, interval="1d", auto_adjust=True)

            if isinstance(hist, pd.DataFrame) and not hist.empty:
                # Extract just Date and Close
                out = pd.DataFrame({
                    "Date": pd.to_datetime(hist.index).tz_localize(None) if hist.index.tz else pd.to_datetime(hist.index),
                    "Close": hist["Close"].values,
                })
                out = out.dropna(subset=["Close"])
                if not out.empty:
                    if attempt_num > 0:
                        logger.info(f"[{ticker}] succeeded on retry attempt {attempt_num}")
                    return out
            # Empty result — could be rate limit
            logger.warning(f"[{ticker}] empty history result (attempt {attempt_num + 1}/{attempts})")
        except Exception as e:
            logger.warning(f"[{ticker}] history fetch error (attempt {attempt_num + 1}/{attempts}): {e}")

        if attempt_num < len(RETRY_DELAYS):
            wait = RETRY_DELAYS[attempt_num]
            logger.warning(f"[{ticker}] waiting {wait}s before retry")
            time.sleep(wait)

    logger.error(f"[{ticker}] all {attempts} attempts failed; returning empty")
    return pd.DataFrame(columns=["Date", "Close"])


# ============ PUBLIC API ============

def ensure_price_cache(ticker: str, cache_dir: str) -> dict:
    """
    Ensure that the cache for one ticker is current.

    Returns a dict with status info:
        {
            "ticker": ...,
            "status": "fresh" | "fetched_full" | "fetched_incremental" | "failed",
            "rows_added": int,
            "first_date": str,
            "last_date": str,
            "error": str (if failed)
        }
    """
    today = pd.Timestamp(datetime.now().date())
    existing = _load_cached_prices(ticker, cache_dir)

    if existing.empty:
        # Full backfill
        new_data = _fetch_history_with_retry(ticker, period=f"{LOOKBACK_YEARS}y")
        if new_data.empty:
            return {
                "ticker": ticker,
                "status": "failed",
                "rows_added": 0,
                "first_date": None,
                "last_date": None,
                "error": "full backfill returned no data",
            }
        _save_cached_prices(ticker, new_data, cache_dir)
        return {
            "ticker": ticker,
            "status": "fetched_full",
            "rows_added": len(new_data),
            "first_date": str(new_data["Date"].min().date()),
            "last_date": str(new_data["Date"].max().date()),
            "error": None,
        }

    # Cache exists; check if needs update
    last_cached = pd.Timestamp(existing["Date"].max())
    if last_cached.date() >= today.date():
        # Already current
        return {
            "ticker": ticker,
            "status": "fresh",
            "rows_added": 0,
            "first_date": str(existing["Date"].min().date()),
            "last_date": str(existing["Date"].max().date()),
            "error": None,
        }

    # Incremental fetch — start from day after last cached date
    start = (last_cached + timedelta(days=1)).strftime("%Y-%m-%d")
    new_data = _fetch_history_with_retry(ticker, start_date=start)

    if new_data.empty:
        # Could be weekend/holiday between last_cached and today — not necessarily a failure
        logger.info(f"[{ticker}] incremental fetch returned no new data (last cached: {last_cached.date()})")
        return {
            "ticker": ticker,
            "status": "fresh",
            "rows_added": 0,
            "first_date": str(existing["Date"].min().date()),
            "last_date": str(existing["Date"].max().date()),
            "error": None,
        }

    # Merge: drop any overlap with existing (defensive), append new
    combined = pd.concat([existing, new_data], ignore_index=True)
    combined = combined.drop_duplicates(subset=["Date"], keep="last")
    combined = combined.sort_values("Date").reset_index(drop=True)

    # Trim to lookback window: keep only data from (today - LOOKBACK_YEARS years) onwards
    cutoff = today - pd.DateOffset(years=LOOKBACK_YEARS)
    combined = combined[combined["Date"] >= cutoff].reset_index(drop=True)

    _save_cached_prices(ticker, combined, cache_dir)

    return {
        "ticker": ticker,
        "status": "fetched_incremental",
        "rows_added": len(new_data),
        "first_date": str(combined["Date"].min().date()),
        "last_date": str(combined["Date"].max().date()),
        "error": None,
    }


def load_cached_prices(ticker: str, cache_dir: str) -> pd.DataFrame:
    """
    Public wrapper to load cached prices for one ticker.
    Returns DataFrame with columns [Date, Close], or empty if not cached.
    """
    return _load_cached_prices(ticker, cache_dir)


def load_cached_returns(ticker: str, cache_dir: str) -> pd.Series:
    """
    Load cached prices and convert to daily returns (percent change).
    Returns a pandas Series indexed by Date, or empty Series if no data.
    """
    df = _load_cached_prices(ticker, cache_dir)
    if df.empty:
        return pd.Series(dtype=float)
    df = df.sort_values("Date").reset_index(drop=True)
    returns = df["Close"].pct_change().dropna()
    returns.index = df["Date"].iloc[1:].values
    returns.name = ticker
    return returns