import logging
import asyncio
import time
import random
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

# ============ RATE-LIMIT HANDLING CONFIG ============

POLITENESS_DELAY_SECONDS = 0.5
RETRY_DELAYS = [2, 5, 15, 30, 60]


# ============ HELPERS ============

def _safe_get(d: dict, key: str):
    if d is None:
        return None
    val = d.get(key)
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    return val


def _safe_div(numerator, denominator):
    if numerator is None or denominator is None:
        return None
    try:
        if denominator == 0:
            return None
        return numerator / denominator
    except (TypeError, ZeroDivisionError):
        return None


# ============ RATE-LIMIT DETECTION ============

def _looks_rate_limited(raw_metrics: dict) -> bool:
    """
    Heuristic for rate-limit detection.
    For news sentiment, a successful .info call gives us sector and current_price at minimum.
    If both are missing, treat as rate-limited.
    """
    if not raw_metrics:
        return True

    if raw_metrics.get("sector") and raw_metrics.get("current_price"):
        return False

    # Count populated fields across all sub-blocks
    blocks = ["earnings", "price_momentum", "insider_activity"]
    populated = 0
    total = 0
    for block_name in blocks:
        block = raw_metrics.get(block_name) or {}
        for v in block.values():
            total += 1
            if v is not None:
                populated += 1

    if total > 0 and (populated / total) < 0.10:
        return True

    return False


# ============ EXTRACTION HELPERS PER SUB-COMPONENT ============

def _extract_earnings_data(t, ticker: str, info: dict, flags: list) -> dict:
    """
    Extract earnings beat rate and surprise magnitude from t.earnings_dates,
    plus EPS revision trend from .info fields.
    """
    out = {
        "beat_count_4q": None,
        "miss_count_4q": None,
        "total_quarters_4q": None,
        "avg_surprise_magnitude_4q": None,
        "eps_estimate_revision_90d": None,
        "trailing_eps": _safe_get(info, "trailingEps"),
        "forward_eps": _safe_get(info, "forwardEps"),
    }

    # Earnings beat/miss from earnings_dates
    try:
        ed = t.earnings_dates
        if isinstance(ed, pd.DataFrame) and not ed.empty:
            # Filter to past quarters (not future earnings dates)
            now = datetime.now()
            try:
                if isinstance(ed.index, pd.DatetimeIndex):
                    # Convert tz-aware to tz-naive for comparison
                    idx = ed.index.tz_localize(None) if ed.index.tz else ed.index
                    past = ed[idx < now]
                else:
                    past = ed
            except Exception:
                past = ed

            # Take the last 4 past quarters
            past = past.head(4) if len(past) > 4 else past

            beats = 0
            misses = 0
            surprises = []
            for _, row in past.iterrows():
                actual = row.get("Reported EPS")
                estimate = row.get("EPS Estimate")
                if actual is None or estimate is None:
                    continue
                try:
                    if pd.isna(actual) or pd.isna(estimate):
                        continue
                    if actual > estimate:
                        beats += 1
                    elif actual < estimate:
                        misses += 1
                    if estimate != 0:
                        surprises.append((actual - estimate) / abs(estimate))
                except (TypeError, ValueError):
                    continue

            total = beats + misses + (len(past) - beats - misses)  # include "in-line"
            if total > 0:
                out["beat_count_4q"] = beats
                out["miss_count_4q"] = misses
                out["total_quarters_4q"] = total
            if surprises:
                out["avg_surprise_magnitude_4q"] = sum(surprises) / len(surprises)
    except Exception as e:
        flags.append(f"{ticker}: earnings_dates fetch failed ({e})")

    # EPS estimate revision trend (90-day)
    # yfinance's eps_revisions has columns like "current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo"
    # for each period (current quarter, next quarter, current year, etc.)
    try:
        rev_df = t.eps_revisions
        if isinstance(rev_df, pd.DataFrame) and not rev_df.empty:
            # Try to use the current-quarter row
            # Different yfinance versions name this differently; we look for keywords
            current_q_row = None
            for idx in rev_df.index:
                idx_str = str(idx).lower()
                if "current" in idx_str and ("quarter" in idx_str or "qtr" in idx_str or "0q" in idx_str):
                    current_q_row = rev_df.loc[idx]
                    break
            if current_q_row is None and len(rev_df) > 0:
                # Fall back to first row
                current_q_row = rev_df.iloc[0]

            if current_q_row is not None:
                # Look for "current" and "90daysAgo" (or similar) columns
                cur_val = None
                old_val = None
                for col in current_q_row.index:
                    col_str = str(col).lower()
                    if col_str == "current" or "0day" in col_str:
                        cur_val = current_q_row[col]
                    elif "90day" in col_str:
                        old_val = current_q_row[col]
                if cur_val is not None and old_val is not None and not pd.isna(cur_val) and not pd.isna(old_val):
                    if old_val != 0:
                        out["eps_estimate_revision_90d"] = (cur_val - old_val) / abs(old_val)
                    elif cur_val > 0:
                        out["eps_estimate_revision_90d"] = 1.0  # came from zero, treat as full positive
                    elif cur_val < 0:
                        out["eps_estimate_revision_90d"] = -1.0
    except Exception as e:
        flags.append(f"{ticker}: eps_revisions fetch failed ({e})")

    return out


def _extract_price_momentum(t, ticker: str, info: dict, flags: list) -> dict:
    """Extract 1m, 3m, 6m price returns."""
    out = {
        "return_1m": None,
        "return_3m": None,
        "return_6m": None,
    }

    current_price = _safe_get(info, "currentPrice") or _safe_get(info, "regularMarketPrice")

    try:
        # Pull 7 months of daily history to safely compute 6m return
        hist = t.history(period="7mo", interval="1d")
        if not isinstance(hist, pd.DataFrame) or hist.empty:
            flags.append(f"{ticker}: price history empty")
            return out

        closes = hist["Close"].dropna()
        if closes.empty:
            return out

        latest_close = float(closes.iloc[-1])
        latest_date = closes.index[-1]

        # If we have a current_price from .info, prefer it for the latest value (more recent)
        ref_price = current_price if current_price is not None else latest_close

        def _return_at_offset(days_back: int):
            """Return the percentage change between ref_price and the close ~days_back days ago."""
            target_date = latest_date - timedelta(days=days_back)
            # Find the closest trading day at or before target_date
            try:
                eligible = closes[closes.index <= target_date]
                if eligible.empty:
                    return None
                old_price = float(eligible.iloc[-1])
                if old_price <= 0:
                    return None
                return (ref_price - old_price) / old_price
            except Exception:
                return None

        out["return_1m"] = _return_at_offset(30)
        out["return_3m"] = _return_at_offset(91)
        out["return_6m"] = _return_at_offset(182)

    except Exception as e:
        flags.append(f"{ticker}: price momentum fetch failed ({e})")

    return out


def _extract_insider_activity(t, ticker: str, flags: list) -> dict:
    """
    Extract insider activity over the last 90 days.
    Net insider activity = (purchases value - sales value) / market cap as a normalized signal.
    """
    out = {
        "insider_purchases_count_90d": None,
        "insider_sales_count_90d": None,
        "insider_net_value_90d_usd": None,
    }

    try:
        trans = t.insider_transactions
        if not isinstance(trans, pd.DataFrame) or trans.empty:
            return out

        # Filter to last 90 days
        cutoff = datetime.now() - timedelta(days=90)
        try:
            if "Start Date" in trans.columns:
                # Parse Start Date column
                trans = trans.copy()
                trans["_parsed_date"] = pd.to_datetime(trans["Start Date"], errors="coerce")
                recent = trans[trans["_parsed_date"] >= cutoff]
            elif isinstance(trans.index, pd.DatetimeIndex):
                idx = trans.index.tz_localize(None) if trans.index.tz else trans.index
                recent = trans[idx >= cutoff]
            else:
                recent = trans
        except Exception:
            recent = trans

        if recent.empty:
            return out

        purchases_count = 0
        sales_count = 0
        net_value = 0.0

        # Look for transaction type and value columns
        text_col = None
        value_col = None
        for col in recent.columns:
            cl = str(col).lower()
            if text_col is None and ("text" in cl or "transaction" in cl):
                text_col = col
            if value_col is None and "value" in cl:
                value_col = col

        for _, row in recent.iterrows():
            text = str(row.get(text_col, "")).lower() if text_col else ""
            value = row.get(value_col) if value_col else None

            try:
                if value is not None and not pd.isna(value):
                    value = float(value)
                else:
                    value = 0
            except (TypeError, ValueError):
                value = 0

            # Heuristic: text contains "purchase" or "buy" -> purchase; "sale" or "sell" -> sale
            if "purchase" in text or "buy" in text:
                purchases_count += 1
                net_value += value
            elif "sale" in text or "sell" in text or "disposition" in text:
                sales_count += 1
                net_value -= value

        out["insider_purchases_count_90d"] = purchases_count
        out["insider_sales_count_90d"] = sales_count
        out["insider_net_value_90d_usd"] = net_value if (purchases_count + sales_count) > 0 else None

    except Exception as e:
        flags.append(f"{ticker}: insider_transactions fetch failed ({e})")

    return out


# ============ CORE EXTRACTION (NO RETRY) ============

def _extract_raw_metrics_once(ticker_symbol: str) -> dict:
    """Single-attempt extraction of news sentiment raw metrics."""
    flags = []

    try:
        t = yf.Ticker(ticker_symbol)
    except Exception as e:
        flags.append(f"{ticker_symbol}: failed to initialize yfinance Ticker ({e})")
        return _empty_raw_metrics(flags)

    info = {}
    try:
        info = t.info or {}
    except Exception as e:
        flags.append(f"{ticker_symbol}: failed to fetch .info ({e})")
        info = {}

    sector = info.get("sector")
    industry = info.get("industry")
    market_cap = _safe_get(info, "marketCap")
    current_price = _safe_get(info, "currentPrice") or _safe_get(info, "regularMarketPrice")

    if not sector:
        flags.append(f"{ticker_symbol}: sector classification unavailable")

    earnings_data = _extract_earnings_data(t, ticker_symbol, info, flags)
    price_momentum = _extract_price_momentum(t, ticker_symbol, info, flags)
    insider_activity = _extract_insider_activity(t, ticker_symbol, flags)

    return {
        "sector": sector,
        "industry": industry,
        "market_cap_usd": market_cap,
        "current_price": current_price,
        "earnings": earnings_data,
        "price_momentum": price_momentum,
        "insider_activity": insider_activity,
        "data_quality_flags": flags,
    }


def _empty_raw_metrics(flags: list) -> dict:
    return {
        "sector": None,
        "industry": None,
        "market_cap_usd": None,
        "current_price": None,
        "earnings": {
            "beat_count_4q": None,
            "miss_count_4q": None,
            "total_quarters_4q": None,
            "avg_surprise_magnitude_4q": None,
            "eps_estimate_revision_90d": None,
            "trailing_eps": None,
            "forward_eps": None,
        },
        "price_momentum": {
            "return_1m": None,
            "return_3m": None,
            "return_6m": None,
        },
        "insider_activity": {
            "insider_purchases_count_90d": None,
            "insider_sales_count_90d": None,
            "insider_net_value_90d_usd": None,
        },
        "data_quality_flags": flags,
    }


# ============ PUBLIC: SYNCHRONOUS WITH RETRY ============

def extract_raw_metrics(ticker_symbol: str) -> dict:
    """Extract news sentiment raw metrics with rate-limit-aware retry."""
    last_result = None
    attempts = len(RETRY_DELAYS) + 1

    for attempt_num in range(attempts):
        jitter = random.uniform(0, POLITENESS_DELAY_SECONDS * 0.5)
        time.sleep(POLITENESS_DELAY_SECONDS + jitter)

        result = _extract_raw_metrics_once(ticker_symbol)
        last_result = result

        if not _looks_rate_limited(result):
            if attempt_num > 0:
                logger.info(f"{ticker_symbol}: succeeded on retry attempt {attempt_num}")
            return result

        if attempt_num < len(RETRY_DELAYS):
            wait_seconds = RETRY_DELAYS[attempt_num]
            logger.warning(
                f"{ticker_symbol}: looks rate-limited (attempt {attempt_num + 1}/{attempts}); "
                f"waiting {wait_seconds}s before retry"
            )
            time.sleep(wait_seconds)
        else:
            logger.error(
                f"{ticker_symbol}: still rate-limited after {attempts} attempts; giving up"
            )
            if "data_quality_flags" not in last_result:
                last_result["data_quality_flags"] = []
            last_result["data_quality_flags"].append(
                f"{ticker_symbol}: extraction gave up after {attempts} rate-limited attempts"
            )

    return last_result


async def extract_raw_metrics_async(ticker_symbol: str) -> dict:
    """Async wrapper around extract_raw_metrics."""
    return await asyncio.to_thread(extract_raw_metrics, ticker_symbol)