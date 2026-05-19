"""
Gate 0 — mechanical macro/sector filter for the Stage 5 monitor.

For each triggered ticker, this module decides whether the ticker's move is
fully explained by broader market/sector context, or whether there's a
residual unexplained move that warrants investigation.

This is pure arithmetic — no LLM call. The job is to cheaply rule out
the most obvious cases where a ticker's move is "the whole market moved
today, this ticker came along for the ride." Anything ambiguous gets
escalated to Call 1 for LLM investigation.

Logic per ticker:
  1. Look up market_beta, sector_beta, residual_volatility from beta baseline
  2. Look up today's SPY move and sector ETF move from macro context
  3. Compute:
       expected_from_market = market_beta × SPY_move
       expected_from_sector = sector_beta × sector_move
       expected_move = max(|expected_from_market|, |expected_from_sector|), signed
                       to match the actual move's direction. (We use the
                       larger-magnitude expectation as the "best macro
                       explanation" of the move.)
       residual_move = actual_move - expected_move
  4. If |residual_move| < SKIP_IF_UNEXPLAINED_BELOW_SIGMA × residual_volatility,
     classify as "skip — broad event explained"
  5. Otherwise, classify as "investigate — residual warrants LLM"

Bias: when the math is ambiguous (missing baseline, missing macro data),
the filter defaults to "investigate" (don't skip). The cost of an
unnecessary LLM call ($0.10-0.50) is much less than missing a real event.

Gate 0 only applies to PRICE-derived triggers (where the move size is the
signal). Volume triggers and cumulative-move triggers always pass through
to Call 1, because they're not well-explained by macro/sector context.

Usage:
    from gate0Filter import apply_gate_0
    decisions = apply_gate_0(
        triggered_tickers={"ARDX": signal_decision, ...},
        beta_baselines=beta_baselines_dict,
        macro_context=macro_context_result,
    )
    for ticker, decision in decisions.items():
        if decision["action"] == "investigate":
            # send to Call 1
        else:
            # skipped — log and move on
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional


# ============ CONFIGURATION ============

# Skip threshold expressed in sigmas of residual volatility. Lower = more
# aggressive filtering (more tickers skipped); higher = more conservative
# (fewer tickers skipped, more LLM calls).
#
# 2.0 means: "if today's unexplained move is less than 2 standard deviations
# of typical unexplained moves, classify as macro-explained."
SKIP_IF_UNEXPLAINED_BELOW_SIGMA = 2.0


# ============ LOGGER ============

_logger = logging.getLogger("monitor.gate0Filter")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


# ============ RESULT SHAPE ============

def _empty_gate_decision(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        # "investigate" — escalate to Call 1
        # "skip" — broad event explains the move; don't run Call 1
        "action": "investigate",  # default to investigate (conservative bias)
        "rationale": None,         # human-readable reason
        "details": {
            "actual_move_pct": None,
            "market_beta": None,
            "sector_beta": None,
            "sector_etf_symbol": None,
            "spy_move_pct": None,
            "sector_move_pct": None,
            "expected_move_from_market_pct": None,
            "expected_move_from_sector_pct": None,
            "expected_move_pct": None,    # best-fit (max magnitude, signed)
            "residual_move_pct": None,
            "residual_volatility": None,
            "skip_threshold_pct": None,   # = sigma_multiple × residual_volatility
            "sigma_multiple": SKIP_IF_UNEXPLAINED_BELOW_SIGMA,
        },
        "data_quality_flags": [],
    }


# ============ COMPUTATIONAL HELPERS ============

def _signed_larger_magnitude(a: Optional[float],
                              b: Optional[float],
                              direction_sign: int) -> Optional[float]:
    """
    Return the value (a or b) whose magnitude is larger, with the sign
    forced to match direction_sign. Used to compute "best macro explanation"
    of an actual move: we use the larger of (market beta × market move,
    sector beta × sector move) as the explanation, signed to match the
    direction of the actual move (since betas should be positive and the
    explanation should align directionally).

    direction_sign should be +1 or -1.

    If both are None, returns None. If one is None, uses the other.
    """
    if a is None and b is None:
        return None
    if a is None:
        return abs(b) * direction_sign
    if b is None:
        return abs(a) * direction_sign

    larger_magnitude = max(abs(a), abs(b))
    return larger_magnitude * direction_sign


# ============ PER-TICKER FILTER ============

def apply_gate_0_to_ticker(
    ticker: str,
    actual_move_pct: float,
    beta_baseline: Optional[dict],
    spy_move_pct: Optional[float],
    sector_etf_symbol: Optional[str],
    sector_move_pct: Optional[float],
) -> dict:
    """
    Apply Gate 0 to one ticker.

    Args:
        ticker: ticker symbol
        actual_move_pct: today's actual daily move % for this ticker
        beta_baseline: beta baseline dict for this ticker, containing
                       market_beta, sector_beta, market_residual_stddev, etc.
                       May be None if no baseline available.
        spy_move_pct: today's SPY move % (broad market proxy)
        sector_etf_symbol: the sector ETF symbol used for this ticker
        sector_move_pct: today's sector ETF move %

    Returns:
        Gate decision dict.
    """
    decision = _empty_gate_decision(ticker)
    decision["details"]["actual_move_pct"] = actual_move_pct
    decision["details"]["sector_etf_symbol"] = sector_etf_symbol
    decision["details"]["spy_move_pct"] = spy_move_pct
    decision["details"]["sector_move_pct"] = sector_move_pct

    # ---- Validate inputs ----
    # Without baseline, can't compute expected moves → conservative default
    if beta_baseline is None:
        decision["action"] = "investigate"
        decision["rationale"] = "No beta baseline available; defaulting to investigate."
        decision["data_quality_flags"].append("No beta baseline for this ticker")
        return decision

    market_beta = beta_baseline.get("market_beta")
    sector_beta = beta_baseline.get("sector_beta")
    market_residual_stddev = beta_baseline.get("market_residual_stddev")
    sector_residual_stddev = beta_baseline.get("sector_residual_stddev")

    decision["details"]["market_beta"] = market_beta
    decision["details"]["sector_beta"] = sector_beta

    # Use the smaller of the two residual stddevs as the threshold reference
    # (more conservative — if either decomposition shows tight fit, prefer
    # that one). If only one exists, use it. If neither, conservative default.
    candidate_stddevs = [s for s in (market_residual_stddev, sector_residual_stddev)
                         if s is not None and s > 0]
    if not candidate_stddevs:
        decision["action"] = "investigate"
        decision["rationale"] = "No residual volatility data; defaulting to investigate."
        decision["data_quality_flags"].append(
            "Beta baseline has no usable residual volatility"
        )
        return decision

    residual_volatility = min(candidate_stddevs)
    decision["details"]["residual_volatility"] = residual_volatility

    # ---- Compute expected moves ----
    expected_from_market = None
    if market_beta is not None and spy_move_pct is not None:
        expected_from_market = market_beta * spy_move_pct
        decision["details"]["expected_move_from_market_pct"] = expected_from_market

    expected_from_sector = None
    if sector_beta is not None and sector_move_pct is not None:
        expected_from_sector = sector_beta * sector_move_pct
        decision["details"]["expected_move_from_sector_pct"] = expected_from_sector

    # If we have neither expected move, can't compute residual → investigate
    if expected_from_market is None and expected_from_sector is None:
        decision["action"] = "investigate"
        decision["rationale"] = (
            "Could not compute expected move from either market or sector; "
            "defaulting to investigate."
        )
        decision["data_quality_flags"].append(
            "Missing macro/sector context; gate cannot filter"
        )
        return decision

    # Use the larger-magnitude expected move as the best macro explanation,
    # signed to match the actual move's direction
    actual_sign = 1 if actual_move_pct >= 0 else -1
    expected_move = _signed_larger_magnitude(
        expected_from_market, expected_from_sector, actual_sign
    )
    decision["details"]["expected_move_pct"] = expected_move

    # ---- Compute residual ----
    residual_move = actual_move_pct - expected_move
    decision["details"]["residual_move_pct"] = residual_move

    # ---- Compare to threshold ----
    skip_threshold = SKIP_IF_UNEXPLAINED_BELOW_SIGMA * residual_volatility
    decision["details"]["skip_threshold_pct"] = skip_threshold

    if abs(residual_move) < skip_threshold:
        decision["action"] = "skip"
        decision["rationale"] = (
            f"Residual move ({residual_move:+.2f}%) is below threshold "
            f"({skip_threshold:.2f}% = {SKIP_IF_UNEXPLAINED_BELOW_SIGMA}σ × "
            f"{residual_volatility:.2f}% residual stddev). "
            f"Move appears explained by broader market/sector context."
        )
    else:
        decision["action"] = "investigate"
        decision["rationale"] = (
            f"Residual move ({residual_move:+.2f}%) exceeds threshold "
            f"({skip_threshold:.2f}% = {SKIP_IF_UNEXPLAINED_BELOW_SIGMA}σ × "
            f"{residual_volatility:.2f}% residual stddev). "
            f"Unexplained component warrants investigation."
        )

    return decision


# ============ MULTI-TICKER FILTER ============

def apply_gate_0(
    triggered_decisions: dict,
    beta_baselines: dict,
    macro_context: dict,
) -> dict:
    """
    Apply Gate 0 to multiple triggered tickers in one call.

    Args:
        triggered_decisions: dict mapping ticker → signal aggregator decision
                             (as produced by signalAggregator). Each decision
                             must have triggered=True and fired_signals.
        beta_baselines: full beta baselines dict (as produced by
                        compute_beta_baselines_for_universe). Has a "tickers"
                        key mapping ticker → beta data.
        macro_context: macro context dict (as produced by fetch_macro_context).
                       Has "indicators" key with SPY, sector ETFs, etc.

    Returns:
        {
            "computed_at": ISO timestamp,
            "decisions": {ticker: gate_decision_dict, ...},
            "summary": {
                "tickers_evaluated": int,
                "tickers_skipped": int,
                "tickers_to_investigate": int,
                "tickers_bypassed_to_investigate": int,  # not price-only triggers
            }
        }

    Gate 0 only filters tickers whose trigger came purely from price-derived
    signals. Tickers triggered by volume or cumulative signals bypass the
    filter (action="investigate") because those signals aren't well-explained
    by macro/sector context.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    output = {
        "computed_at": now_iso,
        "decisions": {},
        "summary": {
            "tickers_evaluated": len(triggered_decisions),
            "tickers_skipped": 0,
            "tickers_to_investigate": 0,
            "tickers_bypassed_to_investigate": 0,
        },
    }

    indicators = macro_context.get("indicators", {}) if macro_context else {}
    spy_indicator = indicators.get("SPY", {})
    spy_move_pct = spy_indicator.get("daily_change_pct") if spy_indicator else None

    baselines_by_ticker = beta_baselines.get("tickers", {}) if beta_baselines else {}

    for ticker, signal_decision in triggered_decisions.items():
        # Determine whether this ticker's trigger was price-only
        fired_signals = signal_decision.get("fired_signals", [])
        signal_types_fired = {s.get("signal_type") for s in fired_signals}
        is_price_only_trigger = (signal_types_fired == {"price"})

        # If trigger included volume or cumulative, bypass Gate 0
        if not is_price_only_trigger:
            decision = _empty_gate_decision(ticker)
            decision["action"] = "investigate"
            decision["rationale"] = (
                f"Trigger included non-price signals ({sorted(signal_types_fired)}); "
                "Gate 0 only applies to pure price triggers. Escalating to Call 1."
            )
            output["decisions"][ticker] = decision
            output["summary"]["tickers_to_investigate"] += 1
            output["summary"]["tickers_bypassed_to_investigate"] += 1
            continue

        # Pure price trigger — apply Gate 0
        beta_baseline = baselines_by_ticker.get(ticker)

        # Determine which sector ETF to use for this ticker
        sector_etf_symbol = None
        sector_move_pct = None
        if beta_baseline:
            sector_etf_symbol = beta_baseline.get("sector_etf_symbol")
            if sector_etf_symbol and sector_etf_symbol in indicators:
                sector_move_pct = indicators[sector_etf_symbol].get("daily_change_pct")

        # Extract the actual price move from the fired price signal details
        # Take the larger absolute of daily_move vs move_from_open as the move
        # we're trying to explain
        actual_move_pct = _extract_actual_move(fired_signals)
        if actual_move_pct is None:
            # No usable move data → conservative default
            decision = _empty_gate_decision(ticker)
            decision["action"] = "investigate"
            decision["rationale"] = (
                "Could not extract actual price move from signal details; "
                "defaulting to investigate."
            )
            decision["data_quality_flags"].append(
                "No actual_move_pct extractable from price signal"
            )
            output["decisions"][ticker] = decision
            output["summary"]["tickers_to_investigate"] += 1
            continue

        decision = apply_gate_0_to_ticker(
            ticker=ticker,
            actual_move_pct=actual_move_pct,
            beta_baseline=beta_baseline,
            spy_move_pct=spy_move_pct,
            sector_etf_symbol=sector_etf_symbol,
            sector_move_pct=sector_move_pct,
        )
        output["decisions"][ticker] = decision

        if decision["action"] == "skip":
            output["summary"]["tickers_skipped"] += 1
        else:
            output["summary"]["tickers_to_investigate"] += 1

    return output


def _extract_actual_move(fired_signals: list) -> Optional[float]:
    """
    Pull the actual price move % from the fired signals list. Uses daily_move
    if available, falls back to move_from_open, returns the one with larger
    magnitude.
    """
    daily_move = None
    move_from_open = None
    for sig in fired_signals:
        if sig.get("signal_type") != "price":
            continue
        details = sig.get("details", {})
        d = details.get("daily_move", {})
        if d.get("current_pct") is not None:
            daily_move = d["current_pct"]
        mfo = details.get("move_from_open", {})
        if mfo.get("current_pct") is not None:
            move_from_open = mfo["current_pct"]

    if daily_move is None and move_from_open is None:
        return None
    if daily_move is None:
        return move_from_open
    if move_from_open is None:
        return daily_move
    # Both present — use larger magnitude
    return daily_move if abs(daily_move) >= abs(move_from_open) else move_from_open


# ============ CLI ============

def _cli():
    """
    CLI mode: read pre-computed signal/baseline/macro data from JSON files
    and apply Gate 0. Useful for inspecting filter behavior on sample data.
    """
    parser = argparse.ArgumentParser(description="Apply Gate 0 macro filter")
    parser.add_argument("--triggered", required=True,
                        help="JSON file with triggered_decisions dict")
    parser.add_argument("--baselines", required=True,
                        help="JSON file with beta_baselines dict")
    parser.add_argument("--macro", required=True,
                        help="JSON file with macro_context dict")
    args = parser.parse_args()

    with open(args.triggered, "r", encoding="utf-8") as f:
        triggered = json.load(f)
    with open(args.baselines, "r", encoding="utf-8") as f:
        baselines = json.load(f)
    with open(args.macro, "r", encoding="utf-8") as f:
        macro = json.load(f)

    result = apply_gate_0(triggered, baselines, macro)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())