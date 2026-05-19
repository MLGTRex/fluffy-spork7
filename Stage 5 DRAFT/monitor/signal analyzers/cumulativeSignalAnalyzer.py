"""
Cumulative signal analyzer — checks if a ticker's price has drifted enough
from its evaluation anchor to warrant re-examination, even if no single-day
move was extreme.

Catches the "slow erosion" failure mode: a ticker drifts 1% per day for two
weeks, never trips a single-day threshold, but ends up 15% away from where
the thesis was built. The thesis at the new price might be quite different
from the thesis at the old price.

Two parallel triggers:
  - Absolute threshold (e.g., ±15% from anchor price)
  - Sigma-scaled threshold (cumulative move > N × daily sigma)

Either is sufficient to fire (OR-logic between them).

The signal fires as "major" tier regardless — there's no minor/major
distinction for cumulative. If the cumulative move is significant enough
to cross the threshold, it's a major signal.

Inputs:
  - Current price (from priceData)
  - Evaluation anchor for this ticker (from ticker_evaluation_anchors.json)
  - The ticker's percentile baseline (for daily sigma — pulled from the
    stddev of the signed-moves distribution)

Usage:
    from cumulativeSignalAnalyzer import analyze_cumulative_signal
    result = analyze_cumulative_signal(
        ticker="ARDX",
        current_price=10.20,
        anchor={"evaluated_at": "2026-04-15T...", "evaluated_at_price": 12.45},
        baseline=ticker_baseline,
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

# Absolute cumulative move threshold (% from anchor). Either positive or
# negative direction fires.
ABS_THRESHOLD_PCT = 15.0

# Sigma-scaled threshold: cumulative move > N × daily sigma. This adjusts
# for ticker volatility automatically — a volatile name needs a larger move
# to be significant, a stable name fires sooner.
SIGMA_THRESHOLD_MULTIPLE = 10.0


# ============ LOGGER ============

_logger = logging.getLogger("monitor.cumulativeSignalAnalyzer")
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
        "signal_type": "cumulative",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "anchor_evaluated_at": None,
        "anchor_price": None,
        "current_price": None,
        "fired": False,
        "tier": None,
        "details": {
            "cumulative_move_pct": None,
            "cumulative_move_abs": None,
            "abs_threshold_pct": ABS_THRESHOLD_PCT,
            "abs_threshold_fired": False,
            "daily_sigma": None,
            "sigma_threshold_multiple": SIGMA_THRESHOLD_MULTIPLE,
            "sigma_threshold_pct": None,  # = daily_sigma * SIGMA_THRESHOLD_MULTIPLE
            "sigma_threshold_fired": False,
            "fired_via": None,  # "abs", "sigma", or "both"
        },
        "data_quality_flags": [],
    }


# ============ MAIN ANALYSIS ============

def analyze_cumulative_signal(
    ticker: str,
    current_price: Optional[float],
    anchor: Optional[dict],
    baseline: Optional[dict] = None,
) -> dict:
    """
    Analyze cumulative move since last evaluation.

    Args:
        ticker: ticker symbol
        current_price: current/latest price for this ticker
        anchor: evaluation anchor dict from ticker_evaluation_anchors.json,
                shape: {"evaluated_at": ISO, "evaluated_at_price": float, ...}
                or None if no anchor exists for this ticker yet
        baseline: percentile baseline dict (used for daily sigma).
                  If None, only absolute threshold can fire.

    Returns:
        Signal result dict.
    """
    result = _empty_result(ticker)
    result["current_price"] = current_price

    if current_price is None:
        result["data_quality_flags"].append("No current price provided; signal cannot fire")
        return result

    if not anchor:
        result["data_quality_flags"].append(
            "No evaluation anchor for this ticker; signal cannot fire. "
            "Anchor will be created on next successful pipeline rerun."
        )
        return result

    anchor_price = anchor.get("evaluated_at_price")
    anchor_date = anchor.get("evaluated_at")
    result["anchor_evaluated_at"] = anchor_date
    result["anchor_price"] = anchor_price

    if anchor_price is None or not isinstance(anchor_price, (int, float)) or anchor_price <= 0:
        result["data_quality_flags"].append(
            f"Anchor price is invalid: {anchor_price!r}; signal cannot fire"
        )
        return result

    # ---- Compute cumulative move ----
    cumulative_abs = current_price - anchor_price
    cumulative_pct = (cumulative_abs / anchor_price) * 100.0
    result["details"]["cumulative_move_abs"] = cumulative_abs
    result["details"]["cumulative_move_pct"] = cumulative_pct

    # ---- Absolute threshold check ----
    abs_threshold_fired = abs(cumulative_pct) >= ABS_THRESHOLD_PCT
    result["details"]["abs_threshold_fired"] = abs_threshold_fired

    # ---- Sigma threshold check ----
    sigma_threshold_fired = False
    daily_sigma = None
    if baseline is not None:
        daily_sigma = baseline.get("daily_move_pct", {}).get("stddev")
        if daily_sigma is not None and daily_sigma > 0:
            sigma_threshold_pct = daily_sigma * SIGMA_THRESHOLD_MULTIPLE
            result["details"]["daily_sigma"] = daily_sigma
            result["details"]["sigma_threshold_pct"] = sigma_threshold_pct
            sigma_threshold_fired = abs(cumulative_pct) >= sigma_threshold_pct
            result["details"]["sigma_threshold_fired"] = sigma_threshold_fired
        else:
            result["data_quality_flags"].append(
                "Baseline has no daily sigma; sigma-based cumulative threshold cannot fire"
            )
    else:
        result["data_quality_flags"].append(
            "No baseline provided; sigma-based cumulative threshold cannot fire"
        )

    # ---- Determine final firing status ----
    if abs_threshold_fired and sigma_threshold_fired:
        result["fired"] = True
        result["tier"] = "major"
        result["details"]["fired_via"] = "both"
    elif abs_threshold_fired:
        result["fired"] = True
        result["tier"] = "major"
        result["details"]["fired_via"] = "abs"
    elif sigma_threshold_fired:
        result["fired"] = True
        result["tier"] = "major"
        result["details"]["fired_via"] = "sigma"

    return result


# ============ ANCHOR FILE LOADING ============

def load_anchors(anchors_path: str) -> dict:
    """
    Load the ticker_evaluation_anchors.json file.

    Returns a dict (ticker → anchor record), or empty dict if file missing or
    malformed.
    """
    if not os.path.exists(anchors_path):
        return {}
    try:
        with open(anchors_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError) as e:
        _logger.warning(f"Could not load anchors from {anchors_path}: {e}")
        return {}


# ============ CLI ============

def _cli():
    parser = argparse.ArgumentParser(description="Analyze cumulative signal for a ticker")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--current-price", type=float, help="Override current price")
    parser.add_argument("--anchor-price", type=float, help="Override anchor price")
    parser.add_argument("--anchor-date", default="manual",
                        help="Override anchor date (for manual testing)")
    parser.add_argument("--lookback-days", type=int, default=60)
    args = parser.parse_args()

    # If both prices given, run with manual inputs
    if args.current_price is not None and args.anchor_price is not None:
        anchor = {"evaluated_at": args.anchor_date,
                  "evaluated_at_price": args.anchor_price}
        result = analyze_cumulative_signal(
            args.ticker, args.current_price, anchor
        )
        print(json.dumps(result, indent=2, default=str))
        return 0

    # Otherwise, try to fetch live
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

    # Look for the anchors file
    anchors_path = os.path.normpath(
        os.path.join(monitor_dir, "state", "ticker_evaluation_anchors.json")
    )
    anchors = load_anchors(anchors_path)
    anchor = anchors.get(args.ticker)
    if not anchor:
        print(f"No anchor found for {args.ticker} in {anchors_path}", file=sys.stderr)
        print(f"(Hint: pass --current-price and --anchor-price for manual test)",
              file=sys.stderr)
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

    current_price = price_result.get("current", {}).get("price")
    result = analyze_cumulative_signal(args.ticker, current_price, anchor, baseline)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())