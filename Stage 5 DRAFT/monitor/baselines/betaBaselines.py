"""
Beta baselines — per-ticker market and sector beta regression for the Stage 5
monitor's Gate 0 (mechanical macro filter).

For each ticker, computes:
  - Market beta: regression slope of daily ticker returns vs SPY daily returns
  - Sector beta: regression slope of daily ticker returns vs sector ETF returns
  - Residual volatility: stddev of regression residuals (what's NOT explained
    by market + sector moves)

Used by Gate 0 to decide: "is today's ticker move explainable by the broader
market/sector move, or is there an unexplained residual that warrants
investigation?"

Concretely, for a triggered ticker today:
  expected_move = max(
      market_beta × SPY_move_today,
      sector_beta × sector_etf_move_today
  )
  residual_move = actual_ticker_move - expected_move

If |residual_move| < SKIP_SIGMA × residual_volatility, the move is explained
by macro/sector context and Gate 0 marks the ticker as "skip". Otherwise,
the ticker is escalated to Call 1 for LLM investigation.

Sector mapping comes from a separate config (sector_etf_map.json), populated
based on Stage 1's sector classification of each ticker. ASX tickers fall
back to a broad-market proxy (IOZ.AX) when sector-specific ETFs aren't
available.

Refreshed daily, not per cadence — the regression doesn't shift significantly
within a single trading day.

Designed to be:
  - Pure computation: takes return arrays as input, returns regression dict
  - Modular: separable regression step + persistence step
  - Date-aligned: handles tickers and reference ETFs that may have
    different available dates (e.g., ASX holidays)
"""

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import List, Optional


# ============ CONFIGURATION ============

DEFAULT_LOOKBACK_DAYS = 60

# Minimum number of paired daily returns required to compute a meaningful
# beta. Below this, beta estimates are noisy and unreliable. We still produce
# a baseline but flag it as low-confidence.
MIN_DATA_POINTS_FOR_BETA = 30

# Fallback symbol when a sector ETF can't be determined for a ticker
# (e.g., a sector we don't have mapped, or empty sector classification).
# Using the broad market is a conservative choice — Gate 0 will compare
# residuals against the wider market rather than refusing to filter.
DEFAULT_FALLBACK_SECTOR_SYMBOL = "SPY"

# Fallback symbol for ASX-listed tickers (those ending in .AX). Sector ETFs
# for ASX are less liquid; using the broad ASX 200 ETF (IOZ.AX) as the
# sector proxy is a reasonable approximation.
ASX_FALLBACK_SECTOR_SYMBOL = "IOZ.AX"


# ============ LOGGER ============

_logger = logging.getLogger("monitor.betaBaselines")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


# ============ SECTOR MAPPING ============

# Default mapping from Stage 1's sector classifications to SPDR sector ETF
# symbols. This is the canonical mapping; can be overridden by a config file.
DEFAULT_SECTOR_ETF_MAP = {
    "Healthcare": "XLV",
    "Technology": "XLK",
    "Financials": "XLF",
    "Financial Services": "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    "Communications": "XLC",
}


def resolve_sector_etf(ticker: str, sector: Optional[str],
                       sector_etf_map: Optional[dict] = None) -> str:
    """
    Determine the appropriate sector ETF symbol for a ticker.

    Args:
        ticker: ticker symbol (used to detect ASX vs US listing)
        sector: sector classification string from Stage 1
        sector_etf_map: optional override mapping; defaults to DEFAULT_SECTOR_ETF_MAP

    Returns: the ETF symbol to use as the sector proxy.
    """
    if sector_etf_map is None:
        sector_etf_map = DEFAULT_SECTOR_ETF_MAP

    if sector and sector in sector_etf_map:
        return sector_etf_map[sector]

    # ASX-listed: fall back to broad ASX index rather than US-market default
    if ticker and ticker.endswith(".AX"):
        return ASX_FALLBACK_SECTOR_SYMBOL

    # Otherwise fall back to broad market
    return DEFAULT_FALLBACK_SECTOR_SYMBOL


# ============ RETURNS COMPUTATION ============

def _compute_daily_returns(daily_closes: List[dict]) -> List[dict]:
    """
    From a list of daily bars with 'date' and 'close', produce a list of
    daily returns: each entry has 'date' (the day of the return) and
    'return_pct' (pct change from the prior day's close).

    Chronological order; first bar excluded (no prior).
    """
    returns = []
    for i in range(1, len(daily_closes)):
        prev = daily_closes[i - 1].get("close")
        cur = daily_closes[i].get("close")
        cur_date = daily_closes[i].get("date")
        if prev is None or cur is None or cur_date is None:
            continue
        if not isinstance(prev, (int, float)) or not isinstance(cur, (int, float)):
            continue
        if prev == 0:
            continue
        returns.append({
            "date": cur_date,
            "return_pct": ((cur - prev) / prev) * 100.0,
        })
    return returns


def _pair_returns_by_date(ticker_returns: List[dict],
                          reference_returns: List[dict]) -> tuple:
    """
    Align two return series by date. Returns (ticker_vals, ref_vals) where
    both are lists of the same length, ordered by date, including only dates
    present in both series.
    """
    ref_by_date = {r["date"]: r["return_pct"] for r in reference_returns}
    ticker_vals = []
    ref_vals = []
    for tr in ticker_returns:
        d = tr["date"]
        if d in ref_by_date:
            ticker_vals.append(tr["return_pct"])
            ref_vals.append(ref_by_date[d])
    return ticker_vals, ref_vals


# ============ REGRESSION ============

def _ols_regression(y: List[float], x: List[float]) -> dict:
    """
    Ordinary least squares regression of y on x: y = alpha + beta * x + residual.

    Returns:
        {
            "alpha": intercept (or None if can't compute),
            "beta": slope (or None),
            "residuals": list of y_i - (alpha + beta * x_i),
            "residual_stddev": stddev of residuals (or None),
            "n": number of pairs used,
        }
    """
    n = len(y)
    if n != len(x) or n < 2:
        return {"alpha": None, "beta": None, "residuals": [],
                "residual_stddev": None, "n": n}

    mean_y = sum(y) / n
    mean_x = sum(x) / n

    # beta = sum((x_i - mean_x)(y_i - mean_y)) / sum((x_i - mean_x)^2)
    numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    denominator = sum((x[i] - mean_x) ** 2 for i in range(n))

    if denominator == 0:
        # All x values identical; beta undefined
        return {"alpha": None, "beta": None, "residuals": [],
                "residual_stddev": None, "n": n}

    beta = numerator / denominator
    alpha = mean_y - beta * mean_x

    residuals = [y[i] - (alpha + beta * x[i]) for i in range(n)]
    # Sample stddev of residuals (n-2 degrees of freedom for OLS but n-1 is
    # also acceptable for an estimate; use n-1 for consistency with
    # percentileBaselines)
    if n < 2:
        residual_stddev = 0.0
    else:
        mean_resid = sum(residuals) / n
        variance = sum((r - mean_resid) ** 2 for r in residuals) / (n - 1)
        residual_stddev = variance ** 0.5

    return {
        "alpha": alpha,
        "beta": beta,
        "residuals": residuals,
        "residual_stddev": residual_stddev,
        "n": n,
    }


# ============ BASELINE COMPUTATION ============

def compute_beta_baseline_for_ticker(
    ticker: str,
    sector: Optional[str],
    ticker_daily_bars: List[dict],
    market_daily_bars: List[dict],
    sector_daily_bars: List[dict],
    sector_etf_symbol: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """
    Compute beta baseline for one ticker.

    Args:
        ticker: ticker symbol
        sector: Stage 1's sector classification (used for output reference;
                sector_etf_symbol is the actual ETF used)
        ticker_daily_bars: list of {"date", "close"} for the ticker
        market_daily_bars: list of {"date", "close"} for the market proxy (SPY)
        sector_daily_bars: list of {"date", "close"} for the sector ETF
        sector_etf_symbol: the symbol used for sector (for reference)
        lookback_days: cap on regression window

    Returns:
        Beta baseline dict with market_beta, sector_beta, residual_volatility, etc.
    """
    result = {
        "ticker": ticker,
        "sector": sector,
        "sector_etf_symbol": sector_etf_symbol,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days_requested": lookback_days,
        "market_beta": None,
        "market_residual_stddev": None,
        "market_regression_n": 0,
        "sector_beta": None,
        "sector_residual_stddev": None,
        "sector_regression_n": 0,
        "data_quality_flags": [],
    }

    # Compute returns
    ticker_returns = _compute_daily_returns(ticker_daily_bars)
    market_returns = _compute_daily_returns(market_daily_bars)
    sector_returns = _compute_daily_returns(sector_daily_bars)

    # Truncate to lookback window (keep most recent)
    if lookback_days:
        ticker_returns = ticker_returns[-lookback_days:]
        market_returns = market_returns[-lookback_days:]
        sector_returns = sector_returns[-lookback_days:]

    # ---- Market beta ----
    t_vals, m_vals = _pair_returns_by_date(ticker_returns, market_returns)
    market_reg = _ols_regression(t_vals, m_vals)
    result["market_beta"] = market_reg["beta"]
    result["market_residual_stddev"] = market_reg["residual_stddev"]
    result["market_regression_n"] = market_reg["n"]

    if market_reg["n"] < MIN_DATA_POINTS_FOR_BETA:
        result["data_quality_flags"].append(
            f"Insufficient paired returns for market beta: {market_reg['n']} pairs "
            f"(need {MIN_DATA_POINTS_FOR_BETA}+)"
        )
    elif market_reg["beta"] is None:
        result["data_quality_flags"].append("Market beta could not be computed (degenerate data)")

    # ---- Sector beta ----
    t_vals, s_vals = _pair_returns_by_date(ticker_returns, sector_returns)
    sector_reg = _ols_regression(t_vals, s_vals)
    result["sector_beta"] = sector_reg["beta"]
    result["sector_residual_stddev"] = sector_reg["residual_stddev"]
    result["sector_regression_n"] = sector_reg["n"]

    if sector_reg["n"] < MIN_DATA_POINTS_FOR_BETA:
        result["data_quality_flags"].append(
            f"Insufficient paired returns for sector beta: {sector_reg['n']} pairs"
        )
    elif sector_reg["beta"] is None:
        result["data_quality_flags"].append("Sector beta could not be computed (degenerate data)")

    return result


# ============ MULTI-TICKER COMPUTATION ============

def compute_beta_baselines_for_universe(
    per_ticker_inputs: dict,
    market_daily_bars: List[dict],
    sector_bars_by_symbol: dict,
    sector_etf_map: Optional[dict] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """
    Compute beta baselines for many tickers in one call.

    Args:
        per_ticker_inputs: dict mapping ticker symbol → dict with:
            - "sector": Stage 1's sector classification
            - "daily_bars": list of {"date", "close"} for the ticker
        market_daily_bars: SPY daily bars (used as market proxy for all)
        sector_bars_by_symbol: dict mapping ETF symbol → daily bars list,
                               for all sector ETFs and fallbacks (SPY, IOZ.AX, etc.)
        sector_etf_map: optional override of DEFAULT_SECTOR_ETF_MAP
        lookback_days: passed through

    Returns:
        {
            "computed_at": ISO timestamp,
            "lookback_days": int,
            "sector_etf_map_used": dict,
            "tickers": {ticker: beta_baseline_dict, ...},
            "summary": {
                "tickers_total": int,
                "tickers_with_full_beta": int,
                "tickers_with_flags": int,
            }
        }
    """
    if sector_etf_map is None:
        sector_etf_map = DEFAULT_SECTOR_ETF_MAP

    now_iso = datetime.now(timezone.utc).isoformat()
    output = {
        "computed_at": now_iso,
        "lookback_days": lookback_days,
        "sector_etf_map_used": sector_etf_map,
        "tickers": {},
        "summary": {
            "tickers_total": len(per_ticker_inputs),
            "tickers_with_full_beta": 0,
            "tickers_with_flags": 0,
        },
    }

    for ticker, inputs in per_ticker_inputs.items():
        sector = inputs.get("sector")
        ticker_bars = inputs.get("daily_bars", [])
        sector_etf = resolve_sector_etf(ticker, sector, sector_etf_map)
        sector_bars = sector_bars_by_symbol.get(sector_etf, [])

        if not sector_bars:
            # Sector ETF data not available; flag and continue with whatever
            # we can compute
            _logger.warning(
                f"{ticker}: sector ETF '{sector_etf}' has no bars; "
                f"sector beta will be unavailable"
            )

        baseline = compute_beta_baseline_for_ticker(
            ticker=ticker,
            sector=sector,
            ticker_daily_bars=ticker_bars,
            market_daily_bars=market_daily_bars,
            sector_daily_bars=sector_bars,
            sector_etf_symbol=sector_etf,
            lookback_days=lookback_days,
        )

        if not sector_bars:
            baseline["data_quality_flags"].append(
                f"Sector ETF {sector_etf} bars not provided; sector beta unavailable"
            )

        output["tickers"][ticker] = baseline

        if (baseline["market_beta"] is not None
                and baseline["sector_beta"] is not None):
            output["summary"]["tickers_with_full_beta"] += 1
        if baseline["data_quality_flags"]:
            output["summary"]["tickers_with_flags"] += 1

    return output


# ============ PERSISTENCE ============

def _atomic_write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    parent = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".beta_baselines.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def save_beta_baselines(baselines: dict, output_path: str) -> None:
    """Persist baselines dict atomically."""
    _atomic_write_json(output_path, baselines)
    _logger.info(f"Wrote beta baselines to {output_path}")


def load_beta_baselines(input_path: str) -> Optional[dict]:
    """Load baselines from JSON file. Returns None on missing/malformed."""
    if not os.path.exists(input_path):
        return None
    try:
        with open(input_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _logger.warning(f"Could not load beta baselines from {input_path}: {e}")
        return None


# ============ CLI ============

def _cli():
    """
    CLI mode: fetch live data via the data source modules and compute beta
    baseline for a single ticker.
    """
    parser = argparse.ArgumentParser(description="Compute beta baseline for a ticker")
    parser.add_argument("--ticker", required=True, help="Ticker symbol")
    parser.add_argument("--sector", default=None,
                        help="Sector classification (used to pick sector ETF)")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_sources_dir = os.path.normpath(os.path.join(script_dir, "..", "data sources"))
    sys.path.insert(0, data_sources_dir)
    try:
        from priceData import fetch_price_data
        from macroContext import fetch_macro_context
    except ImportError as e:
        print(f"Could not import data sources: {e}", file=sys.stderr)
        return 1

    print(f"Fetching data for {args.ticker}...", file=sys.stderr)
    ticker_result = fetch_price_data(args.ticker, lookback_days=args.lookback_days)
    macro_result = fetch_macro_context(lookback_days=args.lookback_days)

    sector_etf = resolve_sector_etf(args.ticker, args.sector)

    # Extract bars
    market_bars = []
    if macro_result.get("indicators", {}).get("SPY"):
        market_bars = macro_result["indicators"]["SPY"]["historical"]["daily_closes"]
    sector_bars = []
    if macro_result.get("indicators", {}).get(sector_etf):
        sector_bars = macro_result["indicators"][sector_etf]["historical"]["daily_closes"]

    baseline = compute_beta_baseline_for_ticker(
        ticker=args.ticker,
        sector=args.sector,
        ticker_daily_bars=ticker_result["historical"]["daily_bars"],
        market_daily_bars=market_bars,
        sector_daily_bars=sector_bars,
        sector_etf_symbol=sector_etf,
        lookback_days=args.lookback_days,
    )

    print(json.dumps(baseline, indent=2, default=str))
    return 0 if baseline["market_beta"] is not None else 1


if __name__ == "__main__":
    sys.exit(_cli())