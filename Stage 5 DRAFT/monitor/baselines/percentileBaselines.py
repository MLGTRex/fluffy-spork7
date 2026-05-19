"""
Percentile baselines — per-ticker percentile distributions of daily moves and
volumes for the Stage 5 monitor.

For each ticker, computes the historical distribution of:
  - Daily price moves (signed pct change from prior close)
  - Daily absolute price moves (magnitude of pct change, sign ignored)
  - Daily volumes

These distributions are the basis for percentile thresholds used by signal
analyzers. "A 3.5% move is the 96.7th percentile for ARDX" tells us whether
today's move is unusual relative to this ticker's normal behavior.

Earnings days are excluded from the distributions. Earnings-day moves have a
known cause (the earnings event itself) and don't represent the kind of
"unexpected" move the watcher is trying to detect. Including them would
inflate the upper percentiles and reduce sensitivity to genuinely anomalous
non-earnings moves.

Refreshed daily, not per cadence — the distribution shifts by one trading
day per day, not in 5-minute increments.

Designed to be:
  - Pure computation: takes historical data as input, returns baseline dict
  - Persistable: separate functions for compute and for cache load/save
  - Inspectable: CLI for one-ticker baseline computation against synthetic
    or real data via the data source fetchers

Usage:
    from percentileBaselines import compute_baseline_for_ticker
    baseline = compute_baseline_for_ticker(
        ticker="ARDX",
        daily_bars=[{"date": "...", "close": ...}, ...],
        daily_volumes=[{"date": "...", "volume": ...}, ...],
        earnings_dates=["2026-02-15", ...],
    )
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

# Days of historical data to use when computing baselines. Should match what
# the data sources fetch by default. Configurable so baselines can be tuned
# without re-fetching data.
DEFAULT_LOOKBACK_DAYS = 60

# Percentiles to compute. These are the breakpoints the signal analyzers
# will compare current values against.
PERCENTILES_TO_COMPUTE = [50, 75, 90, 95, 99]

# Minimum data points required to compute a meaningful baseline. Below this,
# percentile estimates are unreliable. We still produce a baseline but flag
# it as low-confidence.
MIN_DATA_POINTS = 30


# ============ LOGGER ============

_logger = logging.getLogger("monitor.percentileBaselines")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


# ============ PERCENTILE COMPUTATION ============

def _percentile(values: List[float], pct: float) -> Optional[float]:
    """
    Compute the pct percentile of values using linear interpolation between
    adjacent ranks. pct is in 0-100. Returns None if values is empty.

    Equivalent to numpy.percentile(values, pct, method='linear') but
    implemented without a numpy dependency at the baseline computation layer.
    """
    if not values:
        return None
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return float(sorted_vals[0])
    # Linear interpolation: rank = (pct/100) * (n-1)
    rank = (pct / 100.0) * (n - 1)
    lower_idx = int(rank)
    upper_idx = min(lower_idx + 1, n - 1)
    frac = rank - lower_idx
    return float(sorted_vals[lower_idx] * (1 - frac) + sorted_vals[upper_idx] * frac)


def _compute_percentile_dict(values: List[float]) -> dict:
    """Return {pXX: value, ...} dict for each configured percentile."""
    return {
        f"p{p}": _percentile(values, p)
        for p in PERCENTILES_TO_COMPUTE
    }


# ============ DAILY MOVE COMPUTATION ============

def _compute_daily_moves(daily_bars: List[dict]) -> List[dict]:
    """
    From a list of daily bars (each with 'date' and 'close'), produce a list
    of daily moves: each entry has 'date' (the day of the move) and
    'move_pct' (pct change from the prior day's close).

    The first bar has no prior, so it's excluded from the move list.

    Bars should already be in chronological order (oldest first).
    """
    moves = []
    for i in range(1, len(daily_bars)):
        prev_close = daily_bars[i - 1].get("close")
        cur_close = daily_bars[i].get("close")
        cur_date = daily_bars[i].get("date")
        if prev_close is None or cur_close is None or cur_date is None:
            continue
        if not isinstance(prev_close, (int, float)) or not isinstance(cur_close, (int, float)):
            continue
        if prev_close == 0:
            continue
        move_pct = ((cur_close - prev_close) / prev_close) * 100.0
        moves.append({"date": cur_date, "move_pct": move_pct})
    return moves


def _filter_earnings_days(records: List[dict], earnings_dates: List[str]) -> tuple:
    """
    Filter out records whose date is in earnings_dates.

    Returns (filtered_records, excluded_count).
    """
    if not earnings_dates:
        return records, 0
    earnings_set = set(earnings_dates)
    filtered = []
    excluded = 0
    for r in records:
        if r.get("date") in earnings_set:
            excluded += 1
        else:
            filtered.append(r)
    return filtered, excluded


# ============ BASELINE COMPUTATION ============

def compute_baseline_for_ticker(
    ticker: str,
    daily_bars: List[dict],
    daily_volumes: List[dict],
    earnings_dates: Optional[List[str]] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """
    Compute the percentile baseline for one ticker.

    Args:
        ticker: ticker symbol
        daily_bars: list of {"date": "YYYY-MM-DD", "close": float, ...}
                    chronologically ordered (oldest first). Other fields
                    optional and ignored.
        daily_volumes: list of {"date": "YYYY-MM-DD", "volume": int, ...}
                       chronologically ordered.
        earnings_dates: list of earnings date strings (YYYY-MM-DD) to exclude
                        from the distribution. If None, no exclusion.
        lookback_days: cap on the number of recent records to use. If the
                       input has more, only the most recent N are used.

    Returns:
        Per-ticker baseline dict (see module docstring). All percentile
        values may be None if data is insufficient.
    """
    earnings_dates = earnings_dates or []

    result = {
        "ticker": ticker,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days_requested": lookback_days,
        "data_points_used": {
            "daily_moves": 0,
            "daily_volumes": 0,
        },
        "earnings_days_excluded": {
            "from_moves": 0,
            "from_volumes": 0,
        },
        "daily_move_pct": {
            "signed": _empty_percentile_dict(),
            "absolute": _empty_percentile_dict(),
            "min": None,
            "max": None,
            "mean": None,
            "stddev": None,
        },
        "daily_volume": {
            "signed": _empty_percentile_dict(),  # named for consistency; same as values
            "min": None,
            "max": None,
            "mean": None,
            "stddev": None,
        },
        "data_quality_flags": [],
    }

    # ---- Daily moves ----
    moves = _compute_daily_moves(daily_bars)
    if lookback_days and len(moves) > lookback_days:
        moves = moves[-lookback_days:]
    moves, moves_excluded = _filter_earnings_days(moves, earnings_dates)
    result["earnings_days_excluded"]["from_moves"] = moves_excluded
    result["data_points_used"]["daily_moves"] = len(moves)

    if len(moves) >= MIN_DATA_POINTS:
        move_values = [m["move_pct"] for m in moves]
        abs_values = [abs(v) for v in move_values]
        result["daily_move_pct"]["signed"] = _compute_percentile_dict(move_values)
        result["daily_move_pct"]["absolute"] = _compute_percentile_dict(abs_values)
        result["daily_move_pct"]["min"] = min(move_values)
        result["daily_move_pct"]["max"] = max(move_values)
        result["daily_move_pct"]["mean"] = sum(move_values) / len(move_values)
        result["daily_move_pct"]["stddev"] = _stddev(move_values)
    elif len(moves) > 0:
        result["data_quality_flags"].append(
            f"Insufficient daily moves ({len(moves)}) for stable percentiles; "
            f"need at least {MIN_DATA_POINTS}"
        )
    else:
        result["data_quality_flags"].append("No daily move data available")

    # ---- Daily volumes ----
    volume_records = list(daily_volumes)  # copy
    if lookback_days and len(volume_records) > lookback_days:
        volume_records = volume_records[-lookback_days:]
    volume_records, vol_excluded = _filter_earnings_days(volume_records, earnings_dates)
    result["earnings_days_excluded"]["from_volumes"] = vol_excluded
    result["data_points_used"]["daily_volumes"] = len(volume_records)

    if len(volume_records) >= MIN_DATA_POINTS:
        vol_values = [v["volume"] for v in volume_records
                      if isinstance(v.get("volume"), (int, float))]
        if vol_values:
            result["daily_volume"]["signed"] = _compute_percentile_dict(vol_values)
            result["daily_volume"]["min"] = min(vol_values)
            result["daily_volume"]["max"] = max(vol_values)
            result["daily_volume"]["mean"] = sum(vol_values) / len(vol_values)
            result["daily_volume"]["stddev"] = _stddev(vol_values)
    elif len(volume_records) > 0:
        result["data_quality_flags"].append(
            f"Insufficient daily volumes ({len(volume_records)}) for stable percentiles"
        )
    else:
        result["data_quality_flags"].append("No daily volume data available")

    return result


def _empty_percentile_dict() -> dict:
    return {f"p{p}": None for p in PERCENTILES_TO_COMPUTE}


def _stddev(values: List[float]) -> float:
    """Sample stddev. Returns 0.0 if n < 2."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return variance ** 0.5


# ============ MULTI-TICKER COMPUTATION ============

def compute_baselines_for_universe(
    per_ticker_inputs: dict,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """
    Compute baselines for many tickers in one call.

    Args:
        per_ticker_inputs: dict mapping ticker symbol → dict with keys:
            - "daily_bars": list of daily price bars
            - "daily_volumes": list of daily volume bars
            - "earnings_dates": list of earnings date strings (optional)
        lookback_days: passed through to each ticker's computation

    Returns:
        {
            "computed_at": ISO timestamp,
            "lookback_days": int,
            "tickers": {ticker: baseline_dict, ...},
            "summary": {
                "tickers_total": int,
                "tickers_with_baseline": int,
                "tickers_with_flags": int,
            }
        }
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    output = {
        "computed_at": now_iso,
        "lookback_days": lookback_days,
        "tickers": {},
        "summary": {
            "tickers_total": len(per_ticker_inputs),
            "tickers_with_baseline": 0,
            "tickers_with_flags": 0,
        },
    }

    for ticker, inputs in per_ticker_inputs.items():
        baseline = compute_baseline_for_ticker(
            ticker=ticker,
            daily_bars=inputs.get("daily_bars", []),
            daily_volumes=inputs.get("daily_volumes", []),
            earnings_dates=inputs.get("earnings_dates", []),
            lookback_days=lookback_days,
        )
        output["tickers"][ticker] = baseline
        if baseline["data_points_used"]["daily_moves"] >= MIN_DATA_POINTS:
            output["summary"]["tickers_with_baseline"] += 1
        if baseline["data_quality_flags"]:
            output["summary"]["tickers_with_flags"] += 1

    return output


# ============ PERSISTENCE ============

def _atomic_write_json(path: str, data) -> None:
    """Write JSON atomically (temp file + rename)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    parent = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".percentile_baselines.", suffix=".tmp", dir=parent)
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


def save_baselines(baselines: dict, output_path: str) -> None:
    """Persist the baselines dict atomically to output_path."""
    _atomic_write_json(output_path, baselines)
    _logger.info(f"Wrote percentile baselines to {output_path}")


def load_baselines(input_path: str) -> Optional[dict]:
    """Load baselines from a JSON file. Returns None if file missing or unparseable."""
    if not os.path.exists(input_path):
        return None
    try:
        with open(input_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _logger.warning(f"Could not load baselines from {input_path}: {e}")
        return None


# ============ CLI ============

def _cli():
    """
    CLI mode: fetch live data via the data source modules and compute a
    baseline for a single ticker. Useful for inspecting what a real baseline
    looks like for one of your portfolio names.
    """
    parser = argparse.ArgumentParser(description="Compute percentile baseline for a ticker")
    parser.add_argument("--ticker", required=True, help="Ticker symbol")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    args = parser.parse_args()

    # Import data source modules
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_sources_dir = os.path.normpath(os.path.join(script_dir, "..", "data sources"))
    sys.path.insert(0, data_sources_dir)
    try:
        from priceData import fetch_price_data
        from volumeData import fetch_volume_data
        from earningsCalendar import fetch_earnings_calendar
    except ImportError as e:
        print(f"Could not import data sources: {e}", file=sys.stderr)
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

    print(json.dumps(baseline, indent=2, default=str))
    return 0 if baseline["data_points_used"]["daily_moves"] >= MIN_DATA_POINTS else 1


if __name__ == "__main__":
    sys.exit(_cli())