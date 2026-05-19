"""
Price signal analyzer — classifies today's price move against the historical
percentile baseline.

For one ticker, given:
  - Today's price data (from priceData)
  - The percentile baseline for this ticker (from percentileBaselines)

Determine:
  - Where does today's daily move fall in the historical distribution?
  - Did the move cross the major (99th pct) or minor (95th pct) threshold?
  - Same question for move-from-open (intraday signal)

Does NOT decide whether to fire the LLM gate — that's the aggregator's job
based on combining this signal with others.

Two sub-signals computed:
  - daily_move: today's pct change vs previous close
  - move_from_open: today's pct change since today's open (intraday context)

Each gets its own classification. Either can fire independently; both feed
the aggregator's hybrid trigger logic.

Threshold configuration is at the top of the file. The major threshold (99th
pct) corresponds to "this happens about 2-3 times per year for this ticker."
The minor threshold (95th pct) corresponds to "this happens about 12-15
times per year." A major signal can fire the trigger alone; minor signals
require corroboration.

Usage:
    from priceSignalAnalyzer import analyze_price_signal
    result = analyze_price_signal(
        ticker="ARDX",
        price_data=price_data_result,        # output of fetch_price_data
        baseline=ticker_baseline,            # output of compute_baseline_for_ticker
    )
    if result["fired"]:
        # signal crossed at least one threshold
        ...
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional


# ============ CONFIGURATION ============

# Percentile thresholds for major and minor tiers. These reference the
# absolute-move distribution (direction-agnostic).
#
# Tunable. Starting values match the design doc.
MAJOR_PERCENTILE = 99   # 99th percentile → ~2-3 firings per year per ticker
MINOR_PERCENTILE = 95   # 95th percentile → ~12-15 firings per year per ticker

# For move-from-open: this is a noisier signal than daily move because
# intraday moves often reverse. Use slightly higher thresholds so we don't
# fire on routine intraday flutters. Configurable.
MAJOR_MOVE_FROM_OPEN_THRESHOLD_PCT = 5.0   # >5% intraday from open is major
MINOR_MOVE_FROM_OPEN_THRESHOLD_PCT = 3.0   # >3% intraday from open is minor


# ============ LOGGER ============

_logger = logging.getLogger("monitor.priceSignalAnalyzer")
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
        "signal_type": "price",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "baseline_computed_at": None,
        "fired": False,
        "tier": None,
        "details": {
            # daily move sub-signal
            "daily_move": {
                "current_pct": None,
                "absolute_pct": None,
                "fired": False,
                "tier": None,
                "percentile_reached": None,
                "threshold_p95_abs": None,
                "threshold_p99_abs": None,
            },
            # intraday move-from-open sub-signal
            "move_from_open": {
                "current_pct": None,
                "absolute_pct": None,
                "fired": False,
                "tier": None,
                "threshold_minor_pct": MINOR_MOVE_FROM_OPEN_THRESHOLD_PCT,
                "threshold_major_pct": MAJOR_MOVE_FROM_OPEN_THRESHOLD_PCT,
            },
        },
        "data_quality_flags": [],
    }


# ============ PERCENTILE LOOKUP ============

def _percentile_reached(absolute_move_pct: float, baseline: dict) -> Optional[float]:
    """
    Given an absolute move percentage and the baseline distribution, estimate
    what percentile this move corresponds to.

    Returns a float in [0, 100] or None if baseline data is insufficient.

    Uses linear interpolation between known percentile breakpoints. If the
    move exceeds the 99th percentile, we extrapolate (return 99+ via a
    bounded value).

    Example: if p95=2.1 and p99=3.4, and the move is 2.75, that's
    halfway between → ~97th percentile.
    """
    abs_pcts = baseline.get("daily_move_pct", {}).get("absolute", {})

    # Known breakpoints we expect (matching PERCENTILES_TO_COMPUTE)
    breakpoints = [50, 75, 90, 95, 99]
    values = []
    for p in breakpoints:
        v = abs_pcts.get(f"p{p}")
        if v is None:
            return None
        values.append((p, v))

    # If move is below the lowest breakpoint (50th pct of absolute moves),
    # it's still in the bottom half — report as 50 or interpolate
    # downward toward 0.
    if absolute_move_pct <= values[0][1]:
        # Below median absolute move. Estimate via linear interp toward 0.
        # 0% move → 0th percentile; values[0][1] move → 50th percentile.
        if values[0][1] == 0:
            return 50.0
        return (absolute_move_pct / values[0][1]) * values[0][0]

    # If above the highest breakpoint, return 99+
    if absolute_move_pct >= values[-1][1]:
        # Extrapolate sensibly: 99 + (how much beyond p99)
        excess_factor = (absolute_move_pct - values[-1][1]) / values[-1][1]
        # Cap at 99.99 to indicate "way beyond"
        return min(99.0 + excess_factor * 0.5, 99.99)

    # Linear interpolate between known breakpoints
    for i in range(len(values) - 1):
        p_lo, v_lo = values[i]
        p_hi, v_hi = values[i + 1]
        if v_lo <= absolute_move_pct <= v_hi:
            if v_hi == v_lo:
                return p_lo
            frac = (absolute_move_pct - v_lo) / (v_hi - v_lo)
            return p_lo + frac * (p_hi - p_lo)

    return None


# ============ MAIN ANALYSIS ============

def analyze_price_signal(ticker: str, price_data: dict,
                          baseline: Optional[dict] = None) -> dict:
    """
    Analyze today's price signal against the historical baseline.

    Args:
        ticker: ticker symbol
        price_data: result dict from fetch_price_data()
        baseline: result dict from compute_baseline_for_ticker() for this ticker.
                  If None, only the move-from-open sub-signal (which doesn't
                  need a baseline) can fire.

    Returns:
        Signal result dict (see module docstring). Top-level 'fired' is True
        if either sub-signal fired. Top-level 'tier' is the higher of the
        two sub-signal tiers (major > minor).
    """
    result = _empty_result(ticker)

    if not price_data:
        result["data_quality_flags"].append("No price data provided")
        return result

    if price_data.get("status") == "fetch_failed":
        result["data_quality_flags"].append("Price data fetch failed; cannot analyze")
        return result

    # Pass through any data quality flags from upstream
    for flag in price_data.get("data_quality_flags", []):
        result["data_quality_flags"].append(f"price_data: {flag}")

    # ---- Daily move sub-signal ----
    daily_change_pct = price_data.get("daily_change_pct")
    if daily_change_pct is not None:
        result["details"]["daily_move"]["current_pct"] = daily_change_pct
        result["details"]["daily_move"]["absolute_pct"] = abs(daily_change_pct)

        if baseline is None:
            result["data_quality_flags"].append(
                "No baseline provided; daily move sub-signal cannot use percentile thresholds"
            )
        else:
            result["baseline_computed_at"] = baseline.get("computed_at")
            abs_pcts = baseline.get("daily_move_pct", {}).get("absolute", {})
            p95 = abs_pcts.get(f"p{MINOR_PERCENTILE}")
            p99 = abs_pcts.get(f"p{MAJOR_PERCENTILE}")
            result["details"]["daily_move"]["threshold_p95_abs"] = p95
            result["details"]["daily_move"]["threshold_p99_abs"] = p99

            if p95 is None or p99 is None:
                result["data_quality_flags"].append(
                    "Baseline missing percentile breakpoints; cannot classify daily move"
                )
            else:
                abs_move = abs(daily_change_pct)
                percentile = _percentile_reached(abs_move, baseline)
                result["details"]["daily_move"]["percentile_reached"] = percentile

                # Tier classification
                if abs_move >= p99:
                    result["details"]["daily_move"]["fired"] = True
                    result["details"]["daily_move"]["tier"] = "major"
                elif abs_move >= p95:
                    result["details"]["daily_move"]["fired"] = True
                    result["details"]["daily_move"]["tier"] = "minor"

    # ---- Move-from-open sub-signal ----
    move_from_open_pct = price_data.get("today", {}).get("move_from_open_pct")
    if move_from_open_pct is not None:
        result["details"]["move_from_open"]["current_pct"] = move_from_open_pct
        abs_move = abs(move_from_open_pct)
        result["details"]["move_from_open"]["absolute_pct"] = abs_move

        if abs_move >= MAJOR_MOVE_FROM_OPEN_THRESHOLD_PCT:
            result["details"]["move_from_open"]["fired"] = True
            result["details"]["move_from_open"]["tier"] = "major"
        elif abs_move >= MINOR_MOVE_FROM_OPEN_THRESHOLD_PCT:
            result["details"]["move_from_open"]["fired"] = True
            result["details"]["move_from_open"]["tier"] = "minor"

    # ---- Combine sub-signals into top-level fired/tier ----
    daily_fired = result["details"]["daily_move"]["fired"]
    daily_tier = result["details"]["daily_move"]["tier"]
    mfo_fired = result["details"]["move_from_open"]["fired"]
    mfo_tier = result["details"]["move_from_open"]["tier"]

    result["fired"] = daily_fired or mfo_fired

    # Top-level tier is the higher of the two
    if "major" in (daily_tier, mfo_tier):
        result["tier"] = "major"
    elif "minor" in (daily_tier, mfo_tier):
        result["tier"] = "minor"

    return result


# ============ CLI ============

def _cli():
    """
    CLI mode: fetch live price data + baseline for a ticker, run the
    analyzer. Useful for inspecting how a real signal classification looks.
    """
    parser = argparse.ArgumentParser(description="Analyze price signal for a ticker")
    parser.add_argument("--ticker", required=True, help="Ticker symbol")
    parser.add_argument("--lookback-days", type=int, default=60)
    args = parser.parse_args()

    # Imports
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

    signal = analyze_price_signal(args.ticker, price_result, baseline)
    print(json.dumps(signal, indent=2, default=str))
    return 0 if signal["fired"] else 0  # not an error to not fire


if __name__ == "__main__":
    sys.exit(_cli())