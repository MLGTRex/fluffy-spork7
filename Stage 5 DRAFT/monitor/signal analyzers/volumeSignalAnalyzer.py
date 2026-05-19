"""
Volume signal analyzer — classifies today's volume against the historical
percentile baseline.

For one ticker, given:
  - Today's volume data (from volumeData)
  - The percentile baseline for this ticker (from percentileBaselines)

Determine:
  - Where does today's volume fall in the historical distribution?
  - Did it cross the major (99th pct) or minor (95th pct) threshold?

Volume signals are useful corroboration for price signals: a 4% move on 3x
typical volume is more likely thesis-relevant than a 4% move on routine
volume. Volume alone (no price move) can also indicate something is
happening that the market hasn't fully priced in yet.

One complication: today's volume might be partial (during the trading day)
or complete (after close). For partial-day volume, we adjust the threshold
comparison so we're not flagging "low volume so far" as suspicious — instead
we extrapolate or compare to typical volume at this time of day. For now,
we take a simpler approach: only fire signals when volume EXCEEDS the
threshold (high volume is what we care about; low volume is usually just
"market hasn't been busy"). This way partial-day low volume can't fire.

Usage:
    from volumeSignalAnalyzer import analyze_volume_signal
    result = analyze_volume_signal(
        ticker="ARDX",
        volume_data=volume_data_result,    # from fetch_volume_data
        baseline=ticker_baseline,          # from compute_baseline_for_ticker
    )
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional


# ============ CONFIGURATION ============

# Volume percentile thresholds. Volume distributions are usually right-skewed
# (lots of routine days, occasional spikes), so the 99th percentile here is
# meaningful — a 1-in-100-day volume spike.
MAJOR_PERCENTILE = 99
MINOR_PERCENTILE = 95


# ============ LOGGER ============

_logger = logging.getLogger("monitor.volumeSignalAnalyzer")
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
        "signal_type": "volume",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "baseline_computed_at": None,
        "fired": False,
        "tier": None,
        "details": {
            "current_volume": None,
            "is_partial_day": None,
            "percentile_reached": None,
            "threshold_p95": None,
            "threshold_p99": None,
            "ratio_to_median": None,  # current / p50 (helpful intuition)
        },
        "data_quality_flags": [],
    }


# ============ PERCENTILE LOOKUP ============

def _percentile_reached(volume: int, baseline: dict) -> Optional[float]:
    """
    Estimate what percentile this volume corresponds to in the distribution.

    Returns a float in [0, 100] or None if baseline data is insufficient.
    """
    vol_pcts = baseline.get("daily_volume", {}).get("signed", {})

    breakpoints = [50, 75, 90, 95, 99]
    values = []
    for p in breakpoints:
        v = vol_pcts.get(f"p{p}")
        if v is None:
            return None
        values.append((p, v))

    # Below p50 → interpolate toward 0
    if volume <= values[0][1]:
        if values[0][1] == 0:
            return 50.0
        return (volume / values[0][1]) * values[0][0]

    # Above p99 → extrapolate
    if volume >= values[-1][1]:
        excess_factor = (volume - values[-1][1]) / values[-1][1] if values[-1][1] > 0 else 0
        return min(99.0 + excess_factor * 0.5, 99.99)

    # Interpolate between breakpoints
    for i in range(len(values) - 1):
        p_lo, v_lo = values[i]
        p_hi, v_hi = values[i + 1]
        if v_lo <= volume <= v_hi:
            if v_hi == v_lo:
                return p_lo
            frac = (volume - v_lo) / (v_hi - v_lo)
            return p_lo + frac * (p_hi - p_lo)

    return None


# ============ MAIN ANALYSIS ============

def analyze_volume_signal(ticker: str, volume_data: dict,
                           baseline: Optional[dict] = None) -> dict:
    """
    Analyze today's volume signal against the historical baseline.

    Args:
        ticker: ticker symbol
        volume_data: result dict from fetch_volume_data()
        baseline: result dict from compute_baseline_for_ticker() for this ticker

    Returns:
        Signal result dict. 'fired' is True if today's volume exceeds the
        minor threshold; 'tier' is "major" if it exceeds the 99th pct, "minor"
        if it exceeds the 95th pct.

    Note: only HIGH volume fires signals. Low volume (partial-day or quiet
    days) is not a trigger.
    """
    result = _empty_result(ticker)

    if not volume_data:
        result["data_quality_flags"].append("No volume data provided")
        return result

    if volume_data.get("status") == "fetch_failed":
        result["data_quality_flags"].append("Volume data fetch failed; cannot analyze")
        return result

    # Pass through any upstream data quality flags
    for flag in volume_data.get("data_quality_flags", []):
        result["data_quality_flags"].append(f"volume_data: {flag}")

    today_volume = volume_data.get("today", {}).get("volume_so_far")
    is_partial = volume_data.get("today", {}).get("is_partial_day")
    result["details"]["current_volume"] = today_volume
    result["details"]["is_partial_day"] = is_partial

    if today_volume is None:
        result["data_quality_flags"].append("No today volume available; signal cannot fire")
        return result

    if baseline is None:
        result["data_quality_flags"].append(
            "No baseline provided; volume signal cannot use percentile thresholds"
        )
        return result

    result["baseline_computed_at"] = baseline.get("computed_at")
    vol_pcts = baseline.get("daily_volume", {}).get("signed", {})
    p95 = vol_pcts.get(f"p{MINOR_PERCENTILE}")
    p99 = vol_pcts.get(f"p{MAJOR_PERCENTILE}")
    p50 = vol_pcts.get("p50")

    result["details"]["threshold_p95"] = p95
    result["details"]["threshold_p99"] = p99

    if p95 is None or p99 is None:
        result["data_quality_flags"].append(
            "Baseline missing volume percentile breakpoints; cannot classify"
        )
        return result

    # Compute ratio to median (intuitive context: "today is 2.3x typical")
    if p50 is not None and p50 > 0:
        result["details"]["ratio_to_median"] = today_volume / p50

    # Percentile reached
    percentile = _percentile_reached(today_volume, baseline)
    result["details"]["percentile_reached"] = percentile

    # Tier classification (high volume only — partial-day low volume doesn't fire)
    if today_volume >= p99:
        result["fired"] = True
        result["tier"] = "major"
    elif today_volume >= p95:
        result["fired"] = True
        result["tier"] = "minor"

    # Partial-day caveat: flag if it's partial-day AND signal fired
    if is_partial and result["fired"]:
        result["data_quality_flags"].append(
            "Signal fired on partial-day volume; full-day volume could be even higher"
        )

    return result


# ============ CLI ============

def _cli():
    parser = argparse.ArgumentParser(description="Analyze volume signal for a ticker")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--lookback-days", type=int, default=60)
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    monitor_dir = os.path.normpath(os.path.join(script_dir, ".."))
    sys.path.insert(0, os.path.join(monitor_dir, "data sources"))
    sys.path.insert(0, os.path.join(monitor_dir, "baselines"))
    try:
        from priceData import fetch_price_data
        from volumeData import fetch_volume_data
        from earningsCalendar import fetch_earnings_calendar
        from percentileBaselines import compute_baseline_for_ticker
    except ImportError as e:
        print(f"Could not import dependencies: {e}", file=sys.stderr)
        return 1

    print(f"Fetching data for {args.ticker}...", file=sys.stderr)
    price_result = fetch_price_data(args.ticker, lookback_days=args.lookback_days)
    volume_result = fetch_volume_data(args.ticker, lookback_days=args.lookback_days)
    earnings_result = fetch_earnings_calendar(args.ticker)

    baseline = compute_baseline_for_ticker(
        ticker=args.ticker,
        daily_bars=price_result["historical"]["daily_bars"],
        daily_volumes=volume_result["historical"]["daily_volumes"],
        earnings_dates=earnings_result["historical_earnings_dates"],
        lookback_days=args.lookback_days,
    )

    signal = analyze_volume_signal(args.ticker, volume_result, baseline)
    print(json.dumps(signal, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())