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
    Heuristic for detecting rate-limit-induced empty data.

    For professional analysis, the key indicators of a successful .info call are:
    - sector populated (used for sector-relative scoring later)
    - market_cap_usd populated
    - At least some core metrics present

    If sector and market_cap are both missing, treat as rate-limited.
    """
    if not raw_metrics:
        return True

    if raw_metrics.get("sector") and raw_metrics.get("market_cap_usd"):
        return False

    # Count populated fields across all sub-blocks
    blocks = ["analyst", "rating_momentum", "institutional", "short_interest"]
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


# ============ CORE EXTRACTION (NO RETRY) ============

def _extract_raw_metrics_once(ticker_symbol: str) -> dict:
    """Single-attempt extraction of professional analysis raw metrics."""
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

    # ============ ANALYST CONSENSUS ============
    recommendation_mean = _safe_get(info, "recommendationMean")
    recommendation_key = info.get("recommendationKey")  # string, no NaN check
    target_mean = _safe_get(info, "targetMeanPrice")
    target_high = _safe_get(info, "targetHighPrice")
    target_low = _safe_get(info, "targetLowPrice")
    target_median = _safe_get(info, "targetMedianPrice")
    num_analysts = _safe_get(info, "numberOfAnalystOpinions")

    upside_to_target = None
    if target_mean is not None and current_price is not None and current_price > 0:
        upside_to_target = (target_mean - current_price) / current_price

    target_dispersion = None  # (high - low) / mean — measure of disagreement
    if target_high is not None and target_low is not None and target_mean is not None and target_mean > 0:
        target_dispersion = (target_high - target_low) / target_mean

    # ============ RATING MOMENTUM ============
    upgrades = 0
    downgrades = 0
    rating_actions_total = 0
    try:
        ud_df = t.upgrades_downgrades
        if isinstance(ud_df, pd.DataFrame) and not ud_df.empty:
            # The DataFrame is indexed by date with columns including "Action" or "Firm"/"ToGrade"/"FromGrade"
            # Filter to last 12 months
            cutoff = datetime.now() - timedelta(days=365)
            try:
                # Index might be DatetimeIndex or have a date-like column
                if isinstance(ud_df.index, pd.DatetimeIndex):
                    recent = ud_df[ud_df.index >= cutoff]
                else:
                    recent = ud_df  # fall back to entire frame if index isn't datetime
            except Exception:
                recent = ud_df

            # Detect upgrade/downgrade actions
            # yfinance typically uses an "Action" column with values like "up", "down", "main", "init"
            if "Action" in recent.columns:
                action_col = recent["Action"].astype(str).str.lower()
                upgrades = int((action_col == "up").sum())
                downgrades = int((action_col == "down").sum())
                rating_actions_total = int(len(recent))
            else:
                # Fallback: compare ToGrade vs FromGrade if present
                if "ToGrade" in recent.columns and "FromGrade" in recent.columns:
                    grade_rank = {
                        "strong sell": 1, "sell": 2, "underperform": 2,
                        "hold": 3, "neutral": 3, "market perform": 3,
                        "buy": 4, "outperform": 4, "overweight": 4,
                        "strong buy": 5,
                    }
                    for _, row in recent.iterrows():
                        to_g = str(row.get("ToGrade", "")).strip().lower()
                        from_g = str(row.get("FromGrade", "")).strip().lower()
                        if to_g in grade_rank and from_g in grade_rank:
                            if grade_rank[to_g] > grade_rank[from_g]:
                                upgrades += 1
                            elif grade_rank[to_g] < grade_rank[from_g]:
                                downgrades += 1
                            rating_actions_total += 1
    except Exception as e:
        flags.append(f"{ticker_symbol}: upgrades/downgrades fetch failed ({e})")

    # ============ INSTITUTIONAL POSITIONING ============
    held_pct_institutions = _safe_get(info, "heldPercentInstitutions")
    held_pct_insiders = _safe_get(info, "heldPercentInsiders")

    # Both yfinance fields are decimals (0.65 = 65%) but occasionally come as percent (65.0).
    # Normalize: if value > 1, divide by 100.
    if held_pct_institutions is not None and held_pct_institutions > 1:
        held_pct_institutions = held_pct_institutions / 100.0
    if held_pct_insiders is not None and held_pct_insiders > 1:
        held_pct_insiders = held_pct_insiders / 100.0

    # ============ SHORT INTEREST ============
    short_pct_of_float = _safe_get(info, "shortPercentOfFloat")
    if short_pct_of_float is not None and short_pct_of_float > 1:
        short_pct_of_float = short_pct_of_float / 100.0
    short_ratio = _safe_get(info, "shortRatio")  # days to cover

    # Short interest change vs prior month (positive = increasing short pressure)
    shares_short = _safe_get(info, "sharesShort")
    shares_short_prior = _safe_get(info, "sharesShortPriorMonth")
    short_change_pct = None
    if shares_short is not None and shares_short_prior is not None and shares_short_prior > 0:
        short_change_pct = (shares_short - shares_short_prior) / shares_short_prior

    # ============ ASSEMBLE ============
    return {
        "sector": sector,
        "industry": industry,
        "market_cap_usd": market_cap,
        "current_price": current_price,
        "analyst": {
            "recommendation_mean": recommendation_mean,
            "recommendation_key": recommendation_key,
            "target_mean_price": target_mean,
            "target_median_price": target_median,
            "target_high_price": target_high,
            "target_low_price": target_low,
            "upside_to_target": upside_to_target,
            "target_dispersion": target_dispersion,
            "num_analysts": num_analysts,
        },
        "rating_momentum": {
            "upgrades_12m": upgrades,
            "downgrades_12m": downgrades,
            "total_actions_12m": rating_actions_total,
        },
        "institutional": {
            "held_pct_institutions": held_pct_institutions,
            "held_pct_insiders": held_pct_insiders,
        },
        "short_interest": {
            "short_pct_of_float": short_pct_of_float,
            "short_ratio": short_ratio,
            "short_change_pct": short_change_pct,
        },
        "data_quality_flags": flags,
    }


def _empty_raw_metrics(flags: list) -> dict:
    return {
        "sector": None,
        "industry": None,
        "market_cap_usd": None,
        "current_price": None,
        "analyst": {
            "recommendation_mean": None,
            "recommendation_key": None,
            "target_mean_price": None,
            "target_median_price": None,
            "target_high_price": None,
            "target_low_price": None,
            "upside_to_target": None,
            "target_dispersion": None,
            "num_analysts": None,
        },
        "rating_momentum": {
            "upgrades_12m": None,
            "downgrades_12m": None,
            "total_actions_12m": None,
        },
        "institutional": {
            "held_pct_institutions": None,
            "held_pct_insiders": None,
        },
        "short_interest": {
            "short_pct_of_float": None,
            "short_ratio": None,
            "short_change_pct": None,
        },
        "data_quality_flags": flags,
    }


# ============ PUBLIC: SYNCHRONOUS WITH RETRY ============

def extract_raw_metrics(ticker_symbol: str) -> dict:
    """
    Extract professional analysis raw metrics for one company with rate-limit-aware retry.
    Same pattern as financialDataExtraction.extract_raw_metrics:
        - Politeness delay before each call
        - Up to 6 attempts (1 initial + 5 retries)
        - Exponential backoff per RETRY_DELAYS
    """
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