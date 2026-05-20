"""
Portfolio performance metrics vs SPY benchmark.

Inputs: Alpaca portfolio_history (daily equity series) + SPY bars.
Outputs: a `performance` dict + two flat time-series dicts ready to be written
as portfolio_value_history.json and benchmark_history.json.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ============ Series alignment ============

def _portfolio_series(portfolio_history: Optional[dict]) -> list[dict]:
    """
    Normalises Alpaca's portfolio_history into [{date, value}, ...] form.

    Alpaca returns parallel arrays: timestamp (unix s), equity (USD). Both
    indexed daily for the 1A/1D request.
    """
    if not portfolio_history:
        return []
    ts = portfolio_history.get("timestamp") or []
    eq = portfolio_history.get("equity") or []
    out: list[dict] = []
    for i, t in enumerate(ts):
        if i >= len(eq):
            break
        value = eq[i]
        if value is None:
            continue
        try:
            if isinstance(t, (int, float)):
                d = datetime.fromtimestamp(int(t), tz=timezone.utc).date().isoformat()
            else:
                d = str(t)[:10]
            out.append({"date": d, "value": float(value)})
        except Exception:
            continue
    return out


def _benchmark_series(benchmark: Optional[dict]) -> list[dict]:
    bars = (benchmark or {}).get("bars") or []
    out = []
    for b in bars:
        d = b.get("date")
        c = b.get("close")
        if d and c is not None:
            try:
                out.append({"date": d, "value": float(c)})
            except (TypeError, ValueError):
                continue
    return out


def _first_funded_date(series: list[dict]) -> Optional[str]:
    """First date where the portfolio had a non-zero value."""
    for entry in series:
        v = entry.get("value")
        if v is not None and v > 0:
            return entry["date"]
    return None


def _align_to_portfolio_dates(
    portfolio: list[dict], benchmark: list[dict]
) -> list[dict]:
    """Forward-fill benchmark series so each portfolio date has a benchmark value."""
    if not portfolio or not benchmark:
        return []
    bench_by_date = {b["date"]: b["value"] for b in benchmark}
    bench_dates = sorted(bench_by_date.keys())

    aligned: list[dict] = []
    last_bench = None
    bench_idx = 0
    for entry in portfolio:
        d = entry["date"]
        while bench_idx < len(bench_dates) and bench_dates[bench_idx] <= d:
            last_bench = bench_by_date[bench_dates[bench_idx]]
            bench_idx += 1
        if last_bench is not None:
            aligned.append({"date": d, "value": last_bench})
    return aligned


def _normalise(series: list[dict]) -> list[dict]:
    """Add a 'normalised' field starting at 1.0 on first date."""
    if not series:
        return []
    base = None
    out = []
    for entry in series:
        v = entry["value"]
        if base is None:
            base = v if v else None
            norm = 1.0 if base else None
        else:
            norm = (v / base) if base else None
        out.append({"date": entry["date"], "value": v, "normalised": norm})
    return out


# ============ Metric helpers ============

def _safe_pct(num, denom):
    if num is None or denom in (None, 0):
        return None
    try:
        return num / denom
    except Exception:
        return None


def _return_over_window(series: list[dict], days: int) -> Optional[float]:
    """
    Total return over the last `days` calendar days. Picks the value
    closest to (and not after) the target start date.
    """
    if not series or days <= 0:
        return None
    end = series[-1]
    try:
        end_date = datetime.fromisoformat(end["date"]).date()
    except Exception:
        return None

    target = end_date.toordinal() - days
    candidate = None
    for entry in series:
        try:
            d = datetime.fromisoformat(entry["date"]).date().toordinal()
        except Exception:
            continue
        if d <= target:
            candidate = entry
        else:
            break
    if not candidate or not candidate["value"]:
        return None
    return (end["value"] / candidate["value"]) - 1.0


def _return_ytd(series: list[dict]) -> Optional[float]:
    if not series:
        return None
    end = series[-1]
    try:
        end_date = datetime.fromisoformat(end["date"]).date()
    except Exception:
        return None
    ytd_anchor_date = end_date.replace(month=1, day=1).isoformat()
    candidate = None
    for entry in series:
        if entry["date"] < ytd_anchor_date:
            candidate = entry
        else:
            break
    if candidate is None:
        candidate = series[0]
    if not candidate["value"]:
        return None
    return (end["value"] / candidate["value"]) - 1.0


def _return_mtd(series: list[dict]) -> Optional[float]:
    if not series:
        return None
    end = series[-1]
    try:
        end_date = datetime.fromisoformat(end["date"]).date()
    except Exception:
        return None
    mtd_anchor = end_date.replace(day=1).isoformat()
    candidate = None
    for entry in series:
        if entry["date"] < mtd_anchor:
            candidate = entry
        else:
            break
    if candidate is None:
        candidate = series[0]
    if not candidate["value"]:
        return None
    return (end["value"] / candidate["value"]) - 1.0


def _daily_returns(series: list[dict]) -> list[float]:
    returns: list[float] = []
    prev = None
    for entry in series:
        v = entry["value"]
        if prev not in (None, 0) and v is not None:
            try:
                returns.append((v / prev) - 1.0)
            except Exception:
                pass
        prev = v
    return returns


def _rolling_30d_sharpe(series: list[dict]) -> Optional[float]:
    rets = _daily_returns(series)
    if len(rets) < 5:
        return None
    window = rets[-30:] if len(rets) > 30 else rets
    if not window:
        return None
    mean = sum(window) / len(window)
    variance = sum((r - mean) ** 2 for r in window) / max(len(window) - 1, 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return None
    return (mean / std) * math.sqrt(252)


def _drawdowns(series: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """Returns (max_drawdown_pct, current_drawdown_pct) where both are negative or 0."""
    if not series:
        return None, None
    peak = None
    max_dd = 0.0
    current_dd = 0.0
    for entry in series:
        v = entry["value"]
        if v is None:
            continue
        if peak is None or v > peak:
            peak = v
        if peak:
            dd = (v / peak) - 1.0
            if dd < max_dd:
                max_dd = dd
            current_dd = dd
    return max_dd, current_dd


# ============ Entry point ============

def compute(
    portfolio_history: Optional[dict],
    benchmark: Optional[dict],
) -> dict:
    """
    Returns:
        {
            "performance": { ...summary metrics... },
            "portfolio_value_history": {as_of, series:[{date,value,normalised}]},
            "benchmark_history": {as_of, series:[{date,value,normalised}], symbol},
        }
    """
    portfolio_series = _portfolio_series(portfolio_history)
    benchmark_series_raw = _benchmark_series(benchmark)

    # Re-anchor: drop the pre-funded period from the portfolio series so the
    # chart and the inception/return metrics start on the portfolio's first
    # funded day, not on Alpaca's ~365-day lookback window. Benchmark stays
    # full-history so _align_to_portfolio_dates can forward-fill SPY into the
    # leading portfolio dates from its last pre-inception close (e.g. when
    # inception falls on a weekend).
    inception = _first_funded_date(portfolio_series)
    if inception:
        portfolio_series = [e for e in portfolio_series if e["date"] >= inception]

    benchmark_aligned = _align_to_portfolio_dates(portfolio_series, benchmark_series_raw)

    portfolio_norm = _normalise(portfolio_series)
    benchmark_norm = _normalise(benchmark_aligned)

    perf: dict = {
        "since_inception_days": None,
        "total_return_pct": None,
        "mtd_return": None,
        "ytd_return": None,
        "m1_return": None,
        "m3_return": None,
        "m6_return": None,
        "m12_return": None,
        "spy_mtd": None,
        "spy_ytd": None,
        "spy_m1": None,
        "spy_m3": None,
        "spy_m6": None,
        "spy_m12": None,
        "rolling_30d_sharpe": None,
        "max_drawdown_pct": None,
        "current_drawdown_pct": None,
    }

    if portfolio_series:
        first = portfolio_series[0]
        last = portfolio_series[-1]
        try:
            d0 = datetime.fromisoformat(first["date"]).date()
            d1 = datetime.fromisoformat(last["date"]).date()
            perf["since_inception_days"] = (d1 - d0).days
        except Exception:
            pass
        if first["value"]:
            perf["total_return_pct"] = (last["value"] / first["value"]) - 1.0
        perf["mtd_return"] = _return_mtd(portfolio_series)
        perf["ytd_return"] = _return_ytd(portfolio_series)
        perf["m1_return"] = _return_over_window(portfolio_series, 30)
        perf["m3_return"] = _return_over_window(portfolio_series, 91)
        perf["m6_return"] = _return_over_window(portfolio_series, 182)
        perf["m12_return"] = _return_over_window(portfolio_series, 365)
        perf["rolling_30d_sharpe"] = _rolling_30d_sharpe(portfolio_series)
        max_dd, current_dd = _drawdowns(portfolio_series)
        perf["max_drawdown_pct"] = max_dd
        perf["current_drawdown_pct"] = current_dd

    if benchmark_aligned:
        perf["spy_mtd"] = _return_mtd(benchmark_aligned)
        perf["spy_ytd"] = _return_ytd(benchmark_aligned)
        perf["spy_m1"] = _return_over_window(benchmark_aligned, 30)
        perf["spy_m3"] = _return_over_window(benchmark_aligned, 91)
        perf["spy_m6"] = _return_over_window(benchmark_aligned, 182)
        perf["spy_m12"] = _return_over_window(benchmark_aligned, 365)

    as_of = datetime.now(timezone.utc).isoformat()
    return {
        "performance": perf,
        "portfolio_value_history": {
            "as_of": as_of,
            "source": "alpaca_portfolio_history",
            "series": portfolio_norm,
        },
        "benchmark_history": {
            "as_of": as_of,
            "symbol": (benchmark or {}).get("symbol", "SPY"),
            "series": benchmark_norm,
            "aligned_to": "portfolio_dates",
        },
    }
