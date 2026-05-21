"""
Price cache for Stage 4 pre-optimization.

Maintains a local cache of daily historical prices for:
    - Stage 3 candidate companies (read from /stage 3 DRAFT/output/)
    - 7 macro factor proxies (defined in macroAnalysis.py's FACTOR_PROXIES dict)

The cache uses one CSV per ticker at /stage 4 DRAFT/cache/prices/{ticker}.csv.
Each CSV has columns: Date, Close (split-adjusted via yfinance's default auto_adjust=True).

On each run:
    - If cache for a ticker doesn't exist: fetch full LOOKBACK_YEARS via period=Ny.
    - Otherwise: re-fetch a trailing window sized to the cache gap (5d / 1mo /
      3mo / 6mo / 1y / Ny) via yfinance's period= mode, and merge. Using
      period= rather than start=/end= avoids Yahoo's "data doesn't exist" /
      "start cannot be after end" rejections that fire when the requested
      window contains no bars (e.g. asking for today's bar before today's
      session has traded).

The cache is shared by correlationAnalysis.py and macroAnalysis.py.
"""

import logging
import os
import time
import random
from datetime import datetime, timezone
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
    """Load cached prices for one ticker. Returns empty DataFrame if not cached.

    Strictly-future-dated rows (Date > today_utc) are dropped on load — past
    runs have written intraday snapshots into rows the cache shouldn't yet
    own, and we want a clean baseline before merging the trailing-window
    re-fetch on top.
    """
    path = _cache_path(ticker, cache_dir)
    if not os.path.exists(path):
        return pd.DataFrame(columns=["Date", "Close"])
    try:
        df = pd.read_csv(path, parse_dates=["Date"])
        today_ts = pd.Timestamp(datetime.now(timezone.utc).date())
        df = df[df["Date"] <= today_ts].reset_index(drop=True)
        return df
    except Exception as e:
        logger.warning(f"[{ticker}] failed to read cache at {path}: {e}; treating as empty")
        return pd.DataFrame(columns=["Date", "Close"])


def _save_cached_prices(ticker: str, df: pd.DataFrame, cache_dir: str):
    """Write cached prices for one ticker."""
    path = _cache_path(ticker, cache_dir)
    df.to_csv(path, index=False)


def _period_for_gap(gap_days: int) -> str:
    """Pick the smallest yfinance `period` that comfortably covers a gap.

    Always returns an over-fetch — even gap=0 yields "5d" — so the merge
    step has overlapping rows to dedupe against and any stale intraday
    snapshot in the cache gets overwritten with a fresh value.
    """
    if gap_days <= 5:
        return "5d"
    if gap_days <= 21:
        return "1mo"
    if gap_days <= 60:
        return "3mo"
    if gap_days <= 180:
        return "6mo"
    if gap_days <= 365:
        return "1y"
    return f"{LOOKBACK_YEARS}y"


def _fetch_history_with_retry(ticker: str, period: str) -> pd.DataFrame:
    """
    Fetch trailing-window price history from yfinance with retry-on-rate-limit
    logic. Always uses yfinance's period= mode ("5d", "1mo", …, "3y") so Yahoo
    returns whatever bars exist in the window rather than rejecting on a
    specific start/end range that may contain no bars.

    Returns DataFrame with columns [Date, Close], or empty on failure.
    """
    attempts = len(RETRY_DELAYS) + 1

    for attempt_num in range(attempts):
        # Politeness delay before each attempt
        jitter = random.uniform(0, POLITENESS_DELAY_SECONDS * 0.5)
        time.sleep(POLITENESS_DELAY_SECONDS + jitter)

        try:
            t = yf.Ticker(ticker)
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
    today = pd.Timestamp(datetime.now(timezone.utc).date())
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

    # Cache exists. Always re-fetch a trailing window sized to cover the gap
    # (plus overlap). period= mode avoids Yahoo's date-window rejections;
    # the merge below dedupes overlap and overwrites stale rows.
    last_cached = pd.Timestamp(existing["Date"].max())
    gap_days = max(0, (today.date() - last_cached.date()).days)
    period = _period_for_gap(gap_days)
    new_data = _fetch_history_with_retry(ticker, period=period)

    if new_data.empty:
        logger.info(f"[{ticker}] {period} fetch returned no data (last cached: {last_cached.date()})")
        return {
            "ticker": ticker,
            "status": "fresh",
            "rows_added": 0,
            "first_date": str(existing["Date"].min().date()),
            "last_date": str(existing["Date"].max().date()),
            "error": None,
        }

    # Merge: append new, dedupe on Date keeping the freshest value, sort.
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