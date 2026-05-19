"""
Signal aggregator — combines per-ticker signal results and applies the hybrid
trigger logic to decide which tickers escalate to Gate 0.

For each ticker, given the outputs of:
  - priceSignalAnalyzer (daily move + move-from-open sub-signals)
  - volumeSignalAnalyzer (volume signal)
  - cumulativeSignalAnalyzer (cumulative move signal)

Apply the hybrid trigger logic:
  - Any MAJOR signal alone fires the trigger
  - Multiple MINOR signals (configurable threshold, default 2) fire the trigger
  - CUMULATIVE signals always fire alone (they only have major tier)

Output a list of triggered tickers with rationale, suitable for passing to
Gate 0.

The aggregator is pure logic — it takes signal results in, produces a
trigger decision out. No data fetching, no LLM calls.

Usage:
    from signalAggregator import aggregate_signals_for_ticker
    decision = aggregate_signals_for_ticker(
        ticker="ARDX",
        price_signal=price_signal_result,
        volume_signal=volume_signal_result,
        cumulative_signal=cumulative_signal_result,
    )
    if decision["triggered"]:
        # this ticker should go to Gate 0
        ...

Or for the whole universe:
    summary = aggregate_signals_for_universe(per_ticker_signals)
    triggered_tickers = summary["triggered_tickers"]
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional


# ============ CONFIGURATION ============

# How many MINOR signals must fire simultaneously to trigger under the
# minor-corroboration rule. Default 2: a minor signal alone is not enough,
# two minor signals from different signal types is.
MINOR_SIGNALS_REQUIRED_FOR_TRIGGER = 2


# ============ LOGGER ============

_logger = logging.getLogger("monitor.signalAggregator")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


# ============ RESULT SHAPE ============

def _empty_decision(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "triggered": False,
        "trigger_path": None,  # "major", "minor_corroboration", "cumulative", or None
        "fired_signals": [],   # list of {"signal_type", "tier", "details"}
        "signal_summary": {
            "price_fired": False, "price_tier": None,
            "volume_fired": False, "volume_tier": None,
            "cumulative_fired": False,
            "major_count": 0,
            "minor_count": 0,
        },
        "config": {
            "minor_signals_required_for_trigger": MINOR_SIGNALS_REQUIRED_FOR_TRIGGER,
        },
        "data_quality_flags": [],
    }


# ============ PER-TICKER AGGREGATION ============

def aggregate_signals_for_ticker(
    ticker: str,
    price_signal: Optional[dict] = None,
    volume_signal: Optional[dict] = None,
    cumulative_signal: Optional[dict] = None,
) -> dict:
    """
    Apply hybrid trigger logic to one ticker's signal results.

    Trigger paths (any one fires):
      - "major": any non-cumulative signal fired at "major" tier
      - "minor_corroboration": >= MINOR_SIGNALS_REQUIRED_FOR_TRIGGER minor
                               signals fired across types
      - "cumulative": cumulative signal fired (only has major tier)

    Returns a decision dict (see _empty_decision shape).
    """
    decision = _empty_decision(ticker)

    # Collect data quality flags from upstream signals
    for sig, label in [(price_signal, "price"), (volume_signal, "volume"),
                       (cumulative_signal, "cumulative")]:
        if sig:
            for flag in sig.get("data_quality_flags", []):
                decision["data_quality_flags"].append(f"{label}_signal: {flag}")

    # Examine price signal
    if price_signal and price_signal.get("fired"):
        decision["signal_summary"]["price_fired"] = True
        decision["signal_summary"]["price_tier"] = price_signal.get("tier")
        decision["fired_signals"].append({
            "signal_type": "price",
            "tier": price_signal.get("tier"),
            "details": price_signal.get("details", {}),
        })

    # Examine volume signal
    if volume_signal and volume_signal.get("fired"):
        decision["signal_summary"]["volume_fired"] = True
        decision["signal_summary"]["volume_tier"] = volume_signal.get("tier")
        decision["fired_signals"].append({
            "signal_type": "volume",
            "tier": volume_signal.get("tier"),
            "details": volume_signal.get("details", {}),
        })

    # Examine cumulative signal (special — fires alone, no minor tier)
    if cumulative_signal and cumulative_signal.get("fired"):
        decision["signal_summary"]["cumulative_fired"] = True
        decision["fired_signals"].append({
            "signal_type": "cumulative",
            "tier": cumulative_signal.get("tier"),
            "details": cumulative_signal.get("details", {}),
        })

    # Count majors and minors among non-cumulative signals
    major_count = sum(
        1 for s in decision["fired_signals"]
        if s["signal_type"] != "cumulative" and s["tier"] == "major"
    )
    minor_count = sum(
        1 for s in decision["fired_signals"]
        if s["signal_type"] != "cumulative" and s["tier"] == "minor"
    )
    decision["signal_summary"]["major_count"] = major_count
    decision["signal_summary"]["minor_count"] = minor_count

    # ---- Apply trigger logic ----
    # Cumulative fires alone
    if decision["signal_summary"]["cumulative_fired"]:
        decision["triggered"] = True
        decision["trigger_path"] = "cumulative"
        return decision

    # Major signal fires alone
    if major_count >= 1:
        decision["triggered"] = True
        decision["trigger_path"] = "major"
        return decision

    # Multiple minor signals fire via corroboration
    if minor_count >= MINOR_SIGNALS_REQUIRED_FOR_TRIGGER:
        decision["triggered"] = True
        decision["trigger_path"] = "minor_corroboration"
        return decision

    # No trigger — no signal or only one minor
    return decision


# ============ UNIVERSE AGGREGATION ============

def aggregate_signals_for_universe(per_ticker_signals: dict) -> dict:
    """
    Apply trigger logic to many tickers at once.

    Args:
        per_ticker_signals: dict mapping ticker → dict with keys:
            - "price_signal": price analyzer output (or None)
            - "volume_signal": volume analyzer output (or None)
            - "cumulative_signal": cumulative analyzer output (or None)

    Returns:
        {
            "computed_at": ISO timestamp,
            "decisions": {ticker: decision_dict, ...},
            "triggered_tickers": [list of tickers that triggered],
            "summary": {
                "tickers_total": int,
                "tickers_triggered": int,
                "tickers_by_path": {"major": int, "minor_corroboration": int, "cumulative": int},
            },
        }
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    output = {
        "computed_at": now_iso,
        "decisions": {},
        "triggered_tickers": [],
        "summary": {
            "tickers_total": len(per_ticker_signals),
            "tickers_triggered": 0,
            "tickers_by_path": {
                "major": 0,
                "minor_corroboration": 0,
                "cumulative": 0,
            },
        },
    }

    for ticker, signals in per_ticker_signals.items():
        decision = aggregate_signals_for_ticker(
            ticker=ticker,
            price_signal=signals.get("price_signal"),
            volume_signal=signals.get("volume_signal"),
            cumulative_signal=signals.get("cumulative_signal"),
        )
        output["decisions"][ticker] = decision
        if decision["triggered"]:
            output["triggered_tickers"].append(ticker)
            output["summary"]["tickers_triggered"] += 1
            path = decision["trigger_path"]
            if path in output["summary"]["tickers_by_path"]:
                output["summary"]["tickers_by_path"][path] += 1

    return output


# ============ CLI ============

def _cli():
    """
    CLI mode: read pre-computed signal results from a JSON file, run the
    aggregator, output the trigger decisions.

    Expects input JSON like:
    {
        "ARDX": {"price_signal": {...}, "volume_signal": {...}, "cumulative_signal": {...}},
        ...
    }
    """
    parser = argparse.ArgumentParser(description="Aggregate signals and decide triggers")
    parser.add_argument("--input", required=True,
                        help="JSON file with per-ticker signal dict")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        per_ticker = json.load(f)

    result = aggregate_signals_for_universe(per_ticker)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())